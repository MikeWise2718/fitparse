import socket
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request, send_file, url_for

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


# The landing card's gallery, in order; the first is also /api/screenshot. All made by
# tools/screenshot.py: 1 and 3 from an invented training year, 2 from one real run that Mike
# chose to show (2026-10-15), with the first and last 500 m of its route cut off the map.
SCREENSHOTS = [
    ('screenshot.png', 'Fitness, fatigue and form (demo data)'),
    ('screenshots/2-playback.webp', 'Playback of a real 10 km run: route, zones, heart rate'),
    ('screenshots/3-trends.png', 'Trends (demo data)'),
]


@core_bp.route('/api/screenshots')
def screenshots():
    """Fleet standard: no auth, no redirect, URLs on this app. Missing files are skipped."""
    static = Path(current_app.static_folder)
    return jsonify({'screenshots': [{'url': url_for('static', filename=f), 'caption': c}
                                    for f, c in SCREENSHOTS if (static / f).exists()]})


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
