"""A route as the planner edits it: a chain of segments.

    {'kind': 'track',  'coords': [...]}                 ground taken from a recorded activity
    {'kind': 'routed', 'sport': 'run', 'coords': [...]} found by the routing engine
    {'kind': 'line',   'coords': [...]}                 drawn straight, where no path is mapped

Consecutive segments share their joining point; the joins are the control points the editor
shows. Every edit is "replace the stretch between distance a and distance b with something
else", so a recorded track keeps its exact GPS line everywhere except where it was changed.
Plans are plain JSON: the browser holds the one being edited and sends it with each request.
"""
from . import geo

TRACK_WINDOW_M = 300.0     # how much of a recorded track a drag re-routes on each side


def _seg(kind, coords, sport=None, **extra):
    out = {'kind': kind, 'coords': [list(c) for c in coords]}
    if sport:
        out['sport'] = sport
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def from_track(coords) -> list:
    return [_seg('track', coords)]


def flatten(segs) -> list:
    out = []
    for s in segs:
        out += s['coords'] if not out else s['coords'][1:]
    return out


def bounds(segs) -> list:
    """Distance along the route at each control point: start, every join, end."""
    out = [0.0]
    for s in segs:
        out.append(out[-1] + geo.length(s['coords']))
    return out


def total(segs) -> float:
    return bounds(segs)[-1]


def _split(seg, d):
    """A segment cut at d metres from its start, as (head, tail); either may be None."""
    coords = seg['coords']
    cum = geo.cumulative(coords)
    if d <= 0.5:
        return None, seg
    if d >= cum[-1] - 0.5:
        return seg, None
    p, i = geo.point_at(coords, cum, d)
    head = coords[:i + 1] + [p]
    tail = [p] + coords[i + 1:]
    extra = {k: v for k, v in seg.items() if k not in ('coords', 'unpaved_m', 'busy_m')}
    return {**extra, 'coords': head}, {**extra, 'coords': tail}


def cut(segs, d):
    """(segments before d, segments after d)."""
    b = bounds(segs)
    before, after = [], []
    for k, s in enumerate(segs):
        if b[k + 1] <= d:
            before.append(s)
        elif b[k] >= d:
            after.append(s)
        else:
            head, tail = _split(s, d - b[k])
            if head:
                before.append(head)
            if tail:
                after.append(tail)
    return before, after


def point(segs, d):
    coords = flatten(segs)
    return geo.point_at(coords, geo.cumulative(coords), d)[0]


def replace(segs, d_from, d_to, new_segs) -> list:
    """The route with the stretch [d_from, d_to] swapped for new_segs. The new part is joined
    to the cut points with short straight pieces if the engine snapped it onto a road nearby."""
    before, _ = cut(segs, d_from)
    _, after = cut(segs, d_to)
    a, b = point(segs, d_from), point(segs, d_to)
    new_segs = [dict(s) for s in new_segs if len(s['coords']) >= 1]
    if new_segs and a is not None and geo.dist(a, new_segs[0]['coords'][0]) > 1.0:
        new_segs[0] = {**new_segs[0], 'coords': [list(a)] + new_segs[0]['coords']}
    if new_segs and b is not None and geo.dist(new_segs[-1]['coords'][-1], b) > 1.0:
        new_segs[-1] = {**new_segs[-1], 'coords': new_segs[-1]['coords'] + [list(b)]}
    return [s for s in before + new_segs + after if len(s['coords']) >= 2]


def routed_segment(router, points, sport) -> dict:
    leg = router.route(points, sport)
    return _seg('routed', leg.coords, sport=sport, unpaved_m=round(leg.unpaved_m), busy_m=round(leg.busy_m))


def reroute(segs, d_from, d_to, vias, sport, router) -> list:
    """Replace [d_from, d_to] with what the engine finds from there, through vias, to there."""
    a, b = point(segs, d_from), point(segs, d_to)
    return replace(segs, d_from, d_to, [routed_segment(router, [a] + list(vias) + [b], sport)])


def window(segs, d):
    """The stretch an edit at distance d may change: the whole segment if it was routed (as in
    any route planner), or a few hundred metres either side on a recorded track."""
    b = bounds(segs)
    for k, s in enumerate(segs):
        if b[k] <= d <= b[k + 1]:
            if s['kind'] == 'track':
                return max(b[k], d - TRACK_WINDOW_M), min(b[k + 1], d + TRACK_WINDOW_M)
            return b[k], b[k + 1]
    return max(0.0, d - TRACK_WINDOW_M), min(b[-1], d + TRACK_WINDOW_M)


def control_window(segs, k):
    """Around control point k (0 = start): its two neighbouring segments' windows joined."""
    b = bounds(segs)
    d = b[k]
    lo = window(segs, max(0.0, d - 0.1))[0] if k > 0 else d
    hi = window(segs, min(b[-1], d + 0.1))[1] if k < len(segs) else d
    return lo, hi


def drag(segs, d, to, sport, router) -> list:
    """The line was grabbed at distance d and dropped at `to`: route through `to` instead."""
    lo, hi = window(segs, d)
    return reroute(segs, lo, hi, [to], sport, router)


def move_control(segs, k, to, sport, router) -> list:
    b = bounds(segs)
    if k == 0 or k == len(segs):
        # An end point: re-route the first or last stretch to start or finish there.
        if k == 0:
            hi = window(segs, 0.1)[1]
            return replace(segs, 0.0, hi, [routed_segment(router, [to, point(segs, hi)], sport)])
        lo = window(segs, b[-1] - 0.1)[0]
        return replace(segs, lo, b[-1], [routed_segment(router, [point(segs, lo), to], sport)])
    lo, hi = control_window(segs, k)
    return reroute(segs, lo, hi, [to], sport, router)


def delete_control(segs, k, sport, router) -> list:
    b = bounds(segs)
    if len(segs) <= 1:
        return segs
    if k == 0:
        return segs[1:]
    if k == len(segs):
        return segs[:-1]
    lo, hi = control_window(segs, k)
    return reroute(segs, lo, hi, [], sport, router)


def append(segs, to, sport, router) -> list:
    if not segs:
        return [_seg('routed', [list(to)[:2] + [None]], sport=sport)]
    last = flatten(segs)[-1]
    if len(flatten(segs)) < 2:          # only a start point so far
        return [routed_segment(router, [last, to], sport)]
    return segs + [routed_segment(router, [last, to], sport)]


def close_loop(segs, sport, router) -> list:
    coords = flatten(segs)
    if len(coords) < 2 or geo.dist(coords[0], coords[-1]) < 5:
        return segs
    return segs + [routed_segment(router, [coords[-1], coords[0]], sport)]


def reverse(segs) -> list:
    return [{**s, 'coords': [list(c) for c in reversed(s['coords'])]} for s in reversed(segs)]


def truncate(segs, d) -> list:
    return cut(segs, d)[0]


def stats(segs) -> dict:
    coords = flatten(segs)
    if len(coords) < 2:
        return {'distance_m': 0, 'ascent_m': 0, 'unpaved_pct': None, 'busy_pct': None, 'loop': False}
    lengths = [geo.length(s['coords']) for s in segs]
    routed = [(s, n) for s, n in zip(segs, lengths) if s['kind'] == 'routed']
    routed_len = sum(n for _, n in routed)
    unpaved = sum(s.get('unpaved_m', 0) for s, _ in routed)
    busy = sum(s.get('busy_m', 0) for s, _ in routed)
    distance = sum(lengths)
    return {
        'distance_m': round(distance, 1),
        'ascent_m': round(geo.ascent(coords)),
        # Surface is only known where the engine routed; a recorded track has no way tags.
        'unpaved_pct': round(100 * unpaved / routed_len) if routed_len > 0.5 * distance else None,
        'busy_pct': round(100 * busy / routed_len) if routed_len > 0.5 * distance else None,
        'loop': geo.is_loop(coords),
    }


def elevation_profile(segs, points: int = 200) -> list:
    """[[distance_m, ele], ...] at about `points` evenly spaced places, for the strip chart."""
    coords = flatten(segs)
    if len(coords) < 2:
        return []
    cum = geo.cumulative(coords)
    step = max(cum[-1] / points, 1.0)
    out, d = [], 0.0
    while d <= cum[-1]:
        p, _ = geo.point_at(coords, cum, d)
        out.append([round(d), round(p[2], 1) if len(p) > 2 and p[2] is not None else None])
        d += step
    return out
