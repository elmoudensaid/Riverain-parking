"""Check the address search service answers for Brussels and allows browser calls (CORS)."""
import json, urllib.parse, urllib.request
for q in ["rue leopold peret 21", "hopital brugmann", "gare du nord", "place cardinal mercier"]:
    url = ("https://photon.komoot.io/api/?q=" + urllib.parse.quote(q) +
           "&lang=fr&limit=3&lat=50.88&lon=4.325&location_bias_scale=0.5&bbox=4.24,50.76,4.49,50.92")
    req = urllib.request.Request(url, headers={"User-Agent": "riverain-parking-check", "Origin": "https://elmoudensaid.github.io"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(q, "| CORS:", r.headers.get("Access-Control-Allow-Origin"))
        for f in json.load(r)["features"]:
            p = f["properties"]
            print("   ", p.get("name"), "|", p.get("street"), p.get("housenumber"), "|", p.get("postcode"), p.get("city"), f["geometry"]["coordinates"])
