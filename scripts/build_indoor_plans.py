#!/usr/bin/env python3
"""Pre-builds OpenStreetMap indoor plans for every venue in Waypoint's `venues` table.

Reads the table over Supabase's public REST endpoint, covers each venue's radius with the app's
0.006-degree map cells, runs the same Overpass query the app runs, and writes one JSON file per
cell into indoor/ — which GitHub Pages serves at
https://xdaniel1.github.io/waypoint-site/indoor/indoor_<row>_<col>.json, the first place the app
looks after its bundle. Run nightly by .github/workflows/indoor-plans.yml.

Environment: SUPABASE_URL, SUPABASE_ANON_KEY (the anon key is public by design; RLS only lets it read).
"""
import json, math, os, pathlib, signal, sys, time, urllib.parse, urllib.request

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
    with hard_limit(60):  # a hung startup must not eat the night's budget
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

class BudgetSpent(Exception):
    """Raised before the first request of a cell when the run has no time left for one."""

def read_capped(response, seconds):
    """The whole body, or TimeoutError once `seconds` have passed in total. urlopen's timeout is
    per blocking read, so a server that keeps trickling bytes would otherwise never time out."""
    deadline = time.monotonic() + seconds
    parts = []
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"answer took over {seconds} s")
        chunk = response.read(64 * 1024)
        if not chunk:
            return b"".join(parts)
        parts.append(chunk)

class hard_limit:
    """Raises TimeoutError in the main thread after `seconds`, wherever the request is blocked:
    connecting, waiting for headers or inside a read. The socket timeout only bounds each wait."""
    def __init__(self, seconds):
        self.seconds = seconds
    def __enter__(self):
        def expire(signum, frame):
            raise TimeoutError(f"request took over {self.seconds} s")
        self.old = signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
    def __exit__(self, *exc):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.old)
        return False

def fetch(row, col):
    """The cell's Overpass answer, or None when every server failed. Raises BudgetSpent if there was
    no time for the next request, so the cell is not counted as tried."""
    body = urllib.parse.urlencode({"data": query(row, col)}).encode()
    for attempt in range(2):
        for endpoint in ENDPOINTS:
            # No new request that could run past the budget; the cell is retried next run.
            if time_left() < REQUEST_TIMEOUT:
                # Out of time, not a failed cell: it keeps its place for the next run.
                raise BudgetSpent()
            try:
                req = urllib.request.Request(endpoint, data=body, headers={"User-Agent": "Waypoint indoor plans (github.com/xDaniel1/waypoint-site)"})
                with hard_limit(REQUEST_TIMEOUT):
                    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                        data = read_capped(response, REQUEST_TIMEOUT)
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
        return {k: v for k, v in part.items() if is_time(v)} if isinstance(part, dict) else {}
    return {"fetched": times("fetched"), "tried": times("tried")}

def is_time(v):
    """A real time stamp: a finite number, not NaN or infinity (which JSON decoding accepts)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

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

def stuck_cells(index, week=7 * 86400):
    """Cells tried more than a week after their last successful fetch (or never fetched)."""
    return sorted(k for k, t in index["tried"].items() if t - index["fetched"].get(k, 0) > week)

def outcome(written, failed):
    """Exit status: red when failures match or outnumber successes, or more than a few cells failed,
    so a degraded rollout is visible."""
    return 1 if failed and (failed >= written or failed > max(3, written // 10)) else 0

def point(p):
    return isinstance(p, dict) and all(isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool) and math.isfinite(p[k]) for k in ("lat", "lon"))

def runs(geometry):
    """The stretches of a way between points Overpass couldn't place (it writes null for them)."""
    out, run = [], []
    for p in geometry:
        if point(p):
            run.append(p)
        else:
            if len(run) >= 2:
                out.append(run)
            run = []
    if len(run) >= 2:
        out.append(run)
    return out

def slim(elements):
    """What the apps read, in a shape every build can decode.

    Drops the node id lists `out body` adds to every way (~20% of a cell). Overpass writes null
    for a point it can't place, and iPhone builds decode geometry as a list of points, so one null
    would fail the whole cell: a relation member is split into its placed stretches, and a way or
    node with an unplaceable point is left out."""
    out = []
    for e in elements:
        e.pop("nodes", None)
        kind = e.get("type")
        if kind == "node" and ("lat" in e or "lon" in e) and not point(e):
            continue
        if kind == "way" and "geometry" in e:
            g = e["geometry"]
            if not isinstance(g, list) or not all(point(p) for p in g):
                continue
        if kind == "relation" and isinstance(e.get("members"), list):
            members = []
            for m in e["members"]:
                g = m.get("geometry") if isinstance(m, dict) else None
                if not isinstance(m, dict):
                    continue
                if g is None or (isinstance(g, list) and all(point(p) for p in g)):
                    members.append(m)
                elif isinstance(g, list):
                    members.extend({**m, "geometry": r} for r in runs(g))
            e["members"] = members
        out.append(e)
    return out

def encode(elements):
    return json.dumps({"elements": elements}, separators=(",", ":")).encode()

def is_fresh(path, fetched, now):
    """A cell is fresh if the index says it was fetched under MAX_AGE_DAYS ago and its file is a
    cell from the current query.

    File times can't be used: Actions checks the files out with new times, so every cell looked
    fresh forever. Cells never fetched into the index, or written by the old query (relations
    without members, so no outlines), are rebuilt."""
    if not is_time(fetched) or fetched > now or now - fetched >= MAX_AGE_DAYS * 86400:
        return False
    try:
        cell = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return False
    if not isinstance(cell, dict) or not isinstance(cell.get("elements"), list):
        return False
    return all(has_members(e) for e in cell["elements"])

def check_answer(data):
    """The elements of an Overpass answer, or ValueError when it is not a whole one.

    Overpass reports a timeout or out-of-memory as a 200 with partial results and a "remark".
    Writing that would replace a good cell with a broken one."""
    parsed = json.loads(data)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("elements"), list):
        raise ValueError("no elements list")
    if "remark" in parsed:
        raise ValueError(f"partial answer: {str(parsed['remark'])[:120]}")
    if not all(well_formed(e) for e in parsed["elements"]):
        raise ValueError("an element is not a node, way or relation with an id")
    if not all(has_members(e) for e in parsed["elements"]):
        raise ValueError("a relation came back without its members")
    return parsed["elements"]

def well_formed(element):
    """An element the apps' strict decoders accept: a node, way or relation with a numeric id,
    string tags, and members whose role is a string."""
    if not (isinstance(element, dict) and element.get("type") in ("node", "way", "relation")
            and isinstance(element.get("id"), int) and not isinstance(element.get("id"), bool)):
        return False
    tags = element.get("tags")
    if tags is not None and not (isinstance(tags, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items())):
        return False
    members = element.get("members")
    if isinstance(members, list):
        for m in members:
            if not isinstance(m, dict) or ("role" in m and m["role"] is not None and not isinstance(m["role"], str)):
                return False
    return True

def has_members(element):
    """Relations must carry a member list, which the old `out tags` query left out. Members
    without usable points are allowed: that is how the map data is, every retry would get the
    same, and rejecting it would keep the whole cell from ever refreshing. The apps skip them."""
    if not isinstance(element, dict):
        return False
    return element.get("type") != "relation" or isinstance(element.get("members"), list)

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
        try:
            data = fetch(row, col)
        except BudgetSpent:
            print("time budget used; the next run continues from here")
            if not written and not failed:
                print("no cell was tried in this run")
                return 1
            break
        index["tried"][key] = int(time.time())
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
        if written % 25 == 0:
            save_index(index)  # a hard-killed job keeps most of its progress
        print(f"{row},{col}: {len(elements)} elements ({', '.join(names)})")
        time.sleep(1.5)
    print(f"written {written}, fresh {skipped}, failed {failed}")
    stuck = stuck_cells(index)
    if stuck:
        # Cells that keep failing (often one too heavy for Overpass) keep their old file; say so.
        print(f"::warning::{len(stuck)} cells have not refreshed in a week of tries: {', '.join(stuck[:20])}")
    return outcome(written, failed)

if __name__ == "__main__":
    sys.exit(main())
