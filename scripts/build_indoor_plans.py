#!/usr/bin/env python3
"""Pre-builds OpenStreetMap indoor plans for every venue in Waypoint's `venues` table.

Reads the table over Supabase's public REST endpoint, covers each venue's radius with the app's
0.006-degree map cells, runs the same Overpass query the app runs, and writes one JSON file per
cell into indoor/ — which GitHub Pages serves at
https://xdaniel1.github.io/waypoint-site/indoor/indoor_<row>_<col>.json, the first place the app
looks after its bundle. Run nightly by .github/workflows/indoor-plans.yml.

Environment: SUPABASE_URL, SUPABASE_ANON_KEY (the anon key is public by design; RLS only lets it read).
"""
import json, math, os, pathlib, sys, time, urllib.parse, urllib.request

CELL, MARGIN = 0.006, 0.002
OUT = pathlib.Path(__file__).resolve().parent.parent / "indoor"
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
             "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
MAX_AGE_DAYS = 14
# A hosted job dies at 6 hours and loses everything uncommitted, so stop early and let the next
# night carry on: cells already written count as fresh and are skipped.
BUDGET_SECONDS = float(os.environ.get("INDOOR_BUDGET_MIN", "240")) * 60
STARTED = time.time()

def venues():
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/venues?select=short_name,latitude,longitude,radius_m,sources&enabled=eq.true"
    key = os.environ["SUPABASE_ANON_KEY"]
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    return json.load(urllib.request.urlopen(req, timeout=30))

def cells_for(lat, lon, radius_m):
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    rows = range(math.floor((lat - dlat) / CELL), math.floor((lat + dlat) / CELL) + 1)
    cols = range(math.floor((lon - dlon) / CELL), math.floor((lon + dlon) / CELL) + 1)
    return {(r, c) for r in rows for c in cols}

def query(row, col):
    s, w = row * CELL - MARGIN, col * CELL - MARGIN
    box = f"{s},{w},{s + CELL + 2 * MARGIN},{w + CELL + 2 * MARGIN}"
    return (f"[out:json][timeout:25];(way[indoor]({box});way[level][shop]({box});way[level][amenity]({box});"
            f"way[highway=steps][level]({box});way[aeroway=gate]({box});"
            f"node[shop][level]({box});node[amenity][level]({box});node[aeroway=gate]({box});"
            f"way[aeroway=terminal]({box});way[building=train_station]({box});way[building=transportation]({box});"
            f"rel[aeroway=terminal]({box});rel[building=train_station]({box});rel[building=transportation]({box}););out tags geom;")

def fetch(row, col):
    body = urllib.parse.urlencode({"data": query(row, col)}).encode()
    for attempt in range(2):
        for endpoint in ENDPOINTS:
            try:
                req = urllib.request.Request(endpoint, data=body, headers={"User-Agent": "Waypoint indoor plans (github.com/xDaniel1/waypoint-site)"})
                data = urllib.request.urlopen(req, timeout=45).read()
                json.loads(data)["elements"]
                return data
            except Exception as error:
                print(f"  {endpoint.split('/')[2]}: {error}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    return None

def main():
    OUT.mkdir(exist_ok=True)
    wanted = {}
    for venue in venues():
        if "osm" not in (venue.get("sources") or ["osm"]):
            continue
        for cell in cells_for(venue["latitude"], venue["longitude"], venue["radius_m"]):
            wanted.setdefault(cell, []).append(venue["short_name"])
    print(f"{len(wanted)} cells across {len(set(sum(wanted.values(), [])))} venues")
    now = time.time()
    written = skipped = failed = 0
    for (row, col), names in sorted(wanted.items()):
        if time.time() - STARTED > BUDGET_SECONDS:
            print("time budget used; the next run continues from here")
            break
        path = OUT / f"indoor_{row}_{col}.json"
        if path.exists() and now - path.stat().st_mtime < MAX_AGE_DAYS * 86400 and "--refresh" not in sys.argv:
            skipped += 1
            continue
        data = fetch(row, col)
        if data is None:
            failed += 1
            print(f"failed {row},{col} ({', '.join(names)})")
            continue
        elements = json.loads(data)["elements"]
        # Empty cells are written too, so the app's lookup is a fast 200 instead of a slow Overpass call.
        path.write_bytes(json.dumps({"elements": elements}, separators=(",", ":")).encode())
        written += 1
        print(f"{row},{col}: {len(elements)} elements ({', '.join(names)})")
        time.sleep(1.5)
    print(f"written {written}, fresh {skipped}, failed {failed}")
    return 1 if failed and not written else 0

if __name__ == "__main__":
    sys.exit(main())
