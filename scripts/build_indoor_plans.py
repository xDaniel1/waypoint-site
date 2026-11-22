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
    return (f"[out:json][timeout:60];(way[indoor]({box});way[level][shop]({box});way[level][amenity]({box});"
            f"way[highway=steps][level]({box});way[aeroway=gate]({box});"
            f"node[shop][level]({box});node[amenity][level]({box});node[aeroway=gate]({box});"
            f"way[aeroway=terminal]({box});way[building=train_station]({box});way[building=transportation]({box});"
            f"rel[aeroway=terminal]({box});rel[building=train_station]({box});rel[building=transportation]({box}););out body geom;")

REQUEST_TIMEOUT = 75

def time_left():
    return BUDGET_SECONDS - (time.time() - STARTED)

def fetch(row, col):
    body = urllib.parse.urlencode({"data": query(row, col)}).encode()
    for attempt in range(2):
        for endpoint in ENDPOINTS:
            # No new request that could run past the budget; the cell is retried next run.
            if time_left() < REQUEST_TIMEOUT:
                return None
            try:
                req = urllib.request.Request(endpoint, data=body, headers={"User-Agent": "Waypoint indoor plans (github.com/xDaniel1/waypoint-site)"})
                data = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT).read()
                check_answer(data)
                return data
            except Exception as error:
                print(f"  {endpoint.split('/')[2]}: {error}", file=sys.stderr)
        if time_left() > REQUEST_TIMEOUT + 10:
            time.sleep(5 * (attempt + 1))
    return None

# When each cell was last fetched. Kept out of the cell files, so a cell whose map data hasn't
# changed is not rewritten, so a refresh commits only the cells that changed plus this index.
# Jekyll does not publish _data/.
INDEX = OUT.parent / "_data" / "indoor_fetched.json"

def load_index():
    """{"fetched": {cell: time}, "tried": {cell: time}}; anything unreadable counts as empty."""
    try:
        raw = json.loads(INDEX.read_bytes())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    def times(name):
        part = raw.get(name)
        return {k: v for k, v in part.items() if isinstance(v, (int, float))} if isinstance(part, dict) else {}
    return {"fetched": times("fetched"), "tried": times("tried")}

def save_index(index):
    INDEX.parent.mkdir(exist_ok=True)
    body = {name: dict(sorted(index[name].items())) for name in ("fetched", "tried")}
    write_atomic(INDEX, (json.dumps(body, indent=0) + "\n").encode())

def write_atomic(path, body):
    """Write to a sibling and rename, so a failed write never leaves half a file to be committed."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(body)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

def order(cells, index):
    """Cells to work on, least recently attempted first. A run cut off by the time budget, or a
    --refresh, picks up where the last one stopped instead of starting over at the same cells, and
    a cell that keeps failing goes to the back once tried."""
    def last(cell):
        key = f"{cell[0]}_{cell[1]}"
        return max(index["fetched"].get(key, 0), index["tried"].get(key, 0))
    return sorted(cells, key=lambda cell: (last(cell), cell))

def outcome(written, failed):
    """Exit status: red when every attempted cell failed, or more than a few did, so a degraded
    rollout is visible."""
    return 1 if failed and (not written or failed > max(3, written // 10)) else 0

def slim(elements):
    """Drop what no app reads. `out body` adds every way's node ids (~20% of a cell)."""
    for e in elements:
        if isinstance(e, dict):
            e.pop("nodes", None)
    return elements

def encode(elements):
    return json.dumps({"elements": elements}, separators=(",", ":")).encode()

def is_fresh(path, fetched, now):
    """A cell is fresh if the index says it was fetched under MAX_AGE_DAYS ago and its file is a
    cell from the current query.

    File times can't be used: Actions checks the files out with new times, so every cell looked
    fresh forever. Cells never fetched into the index, or written by the old query (relations
    without members, so no outlines), are rebuilt."""
    if not isinstance(fetched, (int, float)) or now - fetched >= MAX_AGE_DAYS * 86400:
        return False
    try:
        cell = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return False
    if not isinstance(cell, dict) or not isinstance(cell.get("elements"), list):
        return False
    return all("members" in e for e in cell["elements"] if isinstance(e, dict) and e.get("type") == "relation")

def check_answer(data):
    """The elements of an Overpass answer, or ValueError when it is not a whole one.

    Overpass reports a timeout or out-of-memory as a 200 with partial results and a "remark".
    Writing that would replace a good cell with a broken one."""
    parsed = json.loads(data)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("elements"), list):
        raise ValueError("no elements list")
    if "remark" in parsed:
        raise ValueError(f"partial answer: {str(parsed['remark'])[:120]}")
    return parsed["elements"]

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
    index = load_index()
    try:
        return run(wanted, index, now)
    finally:
        save_index(index)

def run(wanted, index, now):
    written = skipped = failed = 0
    for row, col in order(wanted, index):
        names = wanted[(row, col)]
        if time_left() < REQUEST_TIMEOUT:
            print("time budget used; the next run continues from here")
            break
        path = OUT / f"indoor_{row}_{col}.json"
        key = f"{row}_{col}"
        if "--refresh" not in sys.argv and is_fresh(path, index["fetched"].get(key), now):
            skipped += 1
            continue
        index["tried"][key] = int(time.time())
        data = fetch(row, col)
        if data is None:
            failed += 1
            print(f"failed {row},{col} ({', '.join(names)})")
            continue
        elements = slim(check_answer(data))
        # Empty cells are written too, so the app's lookup is a fast 200 instead of a slow Overpass call.
        body = encode(elements)
        if not path.exists() or path.read_bytes() != body:
            write_atomic(path, body)
        index["fetched"][key] = int(time.time())
        written += 1
        print(f"{row},{col}: {len(elements)} elements ({', '.join(names)})")
        time.sleep(1.5)
    print(f"written {written}, fresh {skipped}, failed {failed}")
    return outcome(written, failed)

if __name__ == "__main__":
    sys.exit(main())
