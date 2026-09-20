from datetime import date

from flask import Blueprint, current_app, g, jsonify, request

from ..services import fitness
from ..settings import get_user_settings

trends_bp = Blueprint('trends', __name__)


def _day(name: str):
    try:
        return date.fromisoformat(request.args[name]) if request.args.get(name) else None
    except ValueError:
        return None


def _range():
    from datetime import datetime
    start, end = _day('from'), _day('to')
    as_dt = lambda d: datetime.combine(d, datetime.min.time()) if d else None  # noqa: E731
    return as_dt(start), as_dt(end)


def _prefs() -> dict:
    return get_user_settings(current_app.config['FITMON_HOME'], g.user.id)


@trends_bp.route('/api/dashboard')
def dashboard():
    prefs = _prefs()
    return jsonify(fitness.dashboard(g.user.id, prefs['week_start'], prefs['load_sports'] or None))


@trends_bp.route('/api/fitness')
def fitness_series():
    return jsonify(fitness.fitness_series(g.user.id, _day('from'), _day('to'),
                                          _prefs()['load_sports'] or None))


@trends_bp.route('/api/trends/volume')
def volume():
    bucket = request.args.get('bucket', 'week')
    if bucket not in ('week', 'month', 'year'):
        bucket = 'week'
    start, end = _range()
    return jsonify(fitness.volume(g.user.id, bucket, start, end, _prefs()['week_start']))


@trends_bp.route('/api/trends/vo2max')
def vo2max():
    start, end = _range()
    return jsonify({
        'points': fitness.vo2max_series(g.user.id, request.args.get('sport'),
                                        request.args.get('sub_sport'), start, end,
                                        changes_only=request.args.get('all') != '1'),
        # Garmin's own smoothed figure, where the health sync has fetched it. It runs about a
        # point above the per-activity value decoded from the FIT files.
        'garmin': fitness.garmin_vo2max_series(g.user.id, _day('from'), _day('to')),
    })


@trends_bp.route('/api/trends/power-curve')
def power_curve():
    start, end = _range()
    # One sport per curve: running power and cycling power are not the same quantity.
    sport = request.args.get('sport') or 'cycling'
    return jsonify({'sport': sport, 'all_time': fitness.best_curve(g.user.id, 'power', sport),
                    'range': fitness.best_curve(g.user.id, 'power', sport, start, end)})


@trends_bp.route('/api/trends/best-efforts')
def best_efforts():
    start, end = _range()
    return jsonify({'all_time': fitness.best_curve(g.user.id, 'pace', 'running'),
                    'range': fitness.best_curve(g.user.id, 'pace', 'running', start, end)})


@trends_bp.route('/api/trends/records')
def record_progression():
    kind = request.args.get('kind', 'power')
    if kind not in ('power', 'pace'):
        kind = 'power'
    window = request.args.get('window', 1200 if kind == 'power' else 5000, type=int)
    return jsonify({'kind': kind, 'window': window,
                    'progression': fitness.record_progression(g.user.id, kind, window,
                                                              request.args.get('sport') or None)})


@trends_bp.route('/api/trends/efficiency')
def efficiency():
    start, end = _range()
    sport = request.args.get('sport', 'running')
    return jsonify({'sport': sport, 'points': fitness.efficiency_series(g.user.id, sport, start, end)})


@trends_bp.route('/api/trends/swim')
def swim():
    start, end = _range()
    return jsonify({'points': fitness.swim_series(g.user.id, start, end)})


@trends_bp.route('/api/trends/strength')
def strength():
    start, end = _range()
    return jsonify({'categories': fitness.strength_series(g.user.id, start, end)})


@trends_bp.route('/api/trends/training-effect')
def training_effect():
    start, end = _range()
    return jsonify({'points': fitness.te_distribution(g.user.id, start, end)})


@trends_bp.route('/api/trends/calendar')
def calendar():
    return jsonify({'days': fitness.calendar(g.user.id, request.args.get('year', date.today().year, type=int))})


@trends_bp.route('/api/body')
def body():
    return jsonify(fitness.body_series(g.user.id))


@trends_bp.route('/api/gear')
def gear():
    return jsonify({'devices': fitness.gear(g.user.id)})
