"""One-off exploration of the official open data services (run in GitHub Actions)."""
import re, sys, urllib.request, json

UA = {"User-Agent": "riverain-parking-explore"}
CAPS = [
    "https://data.parking.brussels/geoserver/ParkingBrussels/ows?service=WFS&request=GetCapabilities",
    "https://geodata.parking.brussels/geoserver/ParkingBrussels/ows?service=WFS&request=GetCapabilities",
    "https://geodata.parking.brussels/geoserver/ows?service=WFS&request=GetCapabilities",
    "https://ows.parking.brussels/ws/ParkingBrussels/ows?service=WFS&request=GetCapabilities",
    "https://data.mobility.brussels/geoserver/bm_parking/wfs?service=WFS&request=GetCapabilities",
    "https://geoservices-urbis.irisnet.be/geoserver/UrbisAdm/wfs?service=WFS&request=GetCapabilities",
    "https://geoservices-urbis.irisnet.be/geoserver/urbisvector/wfs?service=WFS&request=GetCapabilities",
]


def get(url, n=None):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as r:
        b = r.read() if n is None else r.read(n)
        return r.geturl(), b.decode("utf-8", "replace")


for u in CAPS:
    print("=" * 100, "\n", u)
    try:
        final, txt = get(u)
        print("final url:", final, "bytes:", len(txt))
        names = re.findall(r"<(?:wfs:)?FeatureType\b[^>]*>\s*<(?:wfs:)?Name>(.*?)</(?:wfs:)?Name>\s*<(?:wfs:)?Title>(.*?)</", txt, re.S)
        for n, t in names:
            print(f"  {n:60s} {t}")
        if not names:
            print(txt[:300])
    except Exception as e:
        print("ERROR", e)

# sample a few features from candidate layers
SAMPLES = sys.argv[1:]
for spec in SAMPLES:
    base, layer = spec.split("|")
    url = f"{base}?service=WFS&version=2.0.0&request=GetFeature&typeNames={layer}&count=3&outputFormat=application/json&srsName=EPSG:4326"
    print("=" * 100, "\nSAMPLE", layer)
    try:
        _, txt = get(url)
        j = json.loads(txt)
        print("numberMatched:", j.get("numberMatched"), "totalFeatures:", j.get("totalFeatures"))
        for f in j.get("features", [])[:3]:
            g = f.get("geometry") or {}
            print("  geom:", g.get("type"), "props:", json.dumps(f.get("properties"), ensure_ascii=False)[:900])
    except Exception as e:
        print("ERROR", e, txt[:500] if 'txt' in dir() else "")
