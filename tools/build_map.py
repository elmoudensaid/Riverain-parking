"""Build map.json: continuous kerb lines per street side, coloured for the residential parking card,
from official data only:
  - UrbIS street axes and address numbers (Brussels Region),
  - the card's street list and house-number ranges (tools/streets.json, copied from the PDF),
  - parking.brussels on-street places: red/orange pricing zones and reserved places
    (PMR, delivery, charging, moto...), which are shown as their own small markers.
"""
import json, math, re, sys, time, unicodedata, urllib.parse, urllib.request
from collections import Counter, defaultdict

from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import substring
from shapely.strtree import STRtree

PB = "https://ows.parking.brussels/ws/ParkingBrussels/ows"
URBIS = "https://geoservices-urbis.irisnet.be/geoserver/urbisvector/wfs"
S, W, N, E = 50.860, 4.290, 50.905, 4.355
OFFSET = 6.0      # metres between street axis and the drawn kerb line
MAX_ADDR = 60.0   # an address farther than this from its street is ignored
EXTEND = 40.0     # how far a kerb line runs past its first/last address before we deduce

to_ll = Transformer.from_crs("EPSG:31370", "EPSG:4326", always_xy=True).transform
to_m = Transformer.from_crs("EPSG:4326", "EPSG:31370", always_xy=True).transform
x0, y0 = to_m(W, S)
x1, y1 = to_m(E, N)
BBOX = f"{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f},EPSG:31370"

TYPRES = {1: "Place livraison", 2: "Place poids lourds", 3: "Place bus", 5: "Place PMR", 6: "Place taxi",
          8: "Place corps diplomatique", 9: "Place police", 10: "Place moto", 11: "Place vélo",
          12: "Place kiss & ride", 13: "Zone de livraison", 14: "Zone kiss & ride", 16: "Zone 30 minutes",
          17: "Zone 60 minutes", 18: "Zone Shop & Go", 19: "Recharge électrique", 20: "Voiture partagée",
          21: "Zone autocar", 22: "Zone autocar", 23: "Zone autocar", 24: "Zone poids lourds",
          25: "Zone réservée", 26: "Place réservée"}


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower().replace("œ", "oe")
    s = re.sub(r"[’'`]", " ", s)
    s = re.sub(r"[-.]", " ", s)
    s = re.sub(r"\bier\b", "i", s)
    return re.sub(r"\s+", " ", s).strip()


STREETS = {}
for row in json.load(open("tools/streets.json", encoding="utf-8")):
    STREETS.setdefault(norm(row["street"]), {"name": row["street"], "ranges": []})["ranges"] += row["ranges"]
MAXNUM = {k: max(b for a, b in v["ranges"]) for k, v in STREETS.items()}


def covered(key, n):
    return any(a <= n <= b and n % 2 == a % 2 for a, b in STREETS[key]["ranges"])


def ll_coords(line):
    out = []
    for x, y in line.coords:
        lon, lat = to_ll(x, y)
        out.append([round(lat, 6), round(lon, 6)])
    return out


def fetch(base, layer, props=None, extra=None, page=20000):
    out, start = [], 0
    while True:
        q = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": layer,
             "outputFormat": "application/json", "srsName": "EPSG:31370"}
        if page:
            q.update({"count": page, "startIndex": start})
        if props:
            q["propertyName"] = ",".join(props)
        if extra:
            q.update(extra)
        else:
            q["bbox"] = BBOX
        url = base + "?" + urllib.parse.urlencode(q)
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "riverain-parking-builder"})
                with urllib.request.urlopen(req, timeout=240) as r:
                    feats = json.load(r)["features"]
                break
            except Exception as e:
                body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
                body = " | ".join(re.findall(r"<ows:ExceptionText>(.*?)</ows:ExceptionText>", body, re.S))[:600]
                print("retry", layer, e, body, file=sys.stderr)
                time.sleep(15)
        else:
            sys.exit(f"cannot download {layer}")
        out += feats
        if not page or len(feats) < page:
            return out
        start += page


t0 = time.time()
axes = fetch(URBIS, "urbisvector:StreetAxes", page=None)
addrs = fetch(URBIS, "urbisvector:AddressNumbers", page=None,
              extra={"CQL_FILTER": "ZIPCODE IN ('1090','1020','1083','1081','1080')"})
places = fetch(PB, "ParkingBrussels:on_street", page=None,
               extra={"CQL_FILTER": "muniscode IN ('21010','21004','21008','21011','21012')"})
print(f"downloaded {len(axes)} axes, {len(addrs)} address rows, {len(places)} places in {time.time()-t0:.0f}s")

# ---------- street axes ----------
cways = defaultdict(list)
features = []
for f in axes:
    p = f["properties"]
    if p.get("LVL", 0) not in (0, None) or not f.get("geometry"):
        continue  # tunnels / bridges
    g = shape(f["geometry"])
    name = p.get("STRNAMEFRE") or ""
    key = norm(name)
    for part in (g.geoms if g.geom_type.startswith("Multi") else [g]):
        if part.length < 3:
            continue
        if key in STREETS:
            cways[key].append(part)
        elif name:
            features.append({"t": 0, "n": name, "c": ll_coords(part.simplify(1.0))})

# ---------- official address numbers, one point per (street, number) ----------
street_addrs = defaultdict(list)
per_side = defaultdict(list)  # (key, way index, side) -> [(s, covered, number, lat, lon)]
seen = set()
for f in addrs:
    p = f["properties"]
    key = norm(p.get("STRNAMEFRE"))
    if key not in cways or not f.get("geometry"):
        continue
    m = re.match(r"\d+", (p.get("POLICENUM") or "").strip())
    if not m:
        continue
    n = int(m.group())
    if n >= 1000 and MAXNUM[key] < 1000:
        continue  # building/flat codes such as 2103, not house numbers
    if (key, n) in seen:
        continue
    seen.add((key, n))
    pt = shape(f["geometry"])
    pt = pt if pt.geom_type == "Point" else pt.centroid
    dists = [(line.distance(pt), i) for i, line in enumerate(cways[key])]
    if min(dists)[0] > MAX_ADDR:
        continue
    lon, lat = to_ll(pt.x, pt.y)
    street_addrs[key].append((pt, n, covered(key, n)))
    for i in [i for d, i in dists if d <= 30] or [min(dists)[1]]:
        line = cways[key][i]
        s = line.project(pt)
        q1 = line.interpolate(max(0.0, s - 1.0)); q2 = line.interpolate(min(line.length, s + 1.0))
        side = 1 if (q2.x - q1.x) * (pt.y - q1.y) - (q2.y - q1.y) * (pt.x - q1.x) > 0 else -1
        per_side[(key, i, side)].append((s, covered(key, n), n, round(lat, 6), round(lon, 6)))
print("card streets without any address:", [v["name"] for k, v in STREETS.items() if k not in street_addrs] or "none")

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
    # only numbers that really belong to this piece (same colour), so the app never says "n° 294" on red
    nums = sorted({(p[3], p[4], p[2]) for p in pts if a <= p[0] <= b and int(p[1]) == st})
    f = {"t": st, "n": STREETS[key]["name"], "c": ll_coords(off.simplify(0.8)), "a": [list(x) for x in nums]}
    if odd is not None:
        f["p"] = "impairs" if odd else "pairs"
    features.append(f)
    if st == 2:
        unknown_m[STREETS[key]["name"]] += round(b - a)
    else:
        stats[STREETS[key]["name"]][0 if st else 1] += round(b - a)


def clean(pts):
    """Drop house numbers that break the sequence along the street."""
    if len(pts) < 3:
        return pts
    ns = [p[2] for p in pts]
    keep = []
    for k, p in enumerate(pts):
        nb = ns[max(0, k - 3):k] + ns[k + 1:k + 4]
        nb.sort()
        med = nb[len(nb) // 2]
        if abs(p[2] - med) <= max(60, med * 0.6):
            keep.append(p)
    return keep or pts


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
            pts = clean(sorted(per_side.get((key, i, side), [])))
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


# ---------- red / orange pricing zones: the card is not valid there ----------
zone_pts, zone_bad = [], []
reserved = []
for f in places:
    if not f.get("geometry"):
        continue
    p = f["properties"]
    g = shape(f["geometry"])
    c = g.centroid
    if not (x0 <= c.x <= x1 and y0 <= c.y <= y1):
        continue
    zone_pts.append(c)
    zone_bad.append((p.get("typreg") or 0) in (1, 2))
    if p.get("typres"):
        lon, lat = to_ll(c.x, c.y)
        reserved.append([round(lat, 6), round(lon, 6), TYPRES.get(p["typres"], "Place réservée")])
ztree = STRtree(zone_pts)
to_m_line = lambda coords: LineString([to_m(lo, la) for la, lo in coords])
flipped = 0
for f in features:
    if f["t"] != 1:
        continue
    idx = ztree.query(to_m_line(f["c"]), predicate="dwithin", distance=8)
    if len(idx) and sum(zone_bad[i] for i in idx) * 2 > len(idx):
        f["t"], f["z"] = 0, "Zone rouge ou orange : carte non valable"
        flipped += 1
print("kerb pieces in red/orange zones:", flipped)

# ---------- checks: the build stops instead of publishing something wrong ----------
wrong = 0
for f in features:
    for la, lo, n in f.get("a", []):
        key = norm(f["n"])
        if f["t"] in (0, 1) and "z" not in f and covered(key, n) != (f["t"] == 1):
            wrong += 1
green_by_street = Counter(f["n"] for f in features if f["t"] == 1)
no_green = [v["name"] for v in STREETS.values() if not green_by_street.get(v["name"])]
print("house numbers drawn in the wrong colour:", wrong)
print("card streets with no green kerb:", no_green or "none")
checks = {"features": len(features) > 1000, "no wrong colour": wrong == 0, "card streets mostly green": len(no_green) <= 3}
print("checks:", checks)
if not all(checks.values()):
    sys.exit("checks failed: map.json not updated")

LBL = sorted({r[2] for r in reserved})
json.dump({"built": time.strftime("%Y-%m-%d"), "s": {v["name"]: sorted(v["ranges"]) for v in STREETS.values()},
           "f": features, "R": LBL, "r": [[a, b, LBL.index(c)] for a, b, c in reserved]},
          open("map.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print(f"map.json: {len(features)} kerb pieces, {len(reserved)} reserved places")
