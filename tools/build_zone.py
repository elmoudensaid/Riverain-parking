"""Build zone.json: every street side in and around Jette, marked as covered (1) or not (0)
by the residential parking card, using OpenStreetMap street geometry and house numbers.

Covered streets are split per side and per house-number range; all other streets are
drawn as one centre line marked not covered.
"""
import json, re, sys, time, unicodedata, urllib.parse, urllib.request
from collections import defaultdict

from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import substring

BBOX = (50.862, 4.295, 50.902, 4.352)  # south, west, north, east
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
DRIVABLE = {"primary", "secondary", "tertiary", "unclassified", "residential", "living_street",
            "primary_link", "secondary_link", "tertiary_link", "service", "road"}
OFFSET = 5.0      # metres between centre line and drawn side line
MAX_ADDR = 55.0   # an address farther than this from its street is ignored
EXTEND = 40.0     # how far a side line may run past its first/last address

to_m = Transformer.from_crs("EPSG:4326", "EPSG:31370", always_xy=True).transform
to_ll = Transformer.from_crs("EPSG:31370", "EPSG:4326", always_xy=True).transform


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = s.replace("œ", "oe")
    s = re.sub(r"[’'`]", " ", s)
    s = re.sub(r"[-.]", " ", s)
    s = re.sub(r"\bier\b", "i", s)
    return re.sub(r"\s+", " ", s).strip()


STREETS = {}
for row in json.load(open("tools/streets.json", encoding="utf-8")):
    k = norm(row["street"])
    STREETS.setdefault(k, {"name": row["street"], "ranges": []})["ranges"] += row["ranges"]


def find(name):
    if not name:
        return None
    for c in [name] + name.split(" - "):
        if norm(c) in STREETS:
            return norm(c)
    return None


def covered(key, n):
    return any(a <= n <= b and n % 2 == a % 2 for a, b in STREETS[key]["ranges"])


def overpass(q):
    for attempt in range(3):
        for u in OVERPASS:
            try:
                req = urllib.request.Request(u, data=urllib.parse.urlencode({"data": q}).encode(),
                                             headers={"User-Agent": "riverain-parking-builder"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.load(r)
            except Exception as e:  # try the next mirror
                print("overpass error", u, e, file=sys.stderr)
        time.sleep(20)
    sys.exit("Overpass unreachable")


bb = "{},{},{},{}".format(*BBOX)
ways = overpass(f'[out:json][timeout:120];way["highway"]["name"]({bb});out tags geom;')["elements"]
addrs = overpass(f'[out:json][timeout:120];nwr["addr:housenumber"]["addr:street"]({bb});out center tags;')["elements"]
print(f"{len(ways)} ways, {len(addrs)} addresses")


def proj_line(geom):
    return LineString([to_m(p["lon"], p["lat"]) for p in geom])


def ll_coords(line):
    out = []
    for x, y in line.coords:
        lon, lat = to_ll(x, y)
        out.append([round(lat, 6), round(lon, 6)])
    return out


# covered-street ways, indexed by street key
cways = defaultdict(list)
features = []
for w in ways:
    if "geometry" not in w or len(w["geometry"]) < 2:
        continue
    tags = w["tags"]
    key = find(tags.get("name:fr")) or find(tags.get("name"))
    if key:
        cways[key].append(proj_line(w["geometry"]))
    elif tags.get("highway") in DRIVABLE:
        name = (tags.get("name:fr") or tags["name"]).split(" - ")[0]
        features.append({"t": 0, "n": name, "c": ll_coords(proj_line(w["geometry"]).simplify(1.0))})

street_addrs = defaultdict(list)
# attach every address of a covered street to its nearest way of that street
per_side = defaultdict(list)  # (key, way index, side) -> [(s, status, number)]
missing = defaultdict(int)
for a in addrs:
    tags = a.get("tags", {})
    key = find(tags.get("addr:street:fr")) or find(tags.get("addr:street"))
    if not key or key not in cways:
        continue
    m = re.match(r"\d+", tags.get("addr:housenumber", ""))
    if not m:
        continue
    n = int(m.group())
    c = a.get("center") or {"lat": a.get("lat"), "lon": a.get("lon")}
    if c["lat"] is None:
        continue
    p = Point(to_m(c["lon"], c["lat"]))
    # attach to every way of the street that runs right past it (dual carriageways, split ways),
    # or at least to the nearest one
    dists = [(line.distance(p), i) for i, line in enumerate(cways[key])]
    targets = [i for d, i in dists if d <= 30] or [min(dists)[1]]
    if min(dists)[0] > MAX_ADDR:
        missing[key] += 1
        continue
    street_addrs[key].append((p, n, covered(key, n)))
    for i in targets:
        line = cways[key][i]
        s = line.project(p)
        # which side of the street the address is on
        q1 = line.interpolate(max(0.0, s - 1.0))
        q2 = line.interpolate(min(line.length, s + 1.0))
        cross = (q2.x - q1.x) * (p.y - q1.y) - (q2.y - q1.y) * (p.x - q1.x)
        side = 1 if cross > 0 else -1
        per_side[(key, i, side)].append((s, covered(key, n), n, round(c["lat"], 6), round(c["lon"], 6)))


def parity_numbers_covered(key, odd, lo, hi):
    """Status of every house number of this parity in [lo, hi]: 1 all covered, 0 none, None mixed."""
    rs = [(a, b) for a, b in STREETS[key]["ranges"] if (a % 2 == 1) == odd]
    if not rs:
        return 0
    hit = [r for r in rs if r[1] >= lo and r[0] <= hi]
    if not hit:
        return 0
    first = lo if lo % 2 == (1 if odd else 0) else lo + 1
    if hi < 10**6 and all(any(a <= n <= b for a, b in rs) for n in range(first, hi + 1, 2)):
        return 1
    return None


def no_address_status(key, odd, line=None, a=0.0, b=0.0, pts=(), where=None):
    """Status of a stretch with no house number of its own.
    1) the card has no range for this side's parity -> not covered;
    2) numbers keep rising/falling past the last known address -> deduce from the range;
    3) nearest address of the same parity on this street (<= 60 m) -> same status;
    4) otherwise unknown (2 = check on site)."""
    if odd is None:
        return 2
    if not any((r[0] % 2 == 1) == odd for r in STREETS[key]["ranges"]):
        return 0
    same = [p for p in pts if (p[2] % 2 == 1) == odd]
    if len(same) >= 2 and where in ("start", "end"):
        rising = same[-1][2] > same[0][2]
        edge = same[0][2] if where == "start" else same[-1][2]
        going_up = rising if where == "end" else not rising
        lo, hi = (edge + 1, 10**7) if going_up else (1, edge - 1)
        st = parity_numbers_covered(key, odd, lo, hi)
        if st is not None:
            return st
    if line is not None:
        mid = line.interpolate((a + b) / 2)
        cand = [(mid.distance(p), c) for p, n, c in street_addrs.get(key, []) if (n % 2 == 1) == odd]
        if cand:
            d, c = min(cand, key=lambda t: t[0])
            if d <= 100:
                return int(c)
    return 2


unknown_m = defaultdict(int)
stats = defaultdict(lambda: [0, 0])


def emit(key, line, side, a, b, st, odd, pts):
    if b - a < 2:
        return
    seg = substring(line, a, b)
    if seg.length < 2:
        return
    off = seg.offset_curve(side * OFFSET)
    if off.is_empty or off.geom_type != "LineString":
        return
    nums = sorted({(p[3], p[4], p[2]) for p in pts if a - 1 <= p[0] <= b + 1})
    f = {"t": st, "n": STREETS[key]["name"], "c": ll_coords(off.simplify(0.8)), "a": [list(x) for x in nums]}
    if odd is not None:
        f["p"] = "impairs" if odd else "pairs"
    features.append(f)
    if st == 2:
        unknown_m[STREETS[key]["name"]] += round(b - a)
    else:
        stats[STREETS[key]["name"]][0 if st else 1] += round(b - a)


def faces_twin(key, i, line, side):
    """True when this side of the way looks at another carriageway of the same street (a median)."""
    hits = 0
    for f in (0.25, 0.5, 0.75):
        s0 = line.length * f
        q1 = line.interpolate(max(0.0, s0 - 1.0)); q2 = line.interpolate(min(line.length, s0 + 1.0))
        dx, dy = q2.x - q1.x, q2.y - q1.y
        L = (dx * dx + dy * dy) ** 0.5 or 1.0
        nx, ny = -dy / L * side, dx / L * side  # unit normal pointing to this side
        probe = Point(line.interpolate(s0).x + nx * 12, line.interpolate(s0).y + ny * 12)
        if any(j != i and other.distance(probe) <= 12 for j, other in enumerate(cways[key])):
            hits += 1
    return hits >= 2


for key, lines in cways.items():
    for i, line in enumerate(lines):
        side_odd = {}
        for side in (1, -1):
            pts = per_side.get((key, i, side))
            if pts:
                side_odd[side] = sum(1 for p in pts if p[2] % 2) * 2 >= len(pts)
        if not side_odd:
            # no house number along this way at all: borrow the parity of the nearest address of the street
            near = [(line.distance(p), p, n) for p, n, c in street_addrs.get(key, [])]
            if near:
                d, p0, n0 = min(near, key=lambda t: t[0])
                if d <= 80:
                    s0 = line.project(p0)
                    q1 = line.interpolate(max(0.0, s0 - 1.0)); q2 = line.interpolate(min(line.length, s0 + 1.0))
                    cr = (q2.x - q1.x) * (p0.y - q1.y) - (q2.y - q1.y) * (p0.x - q1.x)
                    side_odd[1 if cr > 0 else -1] = n0 % 2 == 1
        for side in (1, -1):
            pts = sorted(per_side.get((key, i, side), []))
            # parity of this side: its own numbers, else the opposite of the other side
            odd = side_odd.get(side, (not side_odd[-side]) if -side in side_odd else None)
            if not pts and faces_twin(key, i, line, side):
                continue  # inner edge of a dual carriageway (tram / central reservation): no kerb parking
            if not pts:
                emit(key, line, side, 0.0, line.length, no_address_status(key, odd, line, 0.0, line.length), odd, [])
                continue
            # covered / not covered pieces, split halfway between differing addresses
            pieces = []
            start = 0.0 if pts[0][0] <= EXTEND else pts[0][0] - EXTEND
            if start > 0:
                pieces.append([0.0, start, no_address_status(key, odd, line, 0.0, start, pts, "start")])
            cur = int(pts[0][1])
            for (s1, st1, *_), (s2, st2, *_) in zip(pts, pts[1:]):
                if int(st2) != cur:
                    mid = (s1 + s2) / 2
                    pieces.append([start, mid, cur])
                    start, cur = mid, int(st2)
            end = line.length if line.length - pts[-1][0] <= EXTEND else pts[-1][0] + EXTEND
            pieces.append([start, end, cur])
            if end < line.length:
                pieces.append([end, line.length, no_address_status(key, odd, line, end, line.length, pts, "end")])
            for a, b, st in pieces:
                emit(key, line, side, a, b, st, odd, pts)

for name in sorted(s["name"] for s in STREETS.values()):
    ok, no = stats.get(name, [0, 0])
    flag = "" if ok else "   <-- NO COVERED SIDE FOUND"
    print(f"{name:40s} covered {ok:5d} m   not {no:5d} m   unknown {unknown_m.get(name, 0):5d} m{flag}")

ranges = {v["name"]: sorted(v["ranges"]) for v in STREETS.values()}
json.dump({"built": time.strftime("%Y-%m-%d"), "s": ranges, "f": features}, open("zone.json", "w"), separators=(",", ":"))
print(f"{len(features)} features written")
