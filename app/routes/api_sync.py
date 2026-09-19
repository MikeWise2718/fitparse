from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func

from .. import auth
from ..events import event_logger
from ..models import GarminActivity, Job, db, utcnow
from ..services import jobs
from ..sync import activities, client as gc

sync_bp = Blueprint('sync', __name__)


def _web_connect_allowed() -> tuple[bool, str | None]:
    """The Garmin password may only cross an encrypted connection, and only for users an
    admin has enabled the flow for. Everyone else uploads files or an export zip."""
    if not g.user.garmin_web_connect:
        return False, 'not_enabled'
    if not (request.is_secure or current_app.config.get('TESTING')
            or request.host.split(':')[0] in ('localhost', '127.0.0.1')):
        return False, 'https_required'
    return True, None


@sync_bp.route('/api/sync/status')
def status():
    home = current_app.config['FITMON_HOME']
    acct = activities.account(g.user.id)
    if acct.status == 'ok' and not gc.has_tokens(home, g.user.id):
        acct.status = 'disconnected'
        db.session.commit()
    counts = dict(db.session.query(GarminActivity.status, func.count())
                  .filter(GarminActivity.user_id == g.user.id).group_by(GarminActivity.status).all())
    running = (auth.scoped(Job).filter(Job.kind == 'sync', Job.status.in_(jobs.ACTIVE))
               .order_by(Job.id.desc()).first())
    last = (auth.scoped(Job).filter(Job.kind == 'sync', Job.status.in_(('done', 'failed')))
            .order_by(Job.id.desc()).first())
    allowed, reason = _web_connect_allowed()
    return jsonify({
        'auth': acct.status, 'last_sync_at': acct.last_sync_at, 'last_error': acct.last_error,
        'backfill_done': acct.backfill_done, 'health_synced_to': acct.health_synced_to,
        'counts': counts, 'running': jobs.job_dict(running) if running else None,
        'last_job': jobs.job_dict(last) if last else None,
        'web_connect': {'allowed': allowed, 'reason': reason},
        'cli_hint': 'fitmon-sync login -u ' + g.user.username,
    })


def _login_response(fn, *args):
    home = current_app.config['FITMON_HOME']
    try:
        fn(home, g.user.id, *args)
    except gc.MfaRequired:
        return jsonify({'status': 'needs_mfa'})
    except gc.SyncRateLimited:
        return jsonify({'error': 'rate_limited', 'message': 'Garmin is rate limiting logins. Try again later.'}), 429
    except gc.SyncAuthError as exc:
        event_logger.warning('sync.connect_failed', 'garmin connect failed', user_id=g.user.id)
        return jsonify({'error': 'garmin_login_failed', 'message': str(exc)}), 401
    except Exception as exc:
        # Deliberately not echoing the exception: library errors can embed request details.
        event_logger.error('sync.connect_failed', f'garmin connect error: {type(exc).__name__}', user_id=g.user.id)
        return jsonify({'error': 'garmin_login_failed', 'message': 'Garmin login failed.'}), 502
    acct = activities.account(g.user.id)
    acct.status, acct.connected_at, acct.last_error = 'ok', utcnow(), None
    db.session.commit()
    event_logger.info('sync.connected', 'garmin account connected', user_id=g.user.id)
    return jsonify({'status': 'ok'})


@sync_bp.route('/api/sync/login', methods=['POST'])
def login():
    allowed, reason = _web_connect_allowed()
    if not allowed:
        return jsonify({'error': reason}), 403
    data = request.get_json(silent=True) or {}
    email, password = (data.get('email') or '').strip(), data.get('password') or ''
    if not email or not password:
        return jsonify({'error': 'invalid_input'}), 400
    return _login_response(gc.login_with_password, email, password)


@sync_bp.route('/api/sync/login/mfa', methods=['POST'])
def login_mfa():
    allowed, reason = _web_connect_allowed()
    if not allowed:
        return jsonify({'error': reason}), 403
    code = str((request.get_json(silent=True) or {}).get('code') or '').strip()
    if not code:
        return jsonify({'error': 'invalid_input'}), 400
    return _login_response(gc.complete_mfa, code)


@sync_bp.route('/api/sync/disconnect', methods=['POST'])
def disconnect():
    gc.delete_tokens(current_app.config['FITMON_HOME'], g.user.id)
    acct = activities.account(g.user.id)
    acct.status = 'disconnected'
    db.session.commit()
    event_logger.info('sync.disconnected', 'garmin account disconnected', user_id=g.user.id)
    return jsonify({'ok': True})


@sync_bp.route('/api/sync/run', methods=['POST'])
def run():
    if not gc.has_tokens(current_app.config['FITMON_HOME'], g.user.id):
        return jsonify({'error': 'login_required'}), 409
    data = request.get_json(silent=True) or {}
    existing = auth.scoped(Job).filter(Job.kind == 'sync', Job.status.in_(jobs.ACTIVE)).first()
    if existing:
        return jsonify({'error': 'already_running', 'job': jobs.job_dict(existing)}), 409
    job = jobs.enqueue(g.user.id, 'sync', {'full': bool(data.get('full')), 'health': bool(data.get('health', True))})
    return jsonify({'job': jobs.job_dict(job)}), 202


@sync_bp.route('/api/sync/activities')
def sync_activities():
    q = auth.scoped(GarminActivity)
    if request.args.get('status'):
        q = q.filter(GarminActivity.status == request.args['status'])
    rows = q.order_by(GarminActivity.start_time.desc()).limit(200).all()
    return jsonify({'activities': [{
        'activity_id': r.activity_id, 'name': r.name, 'type': r.type_key, 'start_time': r.start_time,
        'status': r.status, 'error': r.error, 'attempts': r.attempts, 'file_id': r.file_id} for r in rows]})


@sync_bp.route('/api/sync/activities/<activity_id>/retry', methods=['POST'])
def retry(activity_id):
    row = db.session.get(GarminActivity, (g.user.id, activity_id))
    if row is None:
        return jsonify({'error': 'not_found'}), 404
    row.status, row.attempts, row.error = 'new', 0, None
    db.session.commit()
    return jsonify({'ok': True})
