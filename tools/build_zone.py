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
    i, line = min(enumerate(cways[key]), key=lambda t: t[1].distance(p))
    if line.distance(p) > MAX_ADDR:
        missing[key] += 1
        continue
    s = line.project(p)
    # which side of the street the address is on
    q1 = line.interpolate(max(0.0, s - 1.0))
    q2 = line.interpolate(min(line.length, s + 1.0))
    cross = (q2.x - q1.x) * (p.y - q1.y) - (q2.y - q1.y) * (p.x - q1.x)
    side = 1 if cross > 0 else -1
    per_side[(key, i, side)].append((s, covered(key, n), n))

stats = defaultdict(lambda: [0, 0])
for (key, i, side), pts in per_side.items():
    line = cways[key][i]
    pts.sort()
    odd = sum(1 for _, _, n in pts if n % 2) * 2 >= len(pts)
    # build [start, end, status] pieces along the line, split halfway between differing addresses
    pieces = []
    start = 0.0 if pts[0][0] <= EXTEND else pts[0][0] - EXTEND
    cur = pts[0][1]
    for (s1, st1, _), (s2, st2, _) in zip(pts, pts[1:]):
        if st2 != cur:
            mid = (s1 + s2) / 2
            pieces.append([start, mid, cur])
            start, cur = mid, st2
    end = line.length if line.length - pts[-1][0] <= EXTEND else pts[-1][0] + EXTEND
    pieces.append([start, end, cur])
    for a, b, st in pieces:
        if b - a < 2:
            continue
        seg = substring(line, a, b)
        if seg.length < 2:
            continue
        off = seg.offset_curve(side * OFFSET)
        if off.is_empty or off.geom_type != "LineString":
            continue
        features.append({"t": 1 if st else 0, "n": STREETS[key]["name"], "p": "impairs" if odd else "pairs",
                         "c": ll_coords(off.simplify(0.8))})
        stats[STREETS[key]["name"]][0 if st else 1] += round(b - a)

for name in sorted(s["name"] for s in STREETS.values()):
    ok, no = stats.get(name, [0, 0])
    flag = "" if ok else "   <-- NO COVERED SIDE FOUND"
    print(f"{name:40s} covered {ok:5d} m   not {no:5d} m{flag}")

json.dump({"built": time.strftime("%Y-%m-%d"), "f": features}, open("zone.json", "w"), separators=(",", ":"))
print(f"{len(features)} features written")
