"""One-off exploration of the official open data services (run in GitHub Actions)."""
import collections, json, re, subprocess, urllib.parse, urllib.request

UA = {"User-Agent": "riverain-parking-explore"}
PB = "https://ows.parking.brussels/ws/ParkingBrussels/ows"
GN = "https://geobru-geonetwork.irisnet.be/geonetwork/srv/api/records/"


def get(url, raw=False):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        b = r.read()
        return b if raw else b.decode("utf-8", "replace")


def wfs(base, layer, cql=None):
    q = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": layer,
         "outputFormat": "application/json", "srsName": "EPSG:4326"}
    if cql:
        q["CQL_FILTER"] = cql
    return json.loads(get(base + "?" + urllib.parse.urlencode(q)))


# 1) technical specifications (code tables) attached to the catalogue records
for rec in ["e0396d91-4f11-11ef-b5dd-f020ff9afd75", "3a549366-4a95-11ef-9cb4-f020ff9afd75"]:
    print("=" * 90, "\nRECORD", rec)
    try:
        lst = json.loads(get(GN + rec + "/attachments"))
        for a in lst:
            url = a.get("url") or ""
            print("  attachment:", url)
            if "FR" in url.rsplit("/", 1)[-1] and url.endswith(".pdf"):
                open("/tmp/spec.pdf", "wb").write(get(url, raw=True))
                print(subprocess.run(["pdftotext", "-layout", "/tmp/spec.pdf", "-"], capture_output=True, text=True).stdout)
    except Exception as e:
        print("ERROR", e)

# 2) parking places in Jette: which codes occur
print("=" * 90, "\nON_STREET JETTE")
try:
    fs = wfs(PB, "ParkingBrussels:on_street", "muniscode='21010'")["features"]
    print("places:", len(fs))
    for k in ["typres", "typreg", "situation", "orientation", "evp", "code_metier", "md_id"]:
        print(f"  {k}: {collections.Counter(f['properties'].get(k) for f in fs).most_common(25)}")
except Exception as e:
    print("ERROR", e)

# 3) official sectors for Jette and the Florair border extension
print("=" * 90, "\nSECTORS")
for layer in ["ParkingBrussels:permit_secteurs_mailles", "ParkingBrussels:permit_limitrophes"]:
    try:
        for f in wfs(PB, layer)["features"]:
            txt = json.dumps(f["properties"], ensure_ascii=False)
            if re.search(r"JET|Jette|FLORAIR|Florair|ESSEG|THEOD|BAUD", txt, re.I):
                print(" ", layer.split(":")[1], txt[:300])
    except Exception as e:
        print("ERROR", e)
