import os

from flask import Blueprint, current_app, g, jsonify, request

from .. import auth
from ..events import event_logger
from ..models import FitFile, Job
from ..services import jobs

import_bp = Blueprint('imports', __name__)

ALLOWED = ('.fit', '.zip')


@import_bp.route('/api/import/upload', methods=['POST'])
def upload():
    files = [f for f in request.files.getlist('files') if f and f.filename]
    files = [f for f in files if f.filename.lower().endswith(ALLOWED)]
    if not files:
        return jsonify({'error': 'no_files', 'message': 'Choose .fit or .zip files.'}), 400
    home = current_app.config['FITMON_HOME']
    staged = [jobs.stage_upload(home, g.user.id, f) for f in files]
    job = jobs.enqueue(g.user.id, 'import', {'files': staged})
    event_logger.info('import.upload_queued', f'{len(staged)} file(s) queued', user_id=g.user.id, job_id=job.id)
    return jsonify({'job': jobs.job_dict(job)}), 202


@import_bp.route('/api/import/scan', methods=['POST'])
@auth.admin_required   # reads an arbitrary server path: never for ordinary users
def scan():
    directory = ((request.get_json(silent=True) or {}).get('directory') or '').strip()
    if not directory or not os.path.isdir(directory):
        return jsonify({'error': 'not_a_directory'}), 400
    job = jobs.enqueue(g.user.id, 'scan', {'directory': directory})
    event_logger.info('import.scan_queued', f'scan of {directory}', user_id=g.user.id, job_id=job.id)
    return jsonify({'job': jobs.job_dict(job)}), 202


@import_bp.route('/api/import/reindex', methods=['POST'])
def reindex():
    force = bool((request.get_json(silent=True) or {}).get('force', True))
    job = jobs.enqueue(g.user.id, 'reindex', {'force': force}, unique=True)
    return jsonify({'job': jobs.job_dict(job)}), 202


@import_bp.route('/api/jobs')
def list_jobs():
    rows = auth.scoped(Job).order_by(Job.id.desc()).limit(20).all()
    return jsonify({'jobs': [jobs.job_dict(j) for j in rows]})


@import_bp.route('/api/jobs/<int:job_id>')
def get_job(job_id):
    return jsonify({'job': jobs.job_dict(auth.get_owned(Job, job_id))})


@import_bp.route('/api/import/problems')
def problems():
    rows = (auth.scoped(FitFile).filter(FitFile.parse_status != 'ok')
            .order_by(FitFile.imported_at.desc()).limit(200).all())
    return jsonify({'files': [{
        'id': f.id, 'name': f.original_name, 'status': f.parse_status, 'error': f.parse_error,
        'start_time': f.start_time, 'imported_at': f.imported_at,
    } for f in rows]})
