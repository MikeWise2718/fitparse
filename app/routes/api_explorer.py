import csv
import io
import json
import re

from flask import Blueprint, Response, current_app, g, jsonify, request

from .. import auth
from ..models import FitFile
from ..services import explorer, importer

explorer_bp = Blueprint('explorer', __name__)

MESSAGE_NAME = re.compile(r'^[a-z0-9_]{1,64}$')


def _owned_path(file_id: int):
    f = auth.get_owned(FitFile, file_id)
    path = importer.fit_path(current_app.config['FITMON_HOME'], g.user.id, f.sha256)
    if not path.exists():
        from flask import abort
        abort(404)
    return f, path


def _message_arg(name: str) -> str:
    if not MESSAGE_NAME.match(name or ''):
        from flask import abort
        abort(400)
    return name


@explorer_bp.route('/api/explorer/files')
def files():
    q = auth.scoped(FitFile).order_by(FitFile.start_time.desc())
    text = (request.args.get('q') or '').strip()
    if text:
        q = q.filter(FitFile.original_name.ilike(f'%{text}%'))
    rows = q.limit(min(request.args.get('limit', 200, type=int), 1000)).all()
    return jsonify({'files': [{'id': f.id, 'name': f.original_name, 'start_time': f.start_time,
                               'size': f.size, 'parse_status': f.parse_status, 'product': f.product,
                               'sports': [s.sport for s in f.sessions]} for f in rows]})


@explorer_bp.route('/api/explorer/<int:file_id>/messages')
def messages(file_id):
    f, path = _owned_path(file_id)
    counts = json.loads(f.message_counts or '{}')
    out = {
        'file': {'id': f.id, 'name': f.original_name, 'parse_status': f.parse_status,
                 'parse_error': f.parse_error, 'manufacturer': f.manufacturer, 'product': f.product,
                 'serial_number': f.serial_number, 'time_created': f.time_created, 'sha256': f.sha256},
        'header': explorer.header_info(path),
        'messages': [{'name': k, 'count': v, 'unknown': k.startswith('unknown_')}
                     for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
    }
    if request.args.get('crc') == '1':
        out['crc'] = explorer.check_crc(path)
    return jsonify(out)


@explorer_bp.route('/api/explorer/<int:file_id>/messages/<name>')
def message_rows(file_id, name):
    _, path = _owned_path(file_id)
    return jsonify(explorer.message_page(
        path, _message_arg(name), request.args.get('page', 1, type=int),
        request.args.get('per_page', 100, type=int), request.args.get('units') == 'standard'))


@explorer_bp.route('/api/explorer/<int:file_id>/series')
def series(file_id):
    _, path = _owned_path(file_id)
    field = request.args.get('field', '')
    if not MESSAGE_NAME.match(field):
        return jsonify({'error': 'bad_request'}), 400
    return jsonify(explorer.field_series(path, _message_arg(request.args.get('message', 'record')), field))


@explorer_bp.route('/api/explorer/<int:file_id>/dump/<name>.<fmt>')
def dump(file_id, name, fmt):
    f, path = _owned_path(file_id)
    name = _message_arg(name)
    standard = request.args.get('units') == 'standard'
    stem = f'{(f.original_name or "file").rsplit(".", 1)[0]}-{name}'
    if fmt == 'json':
        body = json.dumps(explorer.message_dump(path, name, standard), indent=1)
        mimetype = 'application/json'
    elif fmt == 'csv':
        page = explorer.message_page(path, name, 1, 10 ** 9, standard)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([fd['name'] + (f" ({fd['units']})" if fd['units'] else '') for fd in page['fields']])
        rows = explorer._cached(path, name, standard)[1]
        names = [fd['name'] for fd in page['fields']]
        for row in rows:
            writer.writerow([row.get(n, (None, None))[0] for n in names])
        body, mimetype = out.getvalue(), 'text/csv'
    else:
        return jsonify({'error': 'bad_request'}), 400
    return Response(body, mimetype=mimetype,
                    headers={'Content-Disposition': f'attachment; filename="{stem}.{fmt}"'})


@explorer_bp.route('/api/explorer/search')
def search():
    """Which of my files contain message X - answered from the counts stored at import."""
    name = _message_arg(request.args.get('message', ''))
    hits = []
    for f in auth.scoped(FitFile).order_by(FitFile.start_time.desc()).all():
        n = json.loads(f.message_counts or '{}').get(name)
        if n:
            hits.append({'id': f.id, 'name': f.original_name, 'start_time': f.start_time, 'count': n})
    return jsonify({'message': name, 'files': hits})
