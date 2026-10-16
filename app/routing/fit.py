"""Make a route a given length: the planner's suggestions.

Every method keeps the start and the finish and turns one knob - how far a new via point sits
from the route, how far a spur reaches, which two points a shortcut joins - until the length
comes within tolerance of the target. Lengthening: push a stretch outwards, add a lollipop at
the far end, add laps of a known loop. Shortening: pull a stretch inwards, take a shortcut,
turn back earlier (out-and-backs), finish earlier (A to B). See specs/route-planner.md 4.2-4.4.

The searches run in parallel against the engine, under one deadline: whatever is finished
when it passes is what the person gets.
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor, wait

from . import geo, plan
from .brouter import RouterError

MAX_SUGGESTIONS = 5


class Deadline:
    def __init__(self, seconds: float):
        self.at = time.monotonic() + seconds

    def passed(self) -> bool:
        return time.monotonic() > self.at


def _solve(f, x0, x1, lo, hi, target, tol_m, deadline, iters=6):
    """Secant search on a knob x for f(x) -> (segs, length) closest to target. f may return None."""
    best = None
    tried = []

    def run(x):
        nonlocal best
        x = max(lo, min(hi, x))
        if any(abs(x - t[0]) < 1e-6 for t in tried) or deadline.passed():
            return None
        try:
            out = f(x)
        except RouterError:
            out = None
        if out is None:
            return None
        tried.append((x, out[1]))
        if best is None or abs(out[1] - target) < abs(best[1] - target):
            best = out
        return out

    run(x0)
    run(x1)
    for _ in range(iters):
        # Aim well inside the tolerance while it is cheap; anything within it is accepted.
        if best is not None and abs(best[1] - target) <= tol_m / 3:
            break
        pts = sorted(tried, key=lambda t: abs(t[1] - target))[:2]
        if len(pts) < 2:
            if not pts:
                break
            x = pts[0][0] * (1.5 if pts[0][1] < target else 0.6) + 10
        else:
            (xa, la), (xb, lb) = pts
            below = [t for t in tried if t[1] < target]
            above = [t for t in tried if t[1] > target]
            if below and above:
                # Bracketed: interpolate between the closest pair on either side.
                (xa, la) = max(below, key=lambda t: t[1])
                (xb, lb) = min(above, key=lambda t: t[1])
            x = xb + (target - lb) * (xb - xa) / (lb - la) if lb != la else (xa + xb) / 2 + 25
        if run(x) is None and deadline.passed():
            break
    return best


def _outward(segs, d, centroid):
    """The perpendicular at d that points away from the route's middle."""
    coords = plan.flatten(segs)
    cum = geo.cumulative(coords)
    p, _ = geo.point_at(coords, cum, d)
    heading = geo.bearing_at(coords, cum, d)
    left, right = heading + math.pi / 2, heading - math.pi / 2
    if geo.dist(geo.offset(p, left, 100), centroid) >= geo.dist(geo.offset(p, right, 100), centroid):
        return p, left, right
    return p, right, left


def push(segs, d, target, sport, router, tol_m, deadline, inward=False):
    """Replace a stretch around d with a detour through a point moved out (or in) from it."""
    total = plan.total(segs)
    need = target - total
    w = max(250.0, min(1500.0, 0.06 * total)) if not inward else max(400.0, min(3000.0, 0.15 * total))
    lo_d, hi_d = max(0.0, d - w), min(total, d + w)
    if hi_d - lo_d < 200:
        return None
    centroid = geo.centroid(plan.flatten(segs))
    p, out_angle, in_angle = _outward(segs, d, centroid)
    angle = in_angle if inward else out_angle

    def f(x):
        new = plan.reroute(segs, lo_d, hi_d, [geo.offset(p, angle, x)], sport, router)
        return new, plan.total(new)

    if inward:
        x0, x1, lo, hi = min(w * 0.4, -need * 0.5), min(w * 0.8, -need), 0.0, w * 1.6
    else:
        x0, x1, lo, hi = need * 0.45, need * 0.75, 20.0, max(need * 1.5, 400.0)
    best = _solve(f, x0, x1, lo, hi, target, tol_m, deadline)
    if best is None:
        return None
    return {'method': 'pull_in' if inward else 'push_out', 'at_m': round(d), 'plan': best[0]}


def lollipop(segs, target, sport, router, tol_m, deadline, turn=0.0):
    """An out-and-back spur from the point of the route farthest from the start."""
    coords = plan.flatten(segs)
    cum = geo.cumulative(coords)
    start = coords[0]
    far = max(range(len(coords)), key=lambda i: geo.dist(start, coords[i]))
    d = cum[far]
    p = coords[far]
    proj = geo.Projection(start[1], start[0])
    (sx, sy), (px, py) = proj.xy(start), proj.xy(p)
    angle = math.atan2(py - sy, px - sx) + turn
    need = target - cum[-1]

    def f(x):
        leg = router.route([p, geo.offset(p, angle, x)], sport)
        out = {'kind': 'routed', 'sport': sport, 'coords': leg.coords,
               'unpaved_m': round(leg.unpaved_m), 'busy_m': round(leg.busy_m)}
        back = {**out, 'coords': [list(c) for c in reversed(leg.coords)]}
        new = plan.replace(segs, d, d, [out, back])
        return new, plan.total(new)

    best = _solve(f, need * 0.45, need * 0.6, 30.0, max(need, 300.0), target, tol_m, deadline)
    if best is None:
        return None
    return {'method': 'lollipop', 'at_m': round(d), 'plan': best[0], 'heading': _compass(angle)}


def known_loop(segs, target, sport, router, tol_m, loop):
    """Laps of another library route that passes close by, spliced in where it is nearest."""
    name, loop_coords = loop
    coords = plan.flatten(segs)
    total = plan.total(segs)
    need = target - total
    loop_len = geo.length(loop_coords)
    if loop_len < 200 or need < 0.5 * loop_len:
        return None
    index = geo.GridIndex(loop_coords, cell=50.0)
    cum = geo.cumulative(coords)
    best = None
    for d in range(0, int(total), 50):
        p, _ = geo.point_at(coords, cum, d)
        j, gap = index.nearest(p, 150.0)
        if j is not None and (best is None or gap < best[2]):
            best = (d, j, gap, p)
    if best is None:
        return None
    d, j, gap, p = best
    laps = max(1, round(need / loop_len))
    ring = loop_coords[j:] + loop_coords[1:j + 1]
    pieces = []
    q = ring[0]
    connector = None
    if gap > 15:
        try:
            connector = router.route([p, q], sport)
        except RouterError:
            return None
    if connector:
        pieces.append({'kind': 'routed', 'sport': sport, 'coords': connector.coords})
    for _ in range(laps):
        pieces.append({'kind': 'track', 'coords': [list(c) for c in ring], 'from_route': name})
    if connector:
        pieces.append({'kind': 'routed', 'sport': sport, 'coords': [list(c) for c in reversed(connector.coords)]})
    new = plan.replace(segs, d, d, pieces)
    if abs(plan.total(new) - target) > 3 * tol_m:
        return None
    return {'method': 'known_loop', 'at_m': d, 'plan': new, 'loop_name': name, 'laps': laps}


def shortcuts(segs, target, sport, router, tol_m, deadline, limit=5):
    """Join two points that are close as the crow flies but far apart along the route."""
    coords = plan.flatten(segs)
    cum = geo.cumulative(coords)
    total = cum[-1]
    saving = total - target
    step = 100.0
    marks = [(d, geo.point_at(coords, cum, d)[0]) for d in [k * step for k in range(int(total // step) + 1)]]
    pairs = []
    for i, (di, pi) in enumerate(marks):
        for dj, pj in marks[i + 1:]:
            along = dj - di
            if along < saving * 0.8:
                continue
            crow = geo.dist(pi, pj)
            if crow > 0.5 * along:
                continue
            est = along - 1.3 * crow
            if 0.7 * saving <= est <= 1.5 * saving:
                pairs.append((abs(est - saving), di, dj))
    pairs.sort()
    chosen = []
    for _, di, dj in pairs:
        if all(abs(di - c[0]) > 400 or abs(dj - c[1]) > 400 for c in chosen):
            chosen.append((di, dj))
        if len(chosen) >= limit:
            break
    out = []
    for di, dj in chosen:
        best = None
        for _ in range(3):
            if deadline.passed():
                break
            try:
                new = plan.reroute(segs, di, dj, [], sport, router)
            except RouterError:
                break
            length = plan.total(new)
            if best is None or abs(length - target) < abs(best[1] - target):
                best = (new, length)
            err = length - target
            if abs(err) <= tol_m:
                break
            # Too long: skip more of the route by finishing the shortcut later.
            dj = max(di + 200, min(total - 1, dj + err))
        if best:
            out.append({'method': 'shortcut', 'at_m': round(di), 'plan': best[0]})
    return out


def is_out_and_back(coords) -> bool:
    if len(coords) < 4 or not geo.is_loop(coords, 300):
        return False
    cum = geo.cumulative(coords)
    half = cum[-1] / 2
    _, i = geo.point_at(coords, cum, half)
    first, second = coords[:i + 1], coords[i + 1:]
    if len(second) < 2:
        return False
    index = geo.GridIndex(second, cell=40.0)
    return geo.coverage(geo.resample(first, 50.0), index, 40.0) >= 0.8


def turn_earlier(segs, target):
    """Out-and-back: turn round at half the target, and come back the same way."""
    out = plan.truncate(segs, target / 2)
    return {'method': 'turn_earlier', 'at_m': round(target / 2), 'plan': out + plan.reverse(out)}


def finish_earlier(segs, target):
    return {'method': 'finish_earlier', 'at_m': round(target), 'plan': plan.truncate(segs, target)}


LABELS = {
    'push_out': 'wider around the stretch at {km} km',
    'pull_in': 'tighter around the stretch at {km} km',
    'lollipop': 'out and back {heading} from the far end ({km} km)',
    'known_loop': '{laps}× your route “{loop}” at {km} km',
    'shortcut': 'shortcut from {km} km',
    'turn_earlier': 'turn round at {km} km',
    'finish_earlier': 'finish at {km} km',
}


def describe(c) -> str:
    return LABELS[c['method']].format(km=f"{c['at_m'] / 1000:.1f}", laps=c.get('laps', 1),
                                      loop=c.get('loop_name', ''), heading=c.get('heading', ''))


def _similar(a, b) -> bool:
    ca, cb = plan.flatten(a), plan.flatten(b)
    if abs(geo.length(ca) - geo.length(cb)) > 0.03 * max(geo.length(ca), 1):
        return False
    return geo.coverage(geo.resample(ca, 40.0), geo.GridIndex(cb, 30.0), 25.0) > 0.97


def fit_length(segs, target, sport, router, tolerance=0.02, known=None, prefer='known',
               loops=(), seconds=8.0, workers=4) -> list:
    """Up to five candidate plans near `target` metres, best first.

    known: optional object with .share(coords) -> 0..1, the part of a line on ground this person
    has covered before. prefer: 'known' ranks familiar ground first, 'new' the opposite.
    loops: (name, coords) of the person's other loop routes of this kind, for laps.
    """
    deadline = Deadline(seconds)
    total = plan.total(segs)
    coords = plan.flatten(segs)
    tol_m = max(tolerance * target, 50.0)
    if total < 100 or target < 200:
        return []
    jobs = []
    loop = geo.is_loop(coords)
    positions = [total * f for f in (0.2, 0.32, 0.44, 0.56, 0.68, 0.8)]
    if target > total:
        jobs += [lambda d=d: push(segs, d, target, sport, router, tol_m, deadline) for d in positions]
        if loop or is_out_and_back(coords):
            jobs += [lambda t=t: lollipop(segs, target, sport, router, tol_m, deadline, turn=t) for t in (0.0, 0.9, -0.9)]
        jobs += [lambda lp=lp: known_loop(segs, target, sport, router, tol_m, lp) for lp in loops]
    else:
        jobs += [lambda d=d: push(segs, d, target, sport, router, tol_m, deadline, inward=True)
                 for d in positions[1:5]]
        jobs.append(lambda: shortcuts(segs, target, sport, router, tol_m, deadline))
        if is_out_and_back(coords):
            jobs.append(lambda: turn_earlier(segs, target))
        elif not loop:
            jobs.append(lambda: finish_earlier(segs, target))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(j) for j in jobs]
        wait(futures, timeout=max(0.5, deadline.at - time.monotonic() + 2))
        for fut in futures:
            if not fut.done():
                continue
            try:
                r = fut.result()
            except RouterError:
                continue
            if r is None:
                continue
            results += r if isinstance(r, list) else [r]
    return rank(results, segs, target, tol_m, known, prefer)


def rank(results, segs, target, tol_m, known=None, prefer='known') -> list:
    base = plan.stats(segs)
    scored = []
    for c in results:
        if not c['plan']:
            continue
        st = plan.stats(c['plan'])
        c['stats'] = st
        c['off_m'] = round(st['distance_m'] - target)
        c['known_pct'] = round(100 * known.share(plan.flatten(c['plan']))) if known else None
        c['label'] = describe(c)
        familiar = (c['known_pct'] or 0) / 100
        score = (familiar if prefer != 'new' else 1 - familiar) if known else 0.0
        score -= max(0, st['ascent_m'] - base['ascent_m']) / 400
        score -= (st['busy_pct'] or 0) / 100
        if c['method'] == 'turn_earlier':
            score += 0.5            # on an out-and-back, the obvious change: no new ground at all
        within = abs(c['off_m']) <= tol_m
        scored.append((not within, abs(c['off_m']) // max(tol_m, 1) if not within else 0, -score, c))
    scored.sort(key=lambda t: t[:3])
    out = []
    for *_, c in scored:
        if any(_similar(c['plan'], o['plan']) for o in out):
            continue
        out.append(c)
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def loop_from(start, target, sport, router, rotation=0.0, seconds=8.0, count=3) -> list:
    """New loops from a start point: waypoints on a circle through the start, scaled to length."""
    deadline = Deadline(seconds)
    tol_m = max(0.02 * target, 50.0)

    def one(rot):
        def f(radius):
            centre = geo.offset(start, rot, radius)
            back = rot + math.pi
            pts = [start] + [geo.offset(centre, back + k * math.pi / 2, radius) for k in (1, 2, 3)] + [start]
            segs = [plan.routed_segment(router, pts, sport)]
            return segs, plan.total(segs)
        r0 = target / (2 * math.pi) / 1.25
        best = _solve(f, r0, r0 * 0.85, 50.0, r0 * 3, target, tol_m, deadline, iters=5)
        return {'method': 'loop', 'at_m': 0, 'plan': best[0], 'rotation': rot} if best else None

    rotations = [rotation + k * 2 * math.pi / count for k in range(count)]
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(one, r) for r in rotations]
        wait(futures, timeout=seconds + 2)
        results = [f.result() for f in futures if f.done() and not f.exception() and f.result()]
    out = []
    for c in results:
        c['stats'] = plan.stats(c['plan'])
        c['off_m'] = round(c['stats']['distance_m'] - target)
        c['label'] = f"loop heading {_compass(c['rotation'])}"
        c['known_pct'] = None
        out.append(c)
    return out


def _compass(angle) -> str:
    deg = (90 - math.degrees(angle)) % 360          # x/y frame to compass bearing
    return ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'][int((deg + 22.5) // 45) % 8]
