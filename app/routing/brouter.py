"""Client for the BRouter routing engine - the only code that talks to it.

BRouter runs on munchlax as its own process (docs/fitmon-deploy.md), bound to loopback on
port 8642 (reserved in pokeflute's registry), and routes over OpenStreetMap tiles kept on disk,
so no route - which is to say, nobody's home address - leaves the house. Swapping the engine
means writing another class with the same route() method.
"""
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field

from . import geo

DEFAULT_URL = 'http://127.0.0.1:8642'

# fitmon's route sports and the BRouter profile each routes with. BRouter ships no running
# profile; hiking-mountain prefers paths and tracks and keeps off main roads, which is what a
# runner wants. Tune by copying a profile to customprofiles/ rather than editing a shipped one.
PROFILES = {'run': 'hiking-mountain', 'hike': 'hiking-mountain', 'road': 'fastbike',
            'gravel': 'gravel', 'mtb': 'mtb', 'bike': 'trekking'}
SPORT_LABELS = {'run': 'Run', 'hike': 'Hike / walk', 'road': 'Road bike', 'gravel': 'Gravel',
                'mtb': 'Mountain bike', 'bike': 'Bike'}

UNPAVED = {'unpaved', 'gravel', 'fine_gravel', 'compacted', 'dirt', 'earth', 'ground', 'grass',
           'mud', 'sand', 'woodchips', 'pebblestone', 'rock', 'grass_paver'}
BUSY_HIGHWAYS = {'primary', 'primary_link', 'secondary', 'secondary_link', 'trunk', 'trunk_link'}


class RouterError(Exception):
    """The engine is unreachable, or found no way between the points."""


@dataclass
class Leg:
    coords: list                 # [[lon, lat, ele], ...]
    distance_m: float
    unpaved_m: float = 0.0
    busy_m: float = 0.0
    extra: dict = field(default_factory=dict)


def sport_for_session(sport: str, sub_sport: str | None) -> str | None:
    """The route sport an activity belongs to, or None if it is not something one plans a route for."""
    if sport == 'running':
        return 'run'
    if sport in ('hiking', 'walking'):
        return 'hike'
    if sport == 'cycling':
        return {'road': 'road', 'gravel_cycling': 'gravel', 'mountain': 'mtb',
                'indoor_cycling': None}.get(sub_sport or '', 'bike')
    return None


# Route sports that share ground: a gravel ride can follow a route saved from a trekking ride.
FAMILY = {'run': 'foot', 'hike': 'foot', 'road': 'bike', 'gravel': 'bike', 'mtb': 'bike', 'bike': 'bike'}
FAMILY_SPORTS = {'foot': ('running', 'hiking', 'walking'), 'bike': ('cycling',)}


def _tag(tags: str, key: str) -> str | None:
    for part in tags.split():
        k, _, v = part.partition('=')
        if k == key:
            return v
    return None


def _surface_stats(messages) -> tuple[float, float]:
    """Unpaved and busy-road metres from BRouter's per-segment way tags."""
    if not messages or len(messages) < 2:
        return 0.0, 0.0
    head = messages[0]
    try:
        i_dist, i_tags = head.index('Distance'), head.index('WayTags')
    except ValueError:
        return 0.0, 0.0
    unpaved = busy = 0.0
    for row in messages[1:]:
        try:
            d = float(row[i_dist])
        except (ValueError, IndexError):
            continue
        tags = row[i_tags] if len(row) > i_tags else ''
        surface, highway = _tag(tags, 'surface'), _tag(tags, 'highway')
        if surface in UNPAVED or (surface is None and highway in ('track', 'path', 'bridleway')):
            unpaved += d
        if highway in BUSY_HIGHWAYS:
            busy += d
    return unpaved, busy


class BRouter:
    def __init__(self, base_url: str | None = None, timeout: float = 15.0, cache_size: int = 4000):
        self.base_url = (base_url or os.environ.get('FITMON_BROUTER_URL') or DEFAULT_URL).rstrip('/')
        self.timeout = timeout
        self._cache: OrderedDict = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            self.route([[8.0, 50.0], [8.0001, 50.0]], 'run')
            return True
        except RouterError as exc:
            # "No route" means the engine answered; only a connection failure means it is down.
            return 'unreachable' not in str(exc)

    def route(self, points, sport: str) -> Leg:
        profile = PROFILES.get(sport, 'trekking')
        key = (profile, tuple((round(p[0], 6), round(p[1], 6)) for p in points))
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        lonlats = '|'.join(f'{p[0]:.6f},{p[1]:.6f}' for p in points)
        query = urllib.parse.urlencode({'lonlats': lonlats, 'profile': profile, 'alternativeidx': 0,
                                        'format': 'geojson'}, safe='|,')
        try:
            with urllib.request.urlopen(f'{self.base_url}/brouter?{query}', timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace').strip()[:200]
            raise RouterError(f'no route: {detail}') from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RouterError(f'routing engine unreachable at {self.base_url}: {exc}') from exc
        try:
            feature = json.loads(body)['features'][0]
        except (ValueError, KeyError, IndexError) as exc:
            raise RouterError(f'no route: {body[:200]!r}') from exc
        coords = [[round(c[0], 6), round(c[1], 6), round(c[2], 1) if len(c) > 2 else None]
                  for c in feature['geometry']['coordinates']]
        props = feature.get('properties', {})
        unpaved, busy = _surface_stats(props.get('messages'))
        try:
            distance = float(props.get('track-length'))
        except (TypeError, ValueError):
            distance = geo.length(coords)
        leg = Leg(coords=coords, distance_m=distance, unpaved_m=unpaved, busy_m=busy)
        with self._lock:
            self._cache[key] = leg
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return leg


class StraightRouter:
    """Joins the points with straight lines. For tests, and for 'draw it as a line' segments."""
    def route(self, points, sport: str) -> Leg:
        coords = []
        for a, b in zip(points, points[1:]):
            n = max(1, int(geo.dist(a, b) // 50))
            part = [geo.interpolate(list(a)[:2], list(b)[:2], k / n) for k in range(n + 1)]
            coords += part if not coords else part[1:]
        coords = [[c[0], c[1], None] for c in coords] or [[points[0][0], points[0][1], None]]
        return Leg(coords=coords, distance_m=geo.length(coords))


_default: BRouter | None = None


def default_router() -> BRouter:
    global _default
    if _default is None:
        _default = BRouter()
    return _default
