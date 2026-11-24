#!/usr/bin/env python3
"""Pre-builds OpenStreetMap indoor plans for every venue in Waypoint's `venues` table.

Reads the table over Supabase's public REST endpoint, covers each venue's radius with the app's
0.006-degree map cells, runs the same Overpass query the app runs (its airport form, with the
landside clauses), trims each answer the way the app's bundle is trimmed, and writes one JSON file per
cell into indoor/ — which GitHub Pages serves at
https://xdaniel1.github.io/waypoint-site/indoor/indoor_<row>_<col>.json, the first place the app
looks after its bundle. Run nightly by .github/workflows/indoor-plans.yml.

Then every venue's cells get the app bundle's fixes, as the iOS repo's fetch_indoor_venues.py
runs them: scripts/kova_indoor/indoor_enrich.py over all venues at once (gates floored from their
ways and neighbouring gates; unnamed shop units named, and store points added in malls and
stations, from Overture Maps Places: permissive licences only, needs `duckdb`, else names are
left), then indoor_prune.py (a floor with no named place on it is taken off, so it gets no floor
button). OSM and Overture data only.

scripts/kova_indoor/ is a verbatim copy of the iOS repo's Scripts/fetch_indoor_venues.py,
indoor_inventory.py, indoor_enrich.py and indoor_prune.py (+ tests): query, trimming, enrichment,
pruning. Copy them again when those change, so the CDN and the bundle stay one format.

Environment: SUPABASE_URL, SUPABASE_ANON_KEY (the anon key is public by design; RLS only lets it read).
"""
import datetime, json, math, os, pathlib, signal, sys, time, urllib.parse, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "kova_indoor"))
import fetch_indoor_venues as fv  # noqa: E402  (query, trimming: the iOS bundle's)
import indoor_enrich  # noqa: E402
import indoor_prune  # noqa: E402

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
    try:
        enrich(listed)
    except Exception as error:  # the cells are valid without the fixes; never fail the run for them
        print(f"::warning::enriching: {str(error)[:300]}")
    return status

# --- the bundle's fixes, over every venue at once ------------------------------------------

# Overture answers per venue box, so a nightly run asks S3 again only for a new release, a new
# row shape or a moved venue. Jekyll does not publish _data/.
OVERTURE_CACHE = OUT.parent / "_data" / "overture_places.json"
# The shape of indoor_enrich.overture_places' rows. Bump it when they change: rows cached in an
# older shape are fetched again. 2: (id, name, category, lat, lon, address); pass 4 reads stated
# floors from the address, which rows cached before it lack.
OVERTURE_SCHEMA = 2
OVERTURE_ROW = 6

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

def venue_areas(listed):
    """The listed venues as indoor_inventory.venue_areas() gives the bundle's:
    name -> (kind, inside(lat, lon), [cells], box (s, w, n, e))."""
    out = {}
    for v in listed:
        lat, lon, r = v["latitude"], v["longitude"], v["radius_m"]
        inside = (lambda c=(lat, lon), r=r: lambda la, lo: indoor_enrich.metres(c, (la, lo)) <= r)()
        out[v["short_name"]] = (v.get("kind") or "other", inside, sorted(cells_for(lat, lon, r)), tuple(venue_box(v)))
    return out

def box_key(box):
    return ",".join(repr(float(x)) for x in box)

class OvertureMissing(Exception):
    """A venue's Overture places could not be fetched and none are cached in the current shape."""

# Past this many minutes into the job no Overture box is fetched (a cached one is still used):
# the hosted job dies at 330, and the cells are written only once every venue is worked out.
OVERTURE_DEADLINE = float(os.environ.get("INDOOR_OVERTURE_DEADLINE_MIN", "300")) * 60
OVERTURE_FETCH_LIMIT = 900  # one box's query; a hung S3 read must not eat the job

def cached_places(cache, release, fetch, saved=lambda: None):
    """indoor_enrich.overture_places, answered from `cache` (changed in place, `saved` called after
    each new fetch) for this release, row shape and box. On a failed fetch, or past the deadline,
    the box's cached rows from another release are used; with none, OvertureMissing. Each box's
    answer is kept for the run, so S3 is asked at most once per box."""
    answers = {}
    def places(box, path=None):
        key = box_key(box)
        if key not in answers:
            try:
                answers[key] = lookup(box, key, path)
            except OvertureMissing as error:
                answers[key] = error
        if isinstance(answers[key], OvertureMissing):
            raise answers[key]
        return list(answers[key])
    def lookup(box, key, path):
        hit = cache.get(key)
        usable = (isinstance(hit, dict) and hit.get("schema") == OVERTURE_SCHEMA and hit.get("box") == [float(x) for x in box]
                  and isinstance(hit.get("places"), list)
                  and all(isinstance(r, list) and len(r) == OVERTURE_ROW for r in hit["places"]))
        if usable and hit.get("release") == release:
            return [tuple(r) for r in hit["places"]]
        try:
            left = OVERTURE_DEADLINE - (time.time() - STARTED)
            if left <= 0:
                raise TimeoutError("no time left in the job to ask S3")
            # A query started just before the deadline may still run past it, but not by much.
            limit, began = min(OVERTURE_FETCH_LIMIT, left + 600), time.monotonic()
            try:
                with hard_limit(limit):
                    rows = [tuple(r) for r in fetch(tuple(box), path)]
            except KeyboardInterrupt:
                # duckdb reports the time limit's signal as an interrupt; a real cancel goes on up.
                if time.monotonic() - began < limit - 1:
                    raise
                raise TimeoutError(f"query took over {limit:.0f} s") from None
            odd = next((r for r in rows if len(r) != OVERTURE_ROW), None)
            if odd is not None:
                raise ValueError(f"a row of {len(odd)} values, expected {OVERTURE_ROW} (bump OVERTURE_SCHEMA)")
        # No duckdb, S3 down, the release withdrawn, the time limit.
        except Exception as error:
            if usable:
                print(f"::warning::Overture places for {key}: {str(error)[:160]}; using release {hit.get('release')}")
                return [tuple(r) for r in hit["places"]]
            raise OvertureMissing(f"{key}: {str(error)[:160]}") from error
        cache[key] = {"release": release, "schema": OVERTURE_SCHEMA, "box": [float(x) for x in box],
                      "places": [list(r) for r in rows]}
        saved()
        return rows
    return places

def sharing_groups(areas):
    """The venues in groups that share no cell with each other (indoor_enrich.enrich widens a run
    to every venue sharing a cell, so each group is worked out on its own)."""
    groups = []
    for name, (_, _, rcs, _) in sorted(areas.items()):
        mine = {"names": {name}, "cells": set(rcs)}
        for g in [g for g in groups if g["cells"] & mine["cells"]]:
            groups.remove(g)
            mine["names"] |= g["names"]
            mine["cells"] |= g["cells"]
        groups.append(mine)
    return [g["names"] for g in groups]

class bundle_of:
    """Points indoor_enrich.enrich at these cells and venues instead of the iOS bundle's: its
    venue list, its cell loading and saving, and (optionally) its Overture lookup. `cells` are
    changed in place; nothing is written."""
    def __init__(self, cells, areas, places=None):
        enrich_areas = {name: (rcs, box, inside, kind == "airport") for name, (kind, inside, rcs, box) in areas.items()}
        self.patches = {"areas": lambda: enrich_areas,
                        "load": lambda rcs: {rc: cells[rc] for rc in rcs if rc in cells},
                        "save": lambda rc, d: True}  # enrich() saves only cells it changed
        if places is not None:
            self.patches["overture_places"] = places
    def __enter__(self):
        self.old = {k: getattr(indoor_enrich, k) for k in self.patches}
        for k, f in self.patches.items():
            setattr(indoor_enrich, k, f)
    def __exit__(self, *exc):
        for k, f in self.old.items():
            setattr(indoor_enrich, k, f)
        return False

def enrich(listed, overture=None):
    """The iOS bundle's fixes over every listed venue's cells on disk, as fetch_indoor_venues.py
    runs them: indoor_enrich.enrich over all venues at once (gate floors, Overture names, store
    points; venues sharing a cell are worked out together so they can't strip each other's
    points), then indoor_prune.prune_cells (floors with no named place taken off). Cells are
    written only when they change. Returns the number of cells written.

    Every venue's Overture places are asked for first. Venues whose places can't be had, and
    every venue sharing a cell with one, are run without Overture: their names and store points
    stay as they are (a cell refetched tonight has none until a later night), the rest get theirs.
    Those venues are not pruned either, nor is any venue with a cell not yet in the current
    format, nor any when Overture is off: a floor could look unnamed for want of what isn't
    there, and a floor taken off stays off until the cell is refetched."""
    overture = os.environ.get("INDOOR_OVERTURE", "1") != "0" if overture is None else overture
    areas = venue_areas(listed)
    keep = {box_key(box) for _, _, _, box in areas.values()}
    cells, before = {}, {}
    for rc in sorted({rc for _, _, rcs, _ in areas.values() for rc in rcs}):
        cell = load_json(OUT / f"indoor_{rc[0]}_{rc[1]}.json")
        if isinstance(cell.get("elements"), list) and cell.get("landside") is True:  # only cells in the current format
            cells[rc], before[rc] = cell, encode(cell["elements"], cell.get("osm_base"))
    areas = {name: a for name, a in areas.items() if any(rc in cells for rc in a[2])}
    if not cells:
        print("enriched: no cells")
        return 0

    def reset():
        for rc, body in before.items():
            cells[rc] = json.loads(body)

    cache = load_json(OVERTURE_CACHE) if overture else {}
    def save_cache():
        try:
            OVERTURE_CACHE.parent.mkdir(exist_ok=True)
            write_atomic(OVERTURE_CACHE, json.dumps({k: v for k, v in cache.items() if k in keep},
                                                    sort_keys=True, ensure_ascii=False).encode())
        except OSError as error:
            print(f"::warning::Overture cache not saved: {error}")
    with_overture = set()
    if overture:
        places = cached_places(cache, indoor_enrich.OVERTURE_RELEASE, indoor_enrich.overture_places, save_cache)
        missing = set()
        for name, (_, _, _, box) in sorted(areas.items()):
            try:
                places(box)
            except OvertureMissing as error:
                print(f"::warning::Overture places for {name} unavailable ({error}); its names and store points left as they are")
                missing.add(name)
        with_overture = {n for g in sharing_groups(areas) if not g & missing for n in g}
        save_cache()
    enriched = True
    try:
        if with_overture:
            with bundle_of(cells, {n: areas[n] for n in with_overture}, places):
                indoor_enrich.enrich(None, overture=True)
        rest = {n: a for n, a in areas.items() if n not in with_overture}
        if rest:
            with bundle_of(cells, rest):
                indoor_enrich.enrich(None, overture=False)
    except Exception as error:
        print(f"::warning::enriching: {str(error)[:300]}")
        reset()
        enriched = False
    pruned = {rc: encode(d["elements"], d.get("osm_base")) for rc, d in cells.items()}
    try:
        # A floor taken off stays off until the cell is refetched, so a venue is pruned only on
        # everything it has: every one of its cells in the current format (during the rollout
        # its others are still old-format), and tonight's Overture names (none when Overture is
        # off, couldn't be had, or enrich failed).
        # Venues sharing a cell are judged together (an element in two venues keeps the floors
        # either keeps), so a group is complete only when all its venues' cells are.
        complete = {n for g in sharing_groups(areas) if all(rc in cells for m in g for rc in areas[m][2]) for n in g}
        only = complete & with_overture if enriched else set()
        if len(only) < len(areas):
            why = (f"{len(areas) - len(complete)} with cells not yet rebuilt (or sharing cells with one), the rest "
                   + ("without tonight's Overture names" if enriched else "because enriching failed"))
            print(f"::warning::not pruned tonight: {len(areas) - len(only)} of {len(areas)} venues ({why})")
        found, dropped = indoor_prune.prune_cells(cells, areas, None if len(only) == len(areas) else only)
        for line in indoor_prune.describe(found):
            print(f"  pruned {line}")
        print(f"pruned: {len(found)} venues or outlines had floors with no named place, {dropped} elements dropped")
    except Exception as error:
        print(f"::warning::pruning: {str(error)[:300]}")
        for rc, body in pruned.items():
            cells[rc] = json.loads(body)
    changed = 0
    for rc, d in cells.items():
        body = encode(d["elements"], d.get("osm_base"))
        if body != before[rc]:
            write_atomic(OUT / f"indoor_{rc[0]}_{rc[1]}.json", body)
            changed += 1
    print(f"enriched and pruned: {changed} cells changed")
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
        elements = check_answer(data)
        # The floors of the ways each unfloored gate stands on, while the ways still list their
        # nodes (slim drops them): indoor_enrich.py's first gate pass reads them.
        fv.record_gate_ways(elements)
        elements = trim(slim(elements), row, col)
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
