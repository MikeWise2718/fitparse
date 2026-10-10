import socket
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request, send_file

from .. import __version__
from ..settings import get_user_settings, save_user_settings

core_bp = Blueprint('core', __name__)


@core_bp.route('/api/ping')
def ping():
    return jsonify({
        'hostname': socket.gethostname(),
        'status': 'ok',
        'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'version': __version__,
    })


@core_bp.route('/api/screenshot')
def screenshot():
    """Fleet standard (pokeflute docs/service-screenshots.md): the landing page shows this on
    fitmon's card. Public like /api/ping - no auth, no redirect - so it must only ever show demo
    data: it is made by tools/screenshot.py from an invented training year, never real data."""
    path = Path(current_app.static_folder) / 'screenshot.png'
    if not path.exists():
        return jsonify({'error': 'no screenshot'}), 404
    return send_file(path, mimetype='image/png', conditional=True, max_age=3600)


@core_bp.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(get_user_settings(current_app.config['FITMON_HOME'], g.user.id))


@core_bp.route('/api/settings', methods=['POST'])
def post_settings():
    updates = request.get_json(silent=True) or {}
    if not isinstance(updates, dict):
        return jsonify({'error': 'expected_object'}), 400
    return jsonify(save_user_settings(current_app.config['FITMON_HOME'], g.user.id, updates))
