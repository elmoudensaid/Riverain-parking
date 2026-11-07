"""Build places.json from official open data only.

Every on-street parking place recorded by parking.brussels around Jette gets a verdict for
the residential card, from:
  - the card's street list and house-number ranges (tools/streets.json, copied from the PDF),
  - the official UrbIS address points and street axes of the Brussels Region,
  - the place's own reservation type and pricing zone (parking.brussels),
and is cross-checked against the official permit sector polygons.

Sources (open data): parking.brussels WFS (on_street, permit_secteurs_mailles, permit_limitrophes),
UrbIS WFS (AddressNumbers, StreetAxes).
"""
import json, re, sys, time, unicodedata, urllib.parse, urllib.request
from collections import Counter, defaultdict

from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

PB = "https://ows.parking.brussels/ws/ParkingBrussels/ows"
URBIS = "https://geoservices-urbis.irisnet.be/geoserver/urbisvector/wfs"
S, W, N, E = 50.860, 4.290, 50.905, 4.355
CARD_SECTORS = ["JETR_FLORAIR", "JETR_THEODOR", "JETR_PARC_ROI_BAUDOUIN", "JETR_ESSEGHEM"]
CARD_BORDER = ["LIM_JET_BRU_FLORAIR"]

to_ll = Transformer.from_crs("EPSG:31370", "EPSG:4326", always_xy=True).transform
to_m = Transformer.from_crs("EPSG:4326", "EPSG:31370", always_xy=True).transform
x0, y0 = to_m(W, S)
x1, y1 = to_m(E, N)
BBOX = f"{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f},EPSG:31370"

# reservation types (typres) from the parking.brussels technical specification
TYPRES = {1: "Place livraison", 2: "Place poids lourds", 3: "Place bus", 5: "Place PMR", 6: "Place taxi",
          8: "Place corps diplomatique", 9: "Place police", 10: "Place moto", 11: "Place vélo",
          12: "Place kiss & ride", 13: "Zone de livraison", 14: "Zone kiss & ride", 16: "Zone 30 minutes",
          17: "Zone 60 minutes", 18: "Zone Shop & Go", 19: "Recharge électrique", 20: "Voiture partagée",
          21: "Zone autocar", 22: "Zone autocar", 23: "Zone autocar", 24: "Zone poids lourds",
          25: "Zone réservée", 26: "Place réservée"}
# pricing zones (typreg)
TYPREG = {0: "non réglementée", 1: "rouge", 2: "orange", 3: "grise", 4: "verte", 5: "bleue", 9: "réservé riverain"}


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower().replace("œ", "oe")
    s = re.sub(r"[’'`]", " ", s)
    s = re.sub(r"[-.]", " ", s)
    s = re.sub(r"\bier\b", "i", s)
    return re.sub(r"\s+", " ", s).strip()


CARD = {}
for row in json.load(open("tools/streets.json", encoding="utf-8")):
    CARD.setdefault(norm(row["street"]), {"name": row["street"], "ranges": []})["ranges"] += row["ranges"]


def in_ranges(key, n):
    return any(a <= n <= b and n % 2 == a % 2 for a, b in CARD[key]["ranges"])


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
# parking.brussels refuses bbox/paging on this layer: ask for the municipalities around Jette instead
places = fetch(PB, "ParkingBrussels:on_street", page=None,
               extra={"CQL_FILTER": "muniscode IN ('21010','21004','21008','21011','21012')"})
_box = (x0, y0, x1, y1)
places = [f for f in places if f.get("geometry") and _box[0] <= shape(f["geometry"]).centroid.x <= _box[2]
          and _box[1] <= shape(f["geometry"]).centroid.y <= _box[3]]
# UrbIS also refuses bbox paging on addresses: filter by postcode (Jette, Laeken, Ganshoren, Koekelberg, Molenbeek)
addrs = fetch(URBIS, "urbisvector:AddressNumbers", page=None,
              extra={"CQL_FILTER": "ZIPCODE IN ('1090','1020','1083','1081','1080')"})
axes = fetch(URBIS, "urbisvector:StreetAxes", page=None)
sectors = fetch(PB, "ParkingBrussels:permit_secteurs_mailles", page=None, extra={"CQL_FILTER": "zonecode IN (%s)" % ",".join(f"'{c}'" for c in CARD_SECTORS)})
borders = fetch(PB, "ParkingBrussels:permit_limitrophes", page=None, extra={"CQL_FILTER": "zonecode IN (%s)" % ",".join(f"'{c}'" for c in CARD_BORDER)})
print(f"downloaded: {len(places)} places, {len(addrs)} address rows, {len(axes)} axes, "
      f"{len(sectors)} sectors, {len(borders)} border zones in {time.time() - t0:.0f}s")

# ---------- reference geometry ----------
sector_geom = unary_union([shape(f["geometry"]) for f in sectors + borders]).buffer(0)

ax_lines, ax_names = [], []
for f in axes:
    p = f["properties"]
    if p.get("LVL", 0) not in (0, None) or not f.get("geometry"):
        continue  # tunnels and bridges over other streets are not where you park
    g = shape(f["geometry"])
    for part in (g.geoms if g.geom_type.startswith("Multi") else [g]):
        ax_lines.append(part)
        ax_names.append(p.get("STRNAMEFRE") or "")
ax_tree = STRtree(ax_lines)

# one point per house number (UrbIS repeats the number for every box/apartment)
seen, A_by_street = set(), defaultdict(list)
for f in addrs:
    p = f["properties"]
    m = re.match(r"\d+", (p.get("POLICENUM") or "").strip())
    if not m or not f.get("geometry"):
        continue
    key = (p.get("STRNAMEFRE"), m.group())
    if key in seen:
        continue
    seen.add(key)
    pt = shape(f["geometry"])
    if pt.geom_type != "Point":
        pt = pt.centroid
    A_by_street[norm(p.get("STRNAMEFRE"))].append((pt, int(m.group())))
A_tree = {k: STRtree([a[0] for a in v]) for k, v in A_by_street.items()}

missing = [v["name"] for k, v in CARD.items() if k not in A_by_street]
print("card streets without any UrbIS address:", missing or "none")


def side_of(line, pt):
    s = line.project(pt)
    q1 = line.interpolate(max(0.0, s - 1.0))
    q2 = line.interpolate(min(line.length, s + 1.0))
    return 1 if (q2.x - q1.x) * (pt.y - q1.y) - (q2.y - q1.y) * (pt.x - q1.x) > 0 else -1


def street_and_number(pt):
    """Street of the nearest axis, then the nearest house number of that street on the same side."""
    idx = ax_tree.query(pt, predicate="dwithin", distance=30)
    if len(idx) == 0:
        return None, None, None
    i = min(idx, key=lambda k: ax_lines[k].distance(pt))
    line, name = ax_lines[i], ax_names[i]
    key = norm(name)
    if key not in A_tree:
        return name, None, line
    my_side, s_pt = side_of(line, pt), line.project(pt)
    cand = []
    for j in A_tree[key].query(pt, predicate="dwithin", distance=70):
        apt, n = A_by_street[key][j]
        if side_of(line, apt) != my_side:
            continue
        cand.append((abs(line.project(apt) - s_pt) + 0.3 * apt.distance(pt), n))
    if not cand:
        return name, None, line
    return name, min(cand)[1], line


# ---------- verdict for every place ----------
# status: 1 = you can park, 0 = you cannot.  reason: short text shown in the app.
out, stats, disagree = [], Counter(), []
for f in places:
    p = f["properties"]
    g = shape(f["geometry"])
    pts = list(g.geoms) if g.geom_type.startswith("Multi") else [g]
    pt = pts[0]
    name, num, _ = street_and_number(pt)
    key = norm(name) if name else None
    typres, typreg = p.get("typres") or 0, p.get("typreg") or 0
    in_poly = sector_geom.contains(pt) or sector_geom.distance(pt) < 3

    # is this spot inside the card's territory? the PDF list decides, the official polygon backs it up
    if key in CARD and num is not None:
        in_card = in_ranges(key, num)
        if in_card != in_poly:
            disagree.append((name, num, in_card, in_poly))
    elif key in CARD:
        in_card = None  # card street but no house number found: cannot decide from the PDF
    else:
        in_card = False

    if typres:
        st, why = 0, TYPRES.get(typres, "Place réservée")
    elif typreg == 0:
        st, why = 1, "Stationnement libre"
    elif typreg == 1:
        st, why = 0, "Zone rouge : carte non valable"
    elif typreg == 2:
        st, why = 0, "Zone orange : carte non valable"
    elif in_card is None:
        st, why = 2, "Numéro introuvable : vérifie sur place"
    elif in_card:
        st, why = 1, "Dans ton secteur"
    elif key in CARD:
        st, why = 0, "Hors de tes numéros"
    else:
        st, why = 0, "Hors de tes secteurs"
    stats[(st, why)] += 1
    for q in pts:
        lon, lat = to_ll(q.x, q.y)
        out.append({"y": round(lat, 6), "x": round(lon, 6), "t": st, "r": why, "n": name or "", "h": num,
                    "s": p.get("si_id"), "X": q.x, "Y": q.y})

# ---------- checks (the build fails instead of publishing something wrong) ----------
print("\nverdicts:")
for (st, why), c in sorted(stats.items(), key=lambda t: -t[1]):
    print(f"  {['non', 'OUI', '???'][st]}  {why:40s} {c}")
print(f"\nPDF ranges vs official sector shape: {len(disagree)} disagreements")
by = defaultdict(list)
for n, num, a, b in disagree:
    by[(n, a, b)].append(num)
for (n, a, b), nums in sorted(by.items(), key=lambda t: -len(t[1]))[:20]:
    print(f"   {n:32s} pdf={'in ' if a else 'out'} shape={'in ' if b else 'out'} x{len(nums):3d} numbers {sorted(set(nums))[:25]}")

ok_by_street = Counter(o["n"] for o in out if o["t"] == 1)
print("\ncard streets with no parking place marked 'oui':")
for k, v in CARD.items():
    if not any(norm(n) == k for n in ok_by_street):
        print("  ", v["name"])

checks = {
    "enough places": len(out) > 5000,
    "addresses found": len(A_by_street) > 200,
    "most card streets resolved": len(missing) <= 3,
    "sector shapes found": len(sectors) == len(CARD_SECTORS),
}
print("\nchecks:", checks)
if not all(checks.values()):
    sys.exit("checks failed: places.json not updated")

# ---------- compact output ----------
# kerb lines: places of the same street side, in order, joined while they share a verdict
groups = defaultdict(list)
for o in out:
    groups[o["s"]].append(o)
lines = []
for gid, g in groups.items():
    if len(g) > 1:
        mx = sum(o["X"] for o in g) / len(g); my = sum(o["Y"] for o in g) / len(g)
        sxx = sum((o["X"] - mx) ** 2 for o in g); syy = sum((o["Y"] - my) ** 2 for o in g)
        sxy = sum((o["X"] - mx) * (o["Y"] - my) for o in g)
        import math
        ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
        ux, uy = math.cos(ang), math.sin(ang)
        g.sort(key=lambda o: (o["X"] - mx) * ux + (o["Y"] - my) * uy)
    run = [g[0]]
    for a, b in zip(g, g[1:]):
        if b["t"] == run[-1]["t"] and math.hypot(b["X"] - a["X"], b["Y"] - a["Y"]) <= 14:
            run.append(b)
        else:
            lines.append(run); run = [b]
    lines.append(run)

R, N = [], []
ri = lambda r: R.index(r) if r in R else (R.append(r) or len(R) - 1)
ni = lambda n: N.index(n) if n in N else (N.append(n) or len(N) - 1)
P = [[o["y"], o["x"], o["t"], ri(o["r"]), ni(o["n"]), o["h"] if o["h"] is not None else -1] for o in out]
Lc = [[run[0]["t"]] + [[o["y"], o["x"]] for o in run] for run in lines]

json.dump({"built": time.strftime("%Y-%m-%d"), "ranges": {v["name"]: sorted(v["ranges"]) for v in CARD.values()},
           "R": R, "N": N, "P": P, "L": Lc}, open("places.json", "w", encoding="utf-8"),
          ensure_ascii=False, separators=(",", ":"))
print(f"\nplaces.json: {len(P)} places, {len(Lc)} kerb lines")
