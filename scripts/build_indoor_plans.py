#!/usr/bin/env python3
"""Pre-builds OpenStreetMap indoor plans for every venue in Waypoint's `venues` table.

Reads the table over Supabase's public REST endpoint, covers each venue's radius with the app's
0.006-degree map cells, runs the same Overpass query the app runs (its airport form, with the
landside clauses), trims each answer the way the app's bundle is trimmed, and writes one JSON file per
cell into indoor/ — which GitHub Pages serves at
https://xdaniel1.github.io/waypoint-site/indoor/indoor_<row>_<col>.json, the first place the app
looks after its bundle. Run nightly by .github/workflows/indoor-plans.yml.

Then every venue's cells get the app bundle's two fixes (scripts/kova_indoor/indoor_enrich.py):
gates with no floor take the one their neighbouring gates agree on, and unnamed shop units are
named from Overture Maps Places (permissive licences only; needs `duckdb`, else names are left).

scripts/kova_indoor/ is a verbatim copy of the iOS repo's Scripts/fetch_indoor_venues.py,
indoor_inventory.py and indoor_enrich.py (query, trimming, enrichment): copy them again when
those change, so the CDN and the bundle stay one format.

Environment: SUPABASE_URL, SUPABASE_ANON_KEY (the anon key is public by design; RLS only lets it read).
"""
import datetime, json, math, os, pathlib, signal, sys, time, urllib.parse, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "kova_indoor"))
import fetch_indoor_venues as fv  # noqa: E402  (query, trimming: the iOS bundle's)
import indoor_enrich  # noqa: E402

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
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/venues?select=short_name,latitude,longitude,radius_m,sources,kind&enabled=eq.true"
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
    """The app's query (IndoorMapService.overpassQuery, airport form) for the cell's box."""
    return fv.query(fv.cell_box(row, col), timeout=60)

# Written into every cell this publisher builds: the cell answers the airport (landside) query, so
# the app doesn't ask Overpass for its garages again, and it is in the current format (a cell
# without it came from the old query and is rebuilt).
GENERATOR = "waypoint-site scripts/build_indoor_plans.py"

def trim(elements, row, col):
    """The cell as the app's bundle keeps it (fetch_indoor_venues.write): only the tags the apps
    read, no clutter, coordinates to 6 decimals, elements touching the cell's box, outlines first."""
    box = fv.cell_box(row, col)
    kept = [c for c in (fv.compact(e) for e in elements) if c and fv.touches(c, box)]
    kept.sort(key=lambda c: ({"relation": 0, "way": 1, "node": 2}[c["type"]], c["id"]))
    return kept

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

CURRENT = 86400  # a replica more than a day behind is stalled; ask the others first

def fetch(row, col, not_before=None):
    """The cell's Overpass answer, or None when every server failed. Raises BudgetSpent if there was
    no time for the next request and nothing usable came back, so the cell is not counted as tried.

    An answer from a replica more than a day behind is kept only as a fallback while the other
    servers are asked: a stalled replica would otherwise mark old data fresh for 14 days."""
    body = urllib.parse.urlencode({"data": query(row, col)}).encode()
    fallback, fallback_base = None, ""
    for attempt in range(2):
        for endpoint in ENDPOINTS:
            # No new request that could run past the budget; the cell is retried next run.
            if time_left() < REQUEST_TIMEOUT:
                if fallback:
                    return fallback
                # Out of time, not a failed cell: it keeps its place for the next run.
                raise BudgetSpent()
            try:
                req = urllib.request.Request(endpoint, data=body, headers={"User-Agent": "Waypoint indoor plans (github.com/xDaniel1/waypoint-site)"})
                with hard_limit(REQUEST_TIMEOUT):
                    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                        data = read_capped(response, REQUEST_TIMEOUT)
                    check_answer(data)
                    base = snapshot_of(data)
                    # A mirror lagging behind the snapshot we already published would roll the cell back.
                    if not_before and (not base or base < not_before):
                        raise ValueError(f"older snapshot ({base} < {not_before})")
                age = snapshot_age(base)
                if age is not None and age <= CURRENT:
                    return data
                print(f"  {endpoint.split('/')[2]}: replica behind ({base}); asking the others", file=sys.stderr)
                if base and base > fallback_base:
                    fallback, fallback_base = data, base
                elif not fallback:
                    fallback = data
            except Exception as error:
                print(f"  {endpoint.split('/')[2]}: {error}", file=sys.stderr)
        if fallback:
            return fallback  # every server asked; the most recent of the lagging answers
        if time_left() > REQUEST_TIMEOUT + 10:
            time.sleep(5 * (attempt + 1))
    return None

# When each cell was last fetched and tried, and the OpenStreetMap snapshot it was last confirmed
# against. Jekyll does not publish _data/.
INDEX = OUT.parent / "_data" / "indoor_fetched.json"

def load_index():
    """{"fetched": {cell: time}, "tried": {cell: time}, "osm": {cell: snapshot}}; anything
    unreadable counts as empty. "osm" is the snapshot the cell's data was last confirmed against."""
    try:
        raw = json.loads(INDEX.read_bytes())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    def times(name):
        part = raw.get(name)
        return {k: v for k, v in part.items() if is_time(v)} if isinstance(part, dict) else {}
    osm = raw.get("osm")
    osm = {k: v for k, v in osm.items() if is_snapshot(v)} if isinstance(osm, dict) else {}
    return {"fetched": times("fetched"), "tried": times("tried"), "first_tried": times("first_tried"), "osm": osm}

def parse_snapshot(v):
    """The time of an Overpass timestamp_osm_base such as 2026-11-22T07:20:03Z, or None."""
    if not (isinstance(v, str) and len(v) == 20):
        return None
    try:
        return datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None

def is_snapshot(v):
    """A real snapshot time, not in the future (an impossible one would block every later answer).
    In this fixed format it compares correctly as text."""
    t = parse_snapshot(v)
    return t is not None and t.timestamp() <= time.time() + 3600

def snapshot_age(v):
    t = parse_snapshot(v)
    return None if t is None else time.time() - t.timestamp()

def snapshot_of(data):
    """The OpenStreetMap snapshot an Overpass answer was made from, or None."""
    try:
        base = json.loads(data).get("osm3s", {}).get("timestamp_osm_base")
    except (ValueError, AttributeError):
        return None
    return base if is_snapshot(base) else None

def is_time(v):
    """A real time stamp: a finite number, not NaN or infinity (which JSON decoding accepts)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

def save_index(index):
    INDEX.parent.mkdir(exist_ok=True)
    body = {name: dict(sorted(index.get(name, {}).items())) for name in ("fetched", "tried", "first_tried", "osm")}
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

def stuck_cells(index, wanted=None, week=7 * 86400):
    """Cells still wanted that were tried more than a week after their last successful fetch."""
    keep = None if wanted is None else {f"{r}_{c}" for r, c in wanted}
    # A cell never fetched counts from its first try, not from 1970.
    first = index.get("first_tried", {})
    return sorted(k for k, t in index["tried"].items()
                  if (keep is None or k in keep) and t - index["fetched"].get(k, first.get(k, t)) > week)

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

def encode(elements, osm_base=None):
    """The cell file. `osm_base` is the snapshot it was last confirmed against, so the web can prefer
    the more recently confirmed of two cells holding one station."""
    cell = {"landside": True, "generator": GENERATOR, "elements": elements}
    if osm_base:
        cell["osm_base"] = osm_base
    return json.dumps(cell, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()

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
    # Built by the old query (no landside, repeat_on, lift or shop=mall clauses): rebuild it.
    if cell.get("landside") is not True:
        return False
    return all(has_members(e) for e in cell["elements"])

def check_answer(data):
    """The elements of an Overpass answer, or ValueError when it is not a whole one.

    Overpass reports a timeout or out-of-memory as a 200 with partial results and a "remark".
    Writing that would replace a good cell with a broken one."""
    def no_constants(name):  # NaN and Infinity aren't JSON; the apps' decoders reject them
        raise ValueError(f"{name} in answer")
    parsed = json.loads(data, parse_constant=no_constants)
    json.dumps(parsed, allow_nan=False)  # a number too big for a float (1e999) reads as inf: reject it
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
            and isinstance(element.get("id"), int) and not isinstance(element.get("id"), bool)
            and -2**63 <= element["id"] < 2**63):  # iOS decodes ids as Int64
        return False
    tags = element.get("tags")
    if tags is not None and not (isinstance(tags, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items())):
        return False
    members = element.get("members")
    if members is not None and not isinstance(members, list):
        return False
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
    wanted, listed = {}, []
    for venue in venues():
        if "osm" not in (venue.get("sources") or ["osm"]):
            continue
        listed.append(venue)
        for cell in cells_for(venue["latitude"], venue["longitude"], venue["radius_m"]):
            wanted.setdefault(cell, []).append(venue["short_name"])
    print(f"{len(wanted)} cells across {len(set(sum(wanted.values(), [])))} venues")
    now = time.time()
    index = load_index()
    try:
        status = run(wanted, index, now)
    finally:
        save_index(index)
    enrich(listed)
    return status

# --- the bundle's fixes, per venue ---------------------------------------------------------

# Overture answers per venue, so a nightly run asks S3 again only for a new release or a moved
# venue. Jekyll does not publish _data/.
OVERTURE_CACHE = OUT.parent / "_data" / "overture_places.json"

def venue_box(venue):
    lat, lon, r = venue["latitude"], venue["longitude"], venue["radius_m"]
    dlat, dlon = r / 111_320, r / (111_320 * math.cos(math.radians(lat)))
    return [round(lat - dlat, 6), round(lon - dlon, 6), round(lat + dlat, 6), round(lon + dlon, 6)]

def load_json(path):
    try:
        data = json.loads(path.read_bytes())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def overture_for(venue, cache, release):
    """The venue's Overture rows: cached for this release and box, else fetched. On a failed fetch
    the last cached rows (any release) are used; with none, None (names left as they are)."""
    name, box = venue["short_name"], venue_box(venue)
    hit = cache.get(name)
    if isinstance(hit, dict) and hit.get("release") == release and hit.get("box") == box:
        return [tuple(r) for r in hit.get("places", [])]
    try:
        rows = indoor_enrich.overture_places(tuple(box))
    except Exception as error:  # no duckdb, S3 down, the release withdrawn
        print(f"::warning::Overture places for {name}: {str(error)[:160]}")
        return [tuple(r) for r in hit["places"]] if isinstance(hit, dict) and isinstance(hit.get("places"), list) else None
    cache[name] = {"release": release, "box": box, "places": [list(r) for r in rows]}
    return rows

def enrich(listed, overture=None):
    """Gate floors and Overture names over every listed venue's cells on disk, written back only
    when they change. A failure here never fails the run: the cells are still valid without it."""
    overture = os.environ.get("INDOOR_OVERTURE", "1") != "0" if overture is None else overture
    cache = load_json(OVERTURE_CACHE) if overture else {}
    release = indoor_enrich.OVERTURE_RELEASE
    changed = 0
    for venue in listed:
        try:
            lat, lon, r = venue["latitude"], venue["longitude"], venue["radius_m"]
            data = {}
            for row, col in sorted(cells_for(lat, lon, r)):
                path = OUT / f"indoor_{row}_{col}.json"
                cell = load_json(path)
                if isinstance(cell.get("elements"), list) and cell.get("landside") is True:
                    data[(row, col)] = cell  # only cells in the current format
            if not data:
                continue
            places = overture_for(venue, cache, release) if overture else None
            within = lambda la, lo, c=(lat, lon), r=r: indoor_enrich.metres(c, (la, lo)) <= r
            before = {rc: encode(d["elements"], d.get("osm_base")) for rc, d in data.items()}
            gates, suspect, names = indoor_enrich.enrich_cells(data, venue["short_name"], venue.get("kind") == "airport",
                                                               places, within)
            for rc, d in data.items():
                body = encode(d["elements"], d.get("osm_base"))
                if body != before[rc]:
                    write_atomic(OUT / f"indoor_{rc[0]}_{rc[1]}.json", body)
                    changed += 1
            if gates or names:
                print(f"{venue['short_name']}: {len(gates)} gates floored from neighbours, {len(names)} units named from Overture")
            for gid, label, f, v in suspect:
                print(f"  {venue['short_name']}: gate {label or gid} (node {gid}) is on {f}, every neighbour on {v}")
        except Exception as error:
            print(f"::warning::enriching {venue.get('short_name')}: {str(error)[:160]}")
    if overture:
        try:
            OVERTURE_CACHE.parent.mkdir(exist_ok=True)
            write_atomic(OVERTURE_CACHE, json.dumps(cache, sort_keys=True, ensure_ascii=False).encode())
        except OSError as error:
            print(f"::warning::Overture cache not saved: {error}")
    print(f"enriched: {changed} cells changed")
    return changed

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
            data = fetch(row, col, index["osm"].get(key))
        except BudgetSpent:
            print("time budget used; the next run continues from here")
            if not written and not failed:
                print("no cell was tried in this run")
                return 1
            break
        index["tried"][key] = int(time.time())
        index.setdefault("first_tried", {}).setdefault(key, index["tried"][key])
        if data is None:
            failed += 1
            print(f"failed {row},{col} ({', '.join(names)})")
            continue
        elements = trim(slim(check_answer(data)), row, col)
        base = snapshot_of(data)
        # Empty cells are written too, so the app's lookup is a fast 200 instead of a slow Overpass call.
        # The cell and the guard both carry the snapshot this answer confirmed, changed or not: a cell
        # that stayed the same has still been checked against newer data than a neighbour that changed
        # earlier, and no mirror older than this check may replace it. (So a refreshed cell is
        # rewritten every 14 days even when its map data didn't change.)
        body = encode(elements, base)
        if not path.exists() or path.read_bytes() != body:
            write_atomic(path, body)
        if base:
            index["osm"][key] = base
        age = snapshot_age(base)
        if age is not None and age <= CURRENT:
            index["fetched"][key] = int(time.time())
        else:
            # Data from a lagging server is better than none, but doesn't count as a refresh: the
            # cell stays due and is asked for again next run, when a current server may answer.
            print(f"  {row},{col}: kept a lagging answer ({base}); due again next run", file=sys.stderr)
        written += 1
        if written % 25 == 0:
            save_index(index)  # a hard-killed job keeps most of its progress
        print(f"{row},{col}: {len(elements)} elements ({', '.join(names)})")
        time.sleep(1.5)
    print(f"written {written}, fresh {skipped}, failed {failed}")
    stuck = stuck_cells(index, wanted)
    if stuck:
        # Cells that keep failing (often one too heavy for Overpass) keep their old file; say so.
        print(f"::warning::{len(stuck)} cells have not refreshed in a week of tries: {', '.join(stuck[:20])}")
    return outcome(written, failed)

if __name__ == "__main__":
    sys.exit(main())
