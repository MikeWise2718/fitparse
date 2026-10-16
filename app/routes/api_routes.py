"""Route library and route planner API (specs/route-planner.md section 7).

The library endpoints are scoped like everything else. The planner endpoints take the plan
being edited in the request body - the browser holds it, with its undo history - and return
the changed plan, so nothing half-edited is stored on the server.
"""
from flask import Blueprint, Response, current_app, g, jsonify, request

from .. import auth
from ..events import event_logger
from ..models import Route, Session, db
from ..routing import fit, geo, plan
from ..routing.brouter import SPORT_LABELS, RouterError, default_router, sport_for_session
from ..services import routelib
from ..settings import get_user_settings

routes_bp = Blueprint('routelib', __name__)

LIST_FIELDS = ['id', 'name', 'sport', 'kind', 'distance_m', 'ascent_m', 'unpaved_pct', 'parent_id',
               'source', 'starred', 'created_at', 'updated_at', 'start_lat', 'start_lon', 'notes']


def _home():
    return current_app.config['FITMON_HOME']


def _router():
    return current_app.config.get('FITMON_ROUTER') or default_router()


def _route_dict(r: Route, counts=None, last=None) -> dict:
    out = {k: getattr(r, k) for k in LIST_FIELDS}
    out['sport_label'] = SPORT_LABELS.get(r.sport, r.sport)
    if counts is not None:
        out['times'] = counts.get(r.id, 0)
        out['last_done'] = last.get(r.id)
    return out


def _error(code: str, message: str, status: int = 400):
    return jsonify({'error': code, 'message': message}), status


def _engine_error(exc: RouterError):
    unreachable = 'unreachable' in str(exc)
    return _error('engine_unreachable' if unreachable else 'no_route',
                  'The routing engine is not running.' if unreachable else
                  'No path found there. Try a point nearer a road or track.', 503 if unreachable else 422)


@routes_bp.route('/api/routes')
def list_routes():
    pending = routelib.refresh(_home(), g.user.id, budget_s=float(request.args.get('budget', 3)))
    q = auth.scoped(Route)
    if request.args.get('sport'):
        q = q.filter(Route.sport.in_(request.args['sport'].split(',')))
    text = (request.args.get('q') or '').strip()
    if text:
        q = q.filter(db.or_(Route.name.ilike(f'%{text}%'), Route.notes.ilike(f'%{text}%')))
    routes = q.order_by(Route.starred.desc(), Route.name).all()
    from ..models import RouteMatch
    rows = (db.session.query(RouteMatch.route_id, db.func.count(), db.func.max(Session.start_time))
            .join(Session, db.and_(Session.file_id == RouteMatch.file_id, Session.idx == RouteMatch.idx))
            .filter(RouteMatch.user_id == g.user.id).group_by(RouteMatch.route_id).all())
    counts = {r[0]: r[1] for r in rows}
    last = {r[0]: r[2] for r in rows}
    return jsonify({'routes': [_route_dict(r, counts, last) for r in routes], 'pending': pending,
                    'sports': SPORT_LABELS})


@routes_bp.route('/api/routes/lines')
def route_lines():
    """Every route's line, thinned, for the overview map."""
    out = []
    for r in auth.scoped(Route).all():
        coords = plan.flatten(routelib.load_plan(_home(), r))
        out.append({'id': r.id, 'sport': r.sport, 'coords': [c[:2] for c in geo.simplify(coords, 15.0)]})
    return jsonify({'lines': out})


@routes_bp.route('/api/routes', methods=['POST'])
def create_route():
    data = request.get_json(silent=True) or {}
    payload = {k: data[k] for k in ('name', 'sport', 'plan', 'notes', 'parent_id', 'starred') if k in data}
    payload['source'] = data.get('source') if data.get('source') in ('planned', 'drawn') else 'planned'
    if 'plan' not in payload:
        return _error('plan_required', 'A route needs a line.')
    try:
        route = routelib.save(_home(), g.user.id, payload)
    except ValueError as exc:
        return _error('invalid_route', str(exc))
    event_logger.info('route.created', f'route {route.id} created', user_id=g.user.id, route_id=route.id)
    return jsonify({'route': _route_dict(route)})


@routes_bp.route('/api/routes/from-session/<int:session_id>', methods=['POST'])
def route_from_session(session_id):
    sess = auth.get_owned(Session, session_id)
    rsport = sport_for_session(sess.sport, sess.sub_sport)
    if not sess.has_gps:
        return _error('no_gps', 'This activity has no GPS track to save.')
    if rsport is None:
        return _error('not_routable', 'Only runs, walks and rides can be saved as routes.')
    data = request.get_json(silent=True) or {}
    try:
        segs = routelib.plan_from_session(sess)
        route = routelib.save(_home(), g.user.id, {
            'name': data.get('name') or routelib.default_name(sess), 'sport': rsport, 'plan': segs,
            'source': 'activity', 'source_file_id': sess.file_id, 'source_idx': sess.idx})
    except ValueError as exc:
        return _error('invalid_route', str(exc))
    event_logger.info('route.created', f'route {route.id} saved from session {sess.id}', user_id=g.user.id,
                      route_id=route.id, session_id=sess.id)
    return jsonify({'route': _route_dict(route)})


@routes_bp.route('/api/routes/<int:route_id>')
def route_detail(route_id):
    r = auth.get_owned(Route, route_id)
    segs = routelib.load_plan(_home(), r)
    parent = db.session.get(Route, r.parent_id) if r.parent_id else None
    variants = auth.scoped(Route).filter(Route.parent_id == r.id).order_by(Route.distance_m).all()
    source = None
    if r.source_file_id is not None:
        s = Session.query.filter_by(file_id=r.source_file_id, idx=r.source_idx, user_id=g.user.id).first()
        source = {'session_id': s.id, 'start_time': s.start_time} if s else None
    return jsonify({
        'route': _route_dict(r), 'plan': segs, 'stats': plan.stats(segs),
        'elevation': plan.elevation_profile(segs),
        'parent': {'id': parent.id, 'name': parent.name} if parent else None,
        'variants': [{'id': v.id, 'name': v.name, 'distance_m': v.distance_m} for v in variants],
        'history': routelib.history(r), 'source_activity': source,
    })


@routes_bp.route('/api/routes/<int:route_id>/update', methods=['POST'])
def update_route(route_id):
    r = auth.get_owned(Route, route_id)
    data = request.get_json(silent=True) or {}
    payload = {k: data[k] for k in ('name', 'sport', 'plan', 'notes', 'parent_id', 'starred') if k in data}
    try:
        routelib.save(_home(), g.user.id, payload, route=r)
    except ValueError as exc:
        db.session.rollback()
        return _error('invalid_route', str(exc))
    return jsonify({'route': _route_dict(r)})


@routes_bp.route('/api/routes/<int:route_id>/delete', methods=['POST'])
def delete_route(route_id):
    r = auth.get_owned(Route, route_id)
    event_logger.info('route.deleted', f'route {r.id} deleted', user_id=g.user.id, route_id=r.id)
    routelib.delete(_home(), r)
    return jsonify({'ok': True})


@routes_bp.route('/api/routes/<int:route_id>/rematch', methods=['POST'])
def rematch_route(route_id):
    r = auth.get_owned(Route, route_id)
    routelib.ensure_shapes(g.user.id, budget_s=20)
    return jsonify({'ok': True, 'matches': routelib.match_route(r, home=_home())})


@routes_bp.route('/api/routes/<int:route_id>/gpx')
def route_gpx(route_id):
    r = auth.get_owned(Route, route_id)
    coords = plan.flatten(routelib.load_plan(_home(), r))
    safe = routelib.safe_filename(r.name)
    return Response(routelib.gpx(r, coords), mimetype='application/gpx+xml',
                    headers={'Content-Disposition': f'attachment; filename={safe}.gpx'})


@routes_bp.route('/api/routes/suggestions')
def route_suggestions():
    routelib.refresh(_home(), g.user.id, budget_s=2)
    return jsonify({'suggestions': routelib.suggestions(g.user.id)})


@routes_bp.route('/api/routes/suggestions/hide', methods=['POST'])
def hide_route_suggestion():
    key = str((request.get_json(silent=True) or {}).get('key') or '')
    if not key:
        return _error('key_required', 'Which suggestion?')
    routelib.hide_suggestion(g.user.id, key)
    return jsonify({'ok': True})


@routes_bp.route('/api/sessions/<int:session_id>/routes')
def session_routes(session_id):
    sess = auth.get_owned(Session, session_id)
    return jsonify({'routes': routelib.routes_for_session(sess),
                    'savable': bool(sess.has_gps and sport_for_session(sess.sport, sess.sub_sport))})


# --------------------------------------------------------------------------- planner

@routes_bp.route('/api/plan/status')
def plan_status():
    router = _router()
    ok = router.available() if hasattr(router, 'available') else True
    return jsonify({'engine': ok, 'sports': SPORT_LABELS})


def _plan_response(segs):
    return jsonify({'plan': segs, 'stats': plan.stats(segs), 'elevation': plan.elevation_profile(segs)})


def _plan_args():
    data = request.get_json(silent=True) or {}
    segs = routelib.validate_plan(data['plan']) if data.get('plan') else []
    sport = data.get('sport') if data.get('sport') in SPORT_LABELS else 'run'
    return data, segs, sport


def _point(raw):
    lon, lat = float(raw[0]), float(raw[1])
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError('point out of range')
    return [lon, lat]


@routes_bp.route('/api/plan/edit', methods=['POST'])
def plan_edit():
    """One editing step. op: drag (the line at distance d, to a point) | move (control k to a
    point) | delete (control k) | append (a point) | close | reverse | truncate (at d)."""
    try:
        data, segs, sport = _plan_args()
        op, router = data.get('op'), _router()
        if op == 'append':
            segs = plan.append(segs, _point(data['to']), sport, router)
        elif not segs:
            return _error('plan_required', 'Start the route first.')
        elif op == 'drag':
            segs = plan.drag(segs, float(data['d']), _point(data['to']), sport, router)
        elif op == 'move':
            segs = plan.move_control(segs, int(data['k']), _point(data['to']), sport, router)
        elif op == 'delete':
            segs = plan.delete_control(segs, int(data['k']), sport, router)
        elif op == 'close':
            segs = plan.close_loop(segs, sport, router)
        elif op == 'reverse':
            segs = plan.reverse(segs)
        elif op == 'truncate':
            segs = plan.truncate(segs, float(data['d']))
        else:
            return _error('unknown_op', 'Unknown edit.')
    except RouterError as exc:
        return _engine_error(exc)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        return _error('invalid_edit', str(exc) or 'Bad edit.')
    return _plan_response(segs)


@routes_bp.route('/api/plan/fit', methods=['POST'])
def plan_fit():
    """Suggestions that bring the plan to target_m metres."""
    try:
        data, segs, sport = _plan_args()
        target = float(data.get('target_m') or 0)
        tolerance = min(0.1, max(0.005, float(data.get('tolerance_pct') or 2) / 100))
    except (TypeError, ValueError) as exc:
        return _error('invalid_plan', str(exc))
    if not segs or target < 200:
        return _error('invalid_target', 'Give a route and a target length.')
    total = plan.total(segs)
    if not 0.4 * total <= target <= 2.0 * total:
        return _error('target_too_far', 'That is more than double or under half of this route: start from another one.')
    settings = get_user_settings(_home(), g.user.id)
    routelib.ensure_shapes(g.user.id, budget_s=2)
    known = routelib.known_ground(g.user.id, sport)
    loops = routelib.loops_for(_home(), g.user.id, sport, exclude_id=data.get('route_id'))
    try:
        cands = fit.fit_length(segs, target, sport, _router(), tolerance=tolerance, known=known,
                               prefer=settings.get('route_prefer', 'known'), loops=loops)
    except RouterError as exc:
        return _engine_error(exc)
    if not cands and not _engine_ok():
        return _error('engine_unreachable', 'The routing engine is not running.', 503)
    return jsonify({'target_m': target, 'candidates': [_candidate(c) for c in cands]})


def _engine_ok() -> bool:
    router = _router()
    return router.available() if hasattr(router, 'available') else True


def _candidate(c):
    return {'label': c['label'], 'method': c['method'], 'plan': c['plan'], 'stats': c['stats'],
            'off_m': c['off_m'], 'known_pct': c.get('known_pct'),
            'elevation': plan.elevation_profile(c['plan'])}


@routes_bp.route('/api/plan/loop', methods=['POST'])
def plan_loop():
    """New loops of target_m from a start point."""
    try:
        data, _, sport = _plan_args()
        start = _point(data['start'])
        target = float(data.get('target_m') or 0)
        rotation = float(data.get('rotation') or 0)
    except (KeyError, TypeError, ValueError) as exc:
        return _error('invalid_request', str(exc))
    if not 500 <= target <= 300000:
        return _error('invalid_target', 'Between 0.5 and 300 km.')
    try:
        cands = fit.loop_from(start, target, sport, _router(), rotation=rotation)
    except RouterError as exc:
        return _engine_error(exc)
    if not cands and not _engine_ok():
        return _error('engine_unreachable', 'The routing engine is not running.', 503)
    known = routelib.known_ground(g.user.id, sport)
    for c in cands:
        c['known_pct'] = round(100 * known.share(plan.flatten(c['plan']))) if known else None
    return jsonify({'target_m': target, 'candidates': [_candidate(c) for c in cands]})
