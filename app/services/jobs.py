"""Job queue. Web requests enqueue; the single worker (app/worker.py) executes.

One worker means imports, reindexes and Garmin syncs are serialised: it is the only bulk
writer, so SQLite's single-writer limit never surfaces as 'database is locked' in a request,
and only one Garmin back-fill can hammer the shared source IP at a time.
"""
import json
import time
import uuid
from pathlib import Path

from ..events import event_logger
from ..models import FitFile, Job, db, utcnow
from ..settings import user_dir
from . import importer

ACTIVE = ('queued', 'running')


def enqueue(user_id: int, kind: str, payload: dict | None = None, unique: bool = False) -> Job:
    if unique:
        existing = Job.query.filter(Job.user_id == user_id, Job.kind == kind, Job.status.in_(ACTIVE)).first()
        if existing:
            return existing
    job = Job(user_id=user_id, kind=kind, payload=json.dumps(payload or {}))
    db.session.add(job)
    db.session.commit()
    return job


def job_dict(job: Job) -> dict:
    return {
        'id': job.id, 'kind': job.kind, 'status': job.status, 'progress': job.progress,
        'total': job.total, 'message': job.message,
        'result': json.loads(job.result) if job.result else None,
        'created_at': job.created_at, 'started_at': job.started_at, 'finished_at': job.finished_at,
    }


def stage_upload(home: Path, user_id: int, storage) -> dict:
    """Save an uploaded werkzeug FileStorage for the worker. The client's filename is kept
    only as a label; the path on disk is ours."""
    incoming = user_dir(home, user_id) / 'incoming'
    incoming.mkdir(parents=True, exist_ok=True)
    path = incoming / f'{uuid.uuid4().hex}.bin'
    storage.save(path)
    return {'path': str(path), 'name': (storage.filename or 'upload')[-255:]}


class Progress:
    """Throttled progress writer so a 5,000-file import is not 5,000 commits."""
    def __init__(self, job: Job, total: int = 0):
        self.job, self._last = job, 0.0
        job.total = total
        db.session.commit()

    def update(self, progress: int, message: str | None = None, total: int | None = None, force=False):
        self.job.progress = progress
        if total is not None:
            self.job.total = total
        if message:
            self.job.message = message[:500]
        if force or time.monotonic() - self._last > 1.0:
            db.session.commit()
            self._last = time.monotonic()


def _tally(results: list) -> dict:
    counts: dict = {}
    for r in results:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    problems = [r for r in results if r['status'] in ('failed', 'rejected', 'partial')][:200]
    return {'counts': counts, 'problems': problems}


def run_import(home: Path, job: Job, payload: dict) -> dict:
    items = payload.get('files', [])
    progress = Progress(job, len(items))
    results = []
    for i, item in enumerate(items):
        path = Path(item['path'])
        try:
            if path.exists():
                raw = path.read_bytes()
                for result in importer.import_upload(home, job.user_id, raw, item['name']):
                    results.append(result)
                    progress.update(i, f"{result['name']}: {result['status']}")
        finally:
            path.unlink(missing_ok=True)
        progress.update(i + 1)
    return _tally(results)


def run_scan(home: Path, job: Job, payload: dict) -> dict:
    paths = importer.scan_paths(payload['directory'])
    progress = Progress(job, len(paths))
    results = []
    for i, path in enumerate(paths):
        try:
            with open(path, 'rb') as fh:
                raw = fh.read(importer.MAX_ZIP_TOTAL_BYTES)
            for result in importer.import_upload(home, job.user_id, raw, path, source='scan'):
                results.append(result)
        except OSError as exc:
            results.append({'status': 'failed', 'name': path, 'reason': str(exc)})
        progress.update(i + 1, path)
    return _tally(results)


def run_reindex(home: Path, job: Job, payload: dict) -> dict:
    """Resumable: files already at the current schema version are skipped unless forced."""
    from ..models import SCHEMA_VERSION
    q = FitFile.query.filter_by(user_id=job.user_id)
    if not payload.get('force'):
        q = q.filter(FitFile.schema_version != SCHEMA_VERSION)
    ids = [row.id for row in q.order_by(FitFile.start_time).all()]
    progress = Progress(job, len(ids))
    counts: dict = {}
    for i, file_id in enumerate(ids):
        row = db.session.get(FitFile, file_id)
        if row:
            status = importer.reindex_file(home, row)
            counts[status] = counts.get(status, 0) + 1
        progress.update(i + 1, f're-indexing {i + 1}/{len(ids)}')
    return {'counts': counts}


def run_sync(home: Path, job: Job, payload: dict) -> dict:
    from ..sync import activities, health
    progress = Progress(job)
    result = activities.sync_user(home, job.user_id, progress, full=bool(payload.get('full')),
                                  limit=payload.get('limit'))
    if payload.get('health') and not result.get('stopped'):
        result['health'] = health.sync_user(home, job.user_id, progress)
        result['stopped'] = result['health'].get('stopped')
    if result.get('stopped') in ('rate_limited', 'budget'):
        # Every account leaves from the same IP: a 429 ends the night for everybody.
        skipped = Job.query.filter(Job.kind == 'sync', Job.status == 'queued').update(
            {'status': 'done', 'finished_at': utcnow(),
             'message': f"skipped: {result['stopped']} during another sync"}, synchronize_session=False)
        db.session.commit()
        result['skipped_jobs'] = skipped
    return result


HANDLERS = {'import': run_import, 'scan': run_scan, 'reindex': run_reindex, 'sync': run_sync}


def claim_next() -> Job | None:
    job = Job.query.filter_by(status='queued').order_by(Job.id).first()
    if not job:
        return None
    claimed = Job.query.filter_by(id=job.id, status='queued').update(
        {'status': 'running', 'started_at': utcnow()}, synchronize_session=False)
    db.session.commit()
    return db.session.get(Job, job.id) if claimed else None


def execute(home: Path, job: Job) -> None:
    started = time.monotonic()
    event_logger.info('job.started', f'job {job.id} ({job.kind}) started', job_id=job.id,
                      user_id=job.user_id, kind=job.kind)
    try:
        result = HANDLERS[job.kind](home, job, json.loads(job.payload or '{}'))
        job = db.session.get(Job, job.id)
        job.status, job.result = 'done', json.dumps(result, default=str)
        job.progress = job.total or job.progress
    except Exception as exc:
        db.session.rollback()
        job = db.session.get(Job, job.id)
        job.status, job.message = 'failed', f'{type(exc).__name__}: {exc}'[:500]
        event_logger.error('job.failed', job.message, job_id=job.id, user_id=job.user_id, kind=job.kind)
    job.finished_at = utcnow()
    db.session.commit()
    event_logger.info('job.finished', f'job {job.id} ({job.kind}) {job.status}', job_id=job.id,
                      user_id=job.user_id, status=job.status, seconds=round(time.monotonic() - started, 1))


def requeue_interrupted() -> int:
    """At worker start: anything still 'running' died with the previous worker. Every handler
    is idempotent (imports dedupe by hash, reindex and sync resume), so just run it again."""
    n = Job.query.filter_by(status='running').update({'status': 'queued'}, synchronize_session=False)
    db.session.commit()
    return n
