"""Small geometry on [lon, lat(, ele)] polylines, in metres.

Distances are haversine; anything that needs x/y (offsets, simplification, nearest point) uses a
local equirectangular projection around the line, which is accurate to well under a metre over
the tens of kilometres a run or ride covers.
"""
import math

R_EARTH = 6371000.0


def dist(a, b) -> float:
    """Haversine distance in metres between two [lon, lat] points."""
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)
    return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(h)))


def cumulative(coords) -> list:
    """Distance along the line at each vertex, starting at 0."""
    out, total = [0.0], 0.0
    for i in range(1, len(coords)):
        total += dist(coords[i - 1], coords[i])
        out.append(total)
    return out


def length(coords) -> float:
    return cumulative(coords)[-1] if len(coords) > 1 else 0.0


def ascent(coords, threshold: float = 3.0) -> float:
    """Climb in metres with a small hysteresis, so DEM noise on the flat does not add up."""
    total, base = 0.0, None
    for c in coords:
        if len(c) < 3 or c[2] is None:
            continue
        if base is None:
            base = c[2]
        elif c[2] - base >= threshold:
            total += c[2] - base
            base = c[2]
        elif c[2] < base:
            base = c[2]
    return total


class Projection:
    """Local x/y in metres around a reference latitude."""
    def __init__(self, lat0: float, lon0: float = 0.0):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = math.cos(math.radians(lat0)) * math.pi * R_EARTH / 180
        self.ky = math.pi * R_EARTH / 180

    @classmethod
    def around(cls, coords):
        lats = [c[1] for c in coords]
        lons = [c[0] for c in coords]
        return cls((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)

    def xy(self, c):
        return ((c[0] - self.lon0) * self.kx, (c[1] - self.lat0) * self.ky)

    def lonlat(self, x, y):
        return [self.lon0 + x / self.kx, self.lat0 + y / self.ky]


def interpolate(a, b, f: float):
    out = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]
    if len(a) > 2 and len(b) > 2 and a[2] is not None and b[2] is not None:
        out.append(a[2] + (b[2] - a[2]) * f)
    return out


def point_at(coords, cum, d: float):
    """(point, index of the vertex before it) at distance d along the line."""
    if d <= 0:
        return list(coords[0]), 0
    if d >= cum[-1]:
        return list(coords[-1]), len(coords) - 1
    lo, hi = 0, len(cum) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cum[mid] <= d:
            lo = mid
        else:
            hi = mid
    seg = cum[hi] - cum[lo]
    f = (d - cum[lo]) / seg if seg > 0 else 0.0
    return interpolate(coords[lo], coords[hi], f), lo


def bearing_at(coords, cum, d: float, span: float = 40.0) -> float:
    """Direction of travel (radians, x/y frame: 0 = east, pi/2 = north) around distance d."""
    a, _ = point_at(coords, cum, max(0.0, d - span))
    b, _ = point_at(coords, cum, min(cum[-1], d + span))
    proj = Projection(a[1])
    (x1, y1), (x2, y2) = proj.xy(a), proj.xy(b)
    return math.atan2(y2 - y1, x2 - x1)


def offset(point, angle: float, metres: float):
    """The point `metres` away from `point` in direction `angle` (x/y frame)."""
    proj = Projection(point[1], point[0])
    return proj.lonlat(math.cos(angle) * metres, math.sin(angle) * metres)


def resample(coords, step: float):
    """Points every `step` metres along the line (the first and last vertex included)."""
    if len(coords) < 2:
        return [list(c) for c in coords]
    cum = cumulative(coords)
    n = max(1, int(cum[-1] // step))
    out = [point_at(coords, cum, i * step)[0] for i in range(n + 1)]
    if cum[-1] - n * step > step / 4:
        out.append(list(coords[-1]))
    return out


def simplify(coords, tolerance: float):
    """Douglas-Peucker in metres. Keeps the endpoints; iterative, so long tracks are fine."""
    if len(coords) < 3:
        return [list(c) for c in coords]
    proj = Projection.around(coords)
    pts = [proj.xy(c) for c in coords]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    tol2 = tolerance * tolerance
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = pts[i], pts[j]
        dx, dy = x2 - x1, y2 - y1
        seg2 = dx * dx + dy * dy
        best, best_d = None, tol2
        for k in range(i + 1, j):
            px, py = pts[k]
            if seg2 == 0:
                d2 = (px - x1) ** 2 + (py - y1) ** 2
            else:
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg2))
                d2 = (px - x1 - t * dx) ** 2 + (py - y1 - t * dy) ** 2
            if d2 > best_d:
                best, best_d = k, d2
        if best is not None:
            keep[best] = True
            stack += [(i, best), (best, j)]
    return [list(c) for c, k in zip(coords, keep) if k]


def centroid(coords):
    proj = Projection.around(coords)
    xs, ys = zip(*(proj.xy(c) for c in coords))
    return proj.lonlat(sum(xs) / len(xs), sum(ys) / len(ys))


def is_loop(coords, within: float = 250.0) -> bool:
    return len(coords) > 2 and dist(coords[0], coords[-1]) <= within


class GridIndex:
    """Points bucketed in square cells, for 'is anything within r metres of here' queries."""
    def __init__(self, coords, cell: float = 30.0):
        self.cell = cell
        self.proj = Projection.around(coords) if coords else Projection(0.0)
        self.cells: dict = {}
        for i, c in enumerate(coords):
            x, y = self.proj.xy(c)
            self.cells.setdefault((int(x // cell), int(y // cell)), []).append((x, y, i))

    def nearest(self, c, radius: float):
        """(index, metres) of the nearest indexed point within radius, or (None, None)."""
        x, y = self.proj.xy(c)
        cx, cy, reach = int(x // self.cell), int(y // self.cell), int(math.ceil(radius / self.cell))
        best, best_d = None, radius * radius
        for gx in range(cx - reach, cx + reach + 1):
            for gy in range(cy - reach, cy + reach + 1):
                for px, py, i in self.cells.get((gx, gy), ()):
                    d2 = (px - x) ** 2 + (py - y) ** 2
                    if d2 <= best_d:
                        best, best_d = i, d2
        return (best, math.sqrt(best_d)) if best is not None else (None, None)

    def near(self, c, radius: float) -> bool:
        return self.nearest(c, radius)[0] is not None


def coverage(points, index: GridIndex, radius: float) -> float:
    """Share of `points` within `radius` metres of the indexed line."""
    if not points:
        return 0.0
    return sum(1 for p in points if index.near(p, radius)) / len(points)


def bbox(coords):
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return [min(lons), min(lats), max(lons), max(lats)]
