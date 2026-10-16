"""Route library and planner. The routing engine is replaced by StraightRouter (straight lines
between the points), so these run anywhere; the engine client itself is tested against a
canned BRouter reply."""
import json
import xml.etree.ElementTree as ET

import pytest

from app.routing import brouter, fit, geo, plan

from .conftest import SAMPLE_POWER, SAMPLE_RUN

ROUTER = brouter.StraightRouter()


def square(side_m=500.0, lon=8.6670, lat=49.9937, step_m=25.0):
    """A closed square loop, as a recorded track would be: a point every few metres."""
    corners = [[lon, lat]]
    for angle in (0.0, 1.5708, 3.1416, 4.7124):
        corners.append(geo.offset(corners[-1], angle, side_m))
    corners[-1] = [lon, lat]
    pts = []
    for a, b in zip(corners, corners[1:]):
        n = int(side_m // step_m)
        pts += [geo.interpolate(a, b, k / n) + [100.0] for k in range(n)]
    return pts + [[lon, lat, 100.0]]


# --------------------------------------------------------------------------- geometry

def test_geometry_basics():
    a, b = [8.0, 50.0], [8.0, 50.001]
    assert geo.dist(a, b) == pytest.approx(111.2, abs=0.5)
    loop = square()
    assert geo.length(loop) == pytest.approx(2000, rel=0.01)
    assert geo.is_loop(loop)
    pts = geo.resample(loop, 100)
    assert len(pts) == pytest.approx(21, abs=1)
    assert len(geo.simplify(loop, 2.0)) <= 6          # a square is its corners
    index = geo.GridIndex(loop, cell=30)
    assert geo.coverage(geo.resample(loop, 50), index, 20) == 1.0
    assert geo.coverage([geo.offset(loop[0], 0.0, 900)], index, 30) == 0.0


# --------------------------------------------------------------------------- plan edits

def test_plan_edits_keep_the_line_continuous():
    segs = plan.from_track(square())
    total = plan.total(segs)
    # Drag the middle of the second (northward) side 300 m outwards, to the east. A recorded
    # track is re-routed 300 m either side of the grab: from 450 m to 1050 m.
    mid = plan.point(segs, 750)
    dragged = plan.drag(segs, 750, geo.offset(mid, 0.0, 300), 'run', ROUTER)
    assert plan.total(dragged) == pytest.approx(total - 600 + 2 * (300 ** 2 + 300 ** 2) ** 0.5, abs=20)
    coords = plan.flatten(dragged)
    assert all(geo.dist(a, b) < 101 for a, b in zip(coords, coords[1:]))  # no jumps
    assert [s['kind'] for s in dragged] == ['track', 'routed', 'track']
    assert plan.flatten(dragged)[0] == plan.flatten(segs)[0]               # same start
    # Delete the new control between the detour and the rest: still one continuous loop.
    undone = plan.delete_control(dragged, 1, 'run', ROUTER)
    assert geo.is_loop(plan.flatten(undone))
    rev = plan.reverse(dragged)
    assert plan.flatten(rev)[0] == plan.flatten(dragged)[-1]
    assert plan.total(plan.truncate(segs, 700)) == pytest.approx(700, abs=1)


def test_drawing_a_route_from_nothing():
    segs = plan.append([], [8.66, 49.99], 'run', ROUTER)
    segs = plan.append(segs, [8.67, 49.99], 'run', ROUTER)
    segs = plan.append(segs, [8.67, 50.0], 'run', ROUTER)
    assert len(segs) == 2
    closed = plan.close_loop(segs, 'run', ROUTER)
    assert geo.is_loop(plan.flatten(closed), within=5)
    st = plan.stats(closed)
    assert st['loop'] and st['distance_m'] > 2500


# --------------------------------------------------------------------------- fitting the length

@pytest.mark.parametrize('target', [2600, 3200])
def test_lengthening_suggestions_hit_the_target(target):
    segs = plan.from_track(square())
    cands = fit.fit_length(segs, target, 'run', ROUTER, tolerance=0.02)
    assert cands, 'no suggestions'
    best = cands[0]
    assert abs(best['stats']['distance_m'] - target) <= 0.02 * target
    assert plan.flatten(best['plan'])[0] == plan.flatten(segs)[0]
    assert len({c['label'] for c in cands}) == len(cands)
    assert all(c['label'] for c in cands)


def test_shortening_suggestions_hit_the_target():
    segs = plan.from_track(square(side_m=1000))
    cands = fit.fit_length(segs, 3300, 'run', ROUTER, tolerance=0.03)
    assert cands
    assert abs(cands[0]['stats']['distance_m'] - 3300) <= 0.03 * 3300
    assert geo.is_loop(plan.flatten(cands[0]['plan']))


def test_out_and_back_turns_round_earlier():
    out = [geo.offset([8.66, 49.99], 0.0, d) + [100.0] for d in range(0, 2001, 20)]
    segs = plan.from_track(out + out[::-1][1:])
    assert fit.is_out_and_back(plan.flatten(segs))
    cands = fit.fit_length(segs, 3000, 'run', ROUTER)
    turn = [c for c in cands if c['method'] == 'turn_earlier']
    assert turn and turn[0]['stats']['distance_m'] == pytest.approx(3000, abs=5)


def test_known_ground_ranks_familiar_suggestions_first():
    segs = plan.from_track(square())

    class Known:
        def share(self, coords):         # pretend only ground south of the start is familiar
            return sum(1 for c in coords if c[1] < 49.9937) / len(coords)

    cands = fit.fit_length(segs, 2800, 'run', ROUTER, known=Known(), prefer='known')
    within = [c for c in cands if abs(c['off_m']) <= 56]
    assert within and within == sorted(within, key=lambda c: -c['known_pct'])


def test_new_loop_from_a_start_point():
    cands = fit.loop_from([8.667, 49.994], 5000, 'run', ROUTER)
    assert len(cands) == 3
    for c in cands:
        coords = plan.flatten(c['plan'])
        assert geo.is_loop(coords, within=5)
        assert abs(c['stats']['distance_m'] - 5000) <= 100


# --------------------------------------------------------------------------- the engine client

def test_brouter_reply_is_read(monkeypatch):
    reply = {'features': [{'geometry': {'coordinates': [[8.666, 49.993, 127.0], [8.667, 49.994, 128.0]]},
                           'properties': {'track-length': '140', 'messages': [
                               ['Longitude', 'Latitude', 'Elevation', 'Distance', 'CostPerKm', 'WayTags'],
                               ['8666418', '49991789', '128', '100', '1320', 'highway=track surface=gravel'],
                               ['8666415', '49991781', '128', '40', '1320', 'highway=secondary surface=asphalt']]}}]}

    class Resp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []
    monkeypatch.setattr(brouter.urllib.request, 'urlopen',
                        lambda url, timeout: seen.append(url) or Resp(json.dumps(reply).encode()))
    leg = brouter.BRouter('http://engine:1').route([[8.666, 49.993], [8.667, 49.994]], 'gravel')
    assert leg.distance_m == 140 and leg.unpaved_m == 100 and leg.busy_m == 40
    assert 'profile=gravel' in seen[0] and 'lonlats=8.666000,49.993000|8.667000,49.994000' in seen[0]
    brouter.BRouter('http://engine:1')  # a fresh client has its own cache
    assert len(seen) == 1


def test_brouter_unreachable_is_a_router_error():
    with pytest.raises(brouter.RouterError, match='unreachable'):
        brouter.BRouter('http://127.0.0.1:9', timeout=1).route([[8.0, 50.0], [8.01, 50.0]], 'run')


# --------------------------------------------------------------------------- the library over the API

@pytest.fixture
def runner(app, alice, import_file):
    app.config['FITMON_ROUTER'] = ROUTER
    res = import_file(alice.user_id, SAMPLE_RUN)
    from app.models import Session
    with app.app_context():
        sid = Session.query.filter_by(file_id=res['file_id']).first().id
    alice.login('alice')
    return sid


def test_save_an_activity_as_a_route_and_see_it_matched(app, alice, runner, home):
    r = alice.post(f'/api/routes/from-session/{runner}', json={'name': 'Fenix loop'})
    assert r.status_code == 200, r.get_json()
    route = r.get_json()['route']
    assert route['sport'] == 'run' and route['source'] == 'activity'

    listing = alice.get('/api/routes').get_json()
    assert listing['pending'] == 0
    assert [x['name'] for x in listing['routes']] == ['Fenix loop']
    assert listing['routes'][0]['times'] == 1          # the activity it came from follows it

    detail = alice.get(f"/api/routes/{route['id']}").get_json()
    assert detail['history'][0]['session_id'] == runner
    assert detail['source_activity']['session_id'] == runner
    assert detail['stats']['distance_m'] == pytest.approx(route['distance_m'])
    assert alice.get(f'/api/sessions/{runner}/routes').get_json()['routes'][0]['name'] == 'Fenix loop'

    gpx = alice.get(f"/api/routes/{route['id']}/gpx")
    assert gpx.mimetype == 'application/gpx+xml'
    pts = ET.fromstring(gpx.data).findall('.//{http://www.topografix.com/GPX/1/1}trkpt')
    assert len(pts) == len(plan.flatten(detail['plan']))

    # The file on disk is the original: rebuild the table from it.
    from app.models import Route
    from app.services import routelib
    with app.app_context():
        assert routelib.reindex_routes(home, alice.user_id) == 1
        again = Route.query.filter_by(user_id=alice.user_id).one()
        assert again.name == 'Fenix loop' and again.source_file_id is not None


def test_a_variant_is_saved_under_its_parent(app, alice, runner):
    parent = alice.post(f'/api/routes/from-session/{runner}').get_json()['route']
    segs = alice.get(f"/api/routes/{parent['id']}").get_json()['plan']
    total = plan.total(segs)
    cands = alice.post('/api/plan/fit', json={'plan': segs, 'target_m': total * 1.2, 'sport': 'run'}).get_json()
    assert cands['candidates'], cands
    variant = alice.post('/api/routes', json={'name': 'longer', 'sport': 'run', 'parent_id': parent['id'],
                                              'plan': cands['candidates'][0]['plan']}).get_json()['route']
    assert variant['parent_id'] == parent['id']
    assert alice.get(f"/api/routes/{parent['id']}").get_json()['variants'][0]['id'] == variant['id']
    # Deleting the parent keeps the variant, now at the top level.
    assert alice.post(f"/api/routes/{parent['id']}/delete").status_code == 200
    assert alice.get(f"/api/routes/{variant['id']}").get_json()['route']['parent_id'] is None


def test_plan_edit_api(alice, runner, app):
    p = alice.post('/api/plan/edit', json={'op': 'append', 'to': [8.66, 49.99], 'sport': 'run'}).get_json()
    p = alice.post('/api/plan/edit', json={'op': 'append', 'to': [8.67, 49.99], 'plan': p['plan']}).get_json()
    assert p['stats']['distance_m'] == pytest.approx(716, abs=5)
    p2 = alice.post('/api/plan/edit', json={'op': 'drag', 'd': 300, 'to': [8.665, 49.995], 'plan': p['plan']}).get_json()
    assert p2['stats']['distance_m'] > p['stats']['distance_m']
    bad = alice.post('/api/plan/edit', json={'op': 'append', 'to': [500, 1], 'plan': p['plan']})
    assert bad.status_code == 400
    assert alice.post('/api/plan/edit', json={'op': 'append', 'to': [8.6, 49.9],
                                              'plan': [{'kind': 'evil', 'coords': []}]}).status_code == 400
    loops = alice.post('/api/plan/loop', json={'start': [8.667, 49.994], 'target_m': 6000, 'sport': 'run'}).get_json()
    assert len(loops['candidates']) == 3


def test_engine_down_is_reported(alice, runner, app):
    app.config['FITMON_ROUTER'] = brouter.BRouter('http://127.0.0.1:9', timeout=1)
    r = alice.post('/api/plan/edit', json={'op': 'append', 'to': [8.67, 49.99],
                                           'plan': plan.append([], [8.66, 49.99], 'run', ROUTER)})
    assert r.status_code == 503 and r.get_json()['error'] == 'engine_unreachable'
    assert alice.get('/api/plan/status').get_json()['engine'] is False


def test_activities_without_gps_cannot_become_routes(app, alice, import_file):
    from app.models import Session
    res = import_file(alice.user_id, SAMPLE_POWER)
    with app.app_context():
        sid = Session.query.filter_by(file_id=res['file_id']).first().id
    alice.login('alice')
    assert alice.post(f'/api/routes/from-session/{sid}').get_json()['error'] == 'no_gps'


def test_repeated_activities_are_suggested(app, alice, home):
    """Activities over the same ground and no route for them: offer one. Twice is enough."""
    from datetime import datetime, timedelta
    from app.models import FitFile, Session, SessionShape, db
    from app.services import routelib
    loop = [c[:2] for c in square()]
    # The fourth is the same loop plus a 100 m out-and-back spur, 10 % longer: same ground, a
    # different route, so it must not be counted with the others (an 11 km run was once offered
    # as "11 km, 5 times" that way, grouped with a 9.9 km loop).
    spur = [geo.offset(loop[0], -1.5708, d) for d in range(0, 101, 25)]
    longer = loop + spur + spur[::-1]
    # The fifth and sixth: another loop 3 km away, done twice. Twice is enough for a route.
    elsewhere = [[c[0] + 0.04, c[1]] for c in loop]
    lines = [loop, loop, loop, longer, elsewhere, elsewhere]
    with app.app_context():
        for k in range(6):
            f = FitFile(user_id=alice.user_id, sha256=f'{k:064d}', original_name=f'{k}.fit')
            db.session.add(f)
            db.session.flush()
            s = Session(file_id=f.id, user_id=alice.user_id, idx=0, sport='running', has_gps=True,
                        start_time=datetime(2026, 5, 1) + timedelta(days=k if k < 3 else -1), distance_m=2000)
            db.session.add(s)
            db.session.flush()
            jitter = [[c[0] + 0.00005 * k, c[1]] for c in lines[k]]
            pts = geo.resample(jitter, 25)
            db.session.add(SessionShape(session_id=s.id, user_id=alice.user_id, file_id=f.id, idx=0, family='foot',
                                        distance_m=geo.length(pts), start_lon=pts[0][0], start_lat=pts[0][1],
                                        min_lon=geo.bbox(pts)[0], min_lat=geo.bbox(pts)[1],
                                        max_lon=geo.bbox(pts)[2], max_lat=geo.bbox(pts)[3],
                                        coords=json.dumps(pts), matched=True))
        db.session.commit()
        found = routelib.suggestions(alice.user_id)
        assert [x['count'] for x in found] == [3, 2]          # the longer variant, done once, is not
        assert found[0]['kind'] == 'loop'
        routelib.hide_suggestion(alice.user_id, found[0]['key'])
        assert [x['count'] for x in routelib.suggestions(alice.user_id)] == [2]
