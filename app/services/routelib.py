"""The route library: saved routes, which activities followed them, and routes worth saving.

Each route's original is a GeoJSON file, users/<id>/routes/<uuid>.geojson: the line, and the
planner's segment chain (routing/plan.py) so it can be edited again. The `routes` table
indexes those files and reindex_routes() rebuilds it from them, the same arrangement as FIT
files and the activity index.

Matching works on SessionShape - every GPS activity resampled every 25 m - which is filled in
lazily from the samples (ensure_shapes) rather than at import. So nothing here depends on the
job worker running new code, and a re-index (which drops shapes with their sessions) simply
gets recomputed the next time the Routes tab is opened.
"""
import json
import os
import threading
import time
import uuid as uuidlib
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..models import (FitFile, Record, Route, RouteMatch, RouteSuggestionHidden, Session, SessionShape, db,
                      utcnow)
from ..routing import fit, geo, plan
from ..routing.brouter import FAMILY, FAMILY_SPORTS, SPORT_LABELS, sport_for_session
from ..settings import user_dir

SHAPE_STEP_M = 25.0
MATCH_RADIUS_M = 35.0       # GPS under trees and a routed line along the same path differ by 10-20 m
MATCH_SHARE = 0.9           # both ways: the activity stays on the route, and covers all of it
SUGGEST_SHARE = 0.85
SUGGEST_MIN_RUNS = 3
MAX_PLAN_POINTS = 40000


# --------------------------------------------------------------------------- files

def routes_dir(home: Path, user_id: int) -> Path:
    path = user_dir(home, user_id) / 'routes'
    path.mkdir(parents=True, exist_ok=True)
    return path


def route_path(home: Path, route: Route) -> Path:
    return routes_dir(home, route.user_id) / f'{route.uuid}.geojson'


def read_feature(home: Path, route: Route) -> dict:
    path = route_path(home, route)
    if not path.exists():
        return {'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': []},
                'properties': {'segments': []}}
    return json.loads(path.read_text(encoding='utf-8'))


def load_plan(home: Path, route: Route) -> list:
    return read_feature(home, route)['properties'].get('segments') or []


def _write(path: Path, feature: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(feature, separators=(',', ':')), encoding='utf-8')
    os.replace(tmp, path)


def validate_plan(raw) -> list:
    """A plan from the browser: a list of segments of numeric coordinates, nothing else."""
    if not isinstance(raw, list) or not raw:
        raise ValueError('plan must be a non-empty list of segments')
    out, points = [], 0
    for s in raw:
        if not isinstance(s, dict) or s.get('kind') not in ('track', 'routed', 'line'):
            raise ValueError('bad segment')
        coords = []
        for c in s.get('coords') or []:
            if not isinstance(c, (list, tuple)) or len(c) < 2:
                raise ValueError('bad coordinate')
            lon, lat = float(c[0]), float(c[1])
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError('coordinate out of range')
            ele = float(c[2]) if len(c) > 2 and c[2] is not None else None
            coords.append([round(lon, 6), round(lat, 6), round(ele, 1) if ele is not None else None])
        points += len(coords)
        if points > MAX_PLAN_POINTS:
            raise ValueError('plan too long')
        seg = {'kind': s['kind'], 'coords': coords}
        if s.get('sport') in SPORT_LABELS:
            seg['sport'] = s['sport']
        for key in ('unpaved_m', 'busy_m'):
            if isinstance(s.get(key), (int, float)):
                seg[key] = round(float(s[key]))
        if isinstance(s.get('from_route'), str):
            seg['from_route'] = s['from_route'][:200]
        out.append(seg)
    return out


def _kind(coords) -> str:
    if fit.is_out_and_back(coords):
        return 'out_back'
    return 'loop' if geo.is_loop(coords) else 'a_b'


def _apply_geometry(route: Route, segs: list) -> list:
    coords = plan.flatten(segs)
    if len(coords) < 2:
        raise ValueError('a route needs at least two points')
    st = plan.stats(segs)
    route.distance_m, route.ascent_m, route.unpaved_pct = st['distance_m'], st['ascent_m'], st['unpaved_pct']
    route.kind = _kind(coords)
    route.start_lon, route.start_lat = coords[0][0], coords[0][1]
    route.min_lon, route.min_lat, route.max_lon, route.max_lat = geo.bbox(coords)
    return coords


def save(home: Path, user_id: int, data: dict, route: Route | None = None) -> Route:
    """Create a route, or change one: name, sport, notes, starred, parent, and/or its plan."""
    creating = route is None
    if creating:
        route = Route(user_id=user_id, uuid=uuidlib.uuid4().hex, source=data.get('source') or 'planned',
                      created_at=utcnow())
    if 'name' in data or creating:
        route.name = (str(data.get('name') or '').strip() or 'Untitled route')[:200]
    if 'sport' in data or creating:
        if data.get('sport') not in SPORT_LABELS:
            raise ValueError('unknown sport')
        route.sport = data['sport']
    if 'notes' in data:
        route.notes = (str(data.get('notes') or '').strip() or None)
        route.notes = route.notes[:5000] if route.notes else None
    if 'starred' in data:
        route.starred = bool(data['starred'])
    if 'parent_id' in data:
        parent = (Route.query.filter_by(id=data['parent_id'], user_id=user_id).first()
                  if data.get('parent_id') else None)
        route.parent_id = parent.id if parent and parent.id != route.id else None
    for key in ('source_file_id', 'source_idx'):
        if key in data:
            setattr(route, key, data[key])

    feature = read_feature(home, route) if not creating else None
    segs = validate_plan(data['plan']) if 'plan' in data else (feature['properties'].get('segments') or [])
    coords = _apply_geometry(route, segs)
    route.updated_at = utcnow()
    db.session.add(route)
    db.session.flush()

    parent = db.session.get(Route, route.parent_id) if route.parent_id else None
    source_file = db.session.get(FitFile, route.source_file_id) if route.source_file_id else None
    _write(route_path(home, route), {
        'type': 'Feature',
        'geometry': {'type': 'LineString', 'coordinates': coords},
        'properties': {
            'fitmon_route': 1, 'uuid': route.uuid, 'name': route.name, 'sport': route.sport,
            'notes': route.notes, 'starred': route.starred, 'source': route.source,
            'parent_uuid': parent.uuid if parent else None,
            # The FIT file by its hash, not its row id: ids are local to this index.
            'source_sha256': source_file.sha256 if source_file else None, 'source_idx': route.source_idx,
            'created_at': route.created_at.isoformat(timespec='seconds') if route.created_at else None,
            'updated_at': route.updated_at.isoformat(timespec='seconds'),
            'segments': segs,
        },
    })
    db.session.commit()
    if 'plan' in data or creating:
        match_route(route, coords)
    return route


def delete(home: Path, route: Route) -> None:
    path = route_path(home, route)
    Route.query.filter_by(parent_id=route.id).update({'parent_id': route.parent_id}, synchronize_session=False)
    db.session.delete(route)
    db.session.commit()
    path.unlink(missing_ok=True)


def reindex_routes(home: Path, user_id: int) -> int:
    """Rebuild this person's route rows from their files (after the DB was lost or rebuilt)."""
    files = sorted(routes_dir(home, user_id).glob('*.geojson'))
    features = []
    for path in files:
        try:
            feature = json.loads(path.read_text(encoding='utf-8'))
            features.append((path.stem, feature['properties']))
        except (ValueError, KeyError):
            continue
    Route.query.filter_by(user_id=user_id).delete()
    db.session.flush()
    by_uuid = {}
    for uid, props in features:
        route = Route(user_id=user_id, uuid=uid, name=(props.get('name') or 'Untitled route')[:200],
                      sport=props.get('sport') if props.get('sport') in SPORT_LABELS else 'run',
                      notes=props.get('notes'), starred=bool(props.get('starred')),
                      source=props.get('source'), source_idx=props.get('source_idx'))
        for key in ('created_at', 'updated_at'):
            try:
                setattr(route, key, datetime.fromisoformat(props.get(key)) if props.get(key) else utcnow())
            except ValueError:
                setattr(route, key, utcnow())
        if props.get('source_sha256'):
            f = FitFile.query.filter_by(user_id=user_id, sha256=props['source_sha256']).first()
            route.source_file_id = f.id if f else None
        try:
            _apply_geometry(route, props.get('segments') or [])
        except ValueError:
            continue
        db.session.add(route)
        by_uuid[uid] = (route, props.get('parent_uuid'))
    db.session.flush()
    for route, parent_uuid in by_uuid.values():
        if parent_uuid in by_uuid:
            route.parent_id = by_uuid[parent_uuid][0].id
    db.session.commit()
    SessionShape.query.filter_by(user_id=user_id).update({'matched': False}, synchronize_session=False)
    db.session.commit()
    return len(by_uuid)


# --------------------------------------------------------------------------- from an activity

def session_coords(session: Session) -> list:
    rows = (db.session.query(Record.lon, Record.lat, Record.alt)
            .filter(Record.session_id == session.id, Record.lat.isnot(None), Record.lon.isnot(None))
            .order_by(Record.idx).all())
    return [[round(r[0], 6), round(r[1], 6), round(r[2], 1) if r[2] is not None else None] for r in rows]


def plan_from_session(session: Session) -> list:
    """The activity's GPS line as one 'track' segment, thinned to what still draws the same."""
    # A trimmed activity's samples already stop at the trim (the importer drops the tail).
    coords = session_coords(session)
    coords = [c for i, c in enumerate(coords) if i == 0 or geo.dist(coords[i - 1], c) > 0.5]
    if len(coords) < 2:
        raise ValueError('this activity has no GPS track')
    return plan.from_track(geo.simplify(coords, 3.0))


def default_name(session: Session) -> str:
    when = session.start_time.strftime('%d %b %Y') if session.start_time else ''
    km = f'{(session.distance_m or 0) / 1000:.1f} km'
    return session.name or f'{SPORT_LABELS.get(sport_for_session(session.sport, session.sub_sport), "Route")} {km} ({when})'


# --------------------------------------------------------------------------- shapes

def _shape_for(session: Session) -> SessionShape:
    coords = [c[:2] for c in session_coords(session)]
    rsport = sport_for_session(session.sport, session.sub_sport)
    shape = SessionShape(session_id=session.id, user_id=session.user_id, file_id=session.file_id,
                         idx=session.idx, family=FAMILY.get(rsport) if rsport else None)
    pts = geo.resample(coords, SHAPE_STEP_M) if len(coords) >= 2 else []
    pts = [[round(p[0], 5), round(p[1], 5)] for p in pts]
    shape.coords = json.dumps(pts, separators=(',', ':'))
    shape.distance_m = geo.length(pts) if len(pts) >= 2 else 0.0
    if pts:
        shape.start_lon, shape.start_lat = pts[0]
        shape.min_lon, shape.min_lat, shape.max_lon, shape.max_lat = geo.bbox(pts)
    return shape


def ensure_shapes(user_id: int, budget_s: float = 3.0) -> int:
    """Compute missing shapes for GPS activities, for at most budget_s seconds. Returns how many
    are still missing, so the caller can say 'indexing N more' and ask again."""
    have = db.session.query(SessionShape.session_id).filter(SessionShape.user_id == user_id)
    missing = (Session.query.filter(Session.user_id == user_id, Session.has_gps.is_(True),
                                    Session.id.notin_(have))
               .order_by(Session.start_time.desc()).all())
    started = time.monotonic()
    done = 0
    for s in missing:
        if time.monotonic() - started > budget_s:
            break
        db.session.add(_shape_for(s))
        done += 1
        if done % 25 == 0:
            db.session.commit()
    db.session.commit()
    return len(missing) - done


def _shape_points(shape: SessionShape) -> list:
    return json.loads(shape.coords or '[]')


# --------------------------------------------------------------------------- matching

def _overlaps(shape: SessionShape, route: Route, pad_deg: float = 0.003) -> bool:
    if shape.min_lat is None or route.min_lat is None:
        return False
    return not (shape.max_lat < route.min_lat - pad_deg or shape.min_lat > route.max_lat + pad_deg or
                shape.max_lon < route.min_lon - pad_deg or shape.min_lon > route.max_lon + pad_deg)


def compare(points, route_points, route_index=None):
    """(share of the activity on the route, share of the route covered, direction)."""
    route_index = route_index or geo.GridIndex(route_points, cell=MATCH_RADIUS_M)
    on = geo.coverage(points, route_index, MATCH_RADIUS_M)
    covered = geo.coverage(route_points, geo.GridIndex(points, cell=MATCH_RADIUS_M), MATCH_RADIUS_M)
    # Direction: does the nearest route point move forwards or backwards as the activity goes on?
    n = len(route_points)
    steps = []
    last = None
    for p in points[::4]:
        j, _ = route_index.nearest(p, MATCH_RADIUS_M)
        if j is None:
            continue
        if last is not None and j != last:
            delta = (j - last) % n
            steps.append(1 if delta < n / 2 else -1)
        last = j
    return on, covered, ('rev' if sum(steps) < 0 else 'fwd')


def _family(route: Route) -> str:
    return FAMILY.get(route.sport, 'foot')


def _candidate_shapes(route: Route):
    q = SessionShape.query.filter(SessionShape.user_id == route.user_id, SessionShape.family == _family(route))
    if route.distance_m:
        q = q.filter(SessionShape.distance_m >= 0.8 * route.distance_m,
                     SessionShape.distance_m <= 1.25 * route.distance_m)
    return [s for s in q.all() if _overlaps(s, route)]


def match_route(route: Route, coords=None, home: Path | None = None) -> int:
    """Recompute which activities followed this route."""
    if coords is None:
        from flask import current_app
        coords = plan.flatten(load_plan(home or current_app.config['FITMON_HOME'], route))
    pts = geo.resample(coords, SHAPE_STEP_M)
    index = geo.GridIndex(pts, cell=MATCH_RADIUS_M)
    RouteMatch.query.filter_by(route_id=route.id).delete()
    n = 0
    for shape in _candidate_shapes(route):
        on, covered, direction = compare(_shape_points(shape), pts, index)
        if on >= MATCH_SHARE and covered >= MATCH_SHARE:
            db.session.merge(RouteMatch(route_id=route.id, file_id=shape.file_id, idx=shape.idx,
                                        user_id=route.user_id, direction=direction,
                                        match_pct=round(100 * min(on, covered), 1)))
            n += 1
    db.session.commit()
    return n


def match_new_shapes(home: Path, user_id: int) -> int:
    """Compare activities not yet compared with this person's routes. Cheap when nothing is new."""
    fresh = SessionShape.query.filter_by(user_id=user_id, matched=False).all()
    if not fresh:
        return 0
    routes = Route.query.filter_by(user_id=user_id).all()
    found = 0
    if routes:
        prepared = []
        for r in routes:
            pts = geo.resample(plan.flatten(load_plan(home, r)), SHAPE_STEP_M)
            if len(pts) >= 2:
                prepared.append((r, pts, geo.GridIndex(pts, cell=MATCH_RADIUS_M)))
        for shape in fresh:
            points = None
            for r, pts, index in prepared:
                if shape.family != _family(r) or not _overlaps(shape, r):
                    continue
                if r.distance_m and not (0.8 * r.distance_m <= (shape.distance_m or 0) <= 1.25 * r.distance_m):
                    continue
                points = points or _shape_points(shape)
                on, covered, direction = compare(points, pts, index)
                if on >= MATCH_SHARE and covered >= MATCH_SHARE:
                    db.session.merge(RouteMatch(route_id=r.id, file_id=shape.file_id, idx=shape.idx,
                                                user_id=user_id, direction=direction,
                                                match_pct=round(100 * min(on, covered), 1)))
                    found += 1
    for shape in fresh:
        shape.matched = True
    db.session.commit()
    return found


def refresh(home: Path, user_id: int, budget_s: float = 3.0) -> int:
    """Bring shapes and matches up to date as far as the time budget allows; returns how many
    activities still wait to be indexed."""
    pending = ensure_shapes(user_id, budget_s)
    match_new_shapes(home, user_id)
    return pending


def history(route: Route) -> list:
    """The activities that followed a route, newest first, with what one compares on it."""
    rows = (db.session.query(RouteMatch, Session)
            .join(Session, db.and_(Session.file_id == RouteMatch.file_id, Session.idx == RouteMatch.idx))
            .filter(RouteMatch.route_id == route.id).order_by(Session.start_time.desc()).all())
    files = {f.id: f.utc_offset_s for f in FitFile.query.filter(
        FitFile.id.in_({s.file_id for _, s in rows})).all()} if rows else {}
    return [{'session_id': s.id, 'start_time': s.start_time, 'utc_offset_s': files.get(s.file_id),
             'sport': s.sport, 'sub_sport': s.sub_sport, 'name': s.name, 'distance_m': s.distance_m,
             'timer_s': s.timer_s, 'avg_speed': s.avg_speed, 'avg_hr': s.avg_hr, 'avg_power': s.avg_power,
             'efficiency': s.efficiency, 'direction': m.direction, 'match_pct': m.match_pct,
             'excluded': s.excluded}
            for m, s in rows]


def routes_for_session(session: Session) -> list:
    rows = (db.session.query(Route, RouteMatch).join(RouteMatch, RouteMatch.route_id == Route.id)
            .filter(RouteMatch.file_id == session.file_id, RouteMatch.idx == session.idx,
                    Route.user_id == session.user_id).all())
    return [{'id': r.id, 'name': r.name, 'direction': m.direction} for r, m in rows]


# --------------------------------------------------------------------------- suggestions

def suggestions(user_id: int) -> list:
    """Groups of >= 3 activities over the same ground that no saved route covers yet."""
    on_routes = {(m.file_id, m.idx) for m in RouteMatch.query.filter_by(user_id=user_id).all()}
    hidden = {h.key for h in RouteSuggestionHidden.query.filter_by(user_id=user_id).all()}
    shapes = (db.session.query(SessionShape, Session.start_time, Session.sport, Session.sub_sport)
              .join(Session, Session.id == SessionShape.session_id)
              .filter(SessionShape.user_id == user_id, SessionShape.family.isnot(None),
                      SessionShape.distance_m >= 1000, Session.excluded.is_(False))
              .order_by(Session.start_time.desc()).all())
    clusters = []
    for shape, start, sport, sub in shapes:
        if (shape.file_id, shape.idx) in on_routes:
            continue
        pts = None
        for c in clusters:
            rep = c['rep']
            if rep.family != shape.family or abs(rep.distance_m - shape.distance_m) > 0.12 * rep.distance_m:
                continue
            if geo.dist([rep.start_lon, rep.start_lat], [shape.start_lon, shape.start_lat]) > 400:
                continue
            pts = pts or _shape_points(shape)
            on, covered, _ = compare(pts, c['pts'], c['index'])
            if on >= SUGGEST_SHARE and covered >= SUGGEST_SHARE:
                c['members'].append((shape, start))
                break
        else:
            pts = pts or _shape_points(shape)
            if len(pts) >= 2:
                clusters.append({'rep': shape, 'pts': pts, 'index': geo.GridIndex(pts, cell=MATCH_RADIUS_M),
                                 'members': [(shape, start)], 'sport': sport_for_session(sport, sub)})
    out = []
    for c in clusters:
        rep = c['rep']
        key = f'{rep.file_id}:{rep.idx}'
        if len(c['members']) < SUGGEST_MIN_RUNS or key in hidden:
            continue
        out.append({'key': key, 'session_id': rep.session_id, 'sport': c['sport'], 'count': len(c['members']),
                    'distance_m': round(rep.distance_m), 'last': c['members'][0][1],
                    'first': c['members'][-1][1], 'kind': _kind(c['pts']),
                    'preview': geo.simplify(c['pts'], 20.0)})
    out.sort(key=lambda s: -s['count'])
    return out[:12]


def hide_suggestion(user_id: int, key: str) -> None:
    db.session.merge(RouteSuggestionHidden(user_id=user_id, key=key[:64]))
    db.session.commit()


# --------------------------------------------------------------------------- known ground

class KnownGround:
    """Cells of about 25 m that this person's own GPS tracks of one family pass through."""
    CELL_LAT = 0.000225
    CELL_LON = 0.00035

    def __init__(self, shapes):
        self.cells = set()
        for coords in shapes:
            for a, b in zip(coords, coords[1:]):
                self.cells.add(self._key(a))
                # Shapes are 25 m apart; fill the middle so a cell is never skipped between them.
                self.cells.add(self._key([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]))
            if coords:
                self.cells.add(self._key(coords[-1]))

    def _key(self, c):
        return (int(c[1] // self.CELL_LAT), int(c[0] // self.CELL_LON))

    def near(self, c) -> bool:
        i, j = self._key(c)
        return any((i + di, j + dj) in self.cells for di in (-1, 0, 1) for dj in (-1, 0, 1))

    def share(self, coords) -> float:
        pts = geo.resample(coords, 25.0)
        return sum(1 for p in pts if self.near(p)) / len(pts) if pts else 0.0


_known_cache: dict = {}
_known_lock = threading.Lock()


def known_ground(user_id: int, sport: str) -> KnownGround | None:
    family = FAMILY.get(sport, 'foot')
    q = SessionShape.query.filter_by(user_id=user_id, family=family)
    stamp = (q.count(), db.session.query(db.func.max(SessionShape.session_id))
             .filter_by(user_id=user_id, family=family).scalar())
    if not stamp[0]:
        return None
    key = (user_id, family)
    with _known_lock:
        hit = _known_cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    known = KnownGround(_shape_points(s) for s in q.all())
    with _known_lock:
        _known_cache[key] = (stamp, known)
    return known


def loops_for(home: Path, user_id: int, sport: str, exclude_id: int | None = None) -> list:
    """(name, coords) of this person's other loop routes of the same family, for laps."""
    family = FAMILY.get(sport, 'foot')
    out = []
    for r in Route.query.filter_by(user_id=user_id, kind='loop').all():
        if r.id == exclude_id or FAMILY.get(r.sport) != family or (r.distance_m or 0) > 8000:
            continue
        out.append((r.name, plan.flatten(load_plan(home, r))))
    return out[:20]


# --------------------------------------------------------------------------- export

def gpx(route: Route, coords) -> str:
    """GPX 1.1 with the route as a track: what Garmin Connect, Komoot and the rest import."""
    pts = []
    for c in coords:
        ele = f'<ele>{c[2]:.1f}</ele>' if len(c) > 2 and c[2] is not None else ''
        pts.append(f'<trkpt lat="{c[1]:.6f}" lon="{c[0]:.6f}">{ele}</trkpt>')
    name = escape(route.name or 'route')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="fitmon" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'<metadata><name>{name}</name></metadata>\n'
            f'<trk><name>{name}</name><type>{escape(route.sport)}</type><trkseg>\n'
            + '\n'.join(pts) + '\n</trkseg></trk>\n</gpx>\n')


def safe_filename(name: str) -> str:
    return ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in (name or '')).strip('-')[:60] or 'route'


def family_sports(sport: str) -> tuple:
    return FAMILY_SPORTS[FAMILY.get(sport, 'foot')]
