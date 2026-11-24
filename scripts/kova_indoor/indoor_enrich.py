#!/usr/bin/env python3
"""Fills two gaps in the bundled indoor cells that OpenStreetMap leaves, without guessing floors.

    python3 Scripts/indoor_enrich.py                  # every bundled area
    python3 Scripts/indoor_enrich.py ORD "Grand Central"   # only these
    python3 Scripts/indoor_enrich.py --no-overture    # gate floors only (no duckdb, no network)

`fetch_indoor_venues.py` runs this on the areas it rebuilds. Both passes are idempotent: what an
earlier run added is taken out first and worked out again from OpenStreetMap's own data.

1. **Gate floors from neighbouring gates.** A gate with no `level` gets the floor every gate
   with a `level` around it agrees on: at least GATE_NEIGHBOURS of them within GATE_REACH, all
   on one floor. Checked leave-one-out against the 848 gates that do carry a level in the 24
   bundled airports: 844 right, 4 wrong (those 4 disagree with every neighbour and are listed by
   `--report` as likely OpenStreetMap mistakes; they are not changed). Nothing else predicts a
   gate's floor this well: the floor of the rooms around a gate was right for 91% of ORD's gates.
   Added tags: `level`, and `level:inferred=gates` so the inventory and the app can tell.

2. **Names for unnamed shop units from Overture Maps Places.** OpenStreetMap often maps a unit
   on its floor (`indoor=room` or `shop=*`, with `level`) and leaves out what's in it. Overture
   places have no floor (and no address in a bundled venue states one), so an Overture place is
   only used to **name a unit OSM already put on a floor**, never as a place of its own:
   - the record's every source is under a licence in LICENCES (all permissive; see
     docs/indoor-audit.md, "Overture licence review"), it is `open` and confidence >= 0.8;
   - its category is a shop or food one (RETAIL), not an airline, a terminal or an office;
   - it stands inside exactly one unnamed, public, non-vacant unit, and no unit on another floor
     covers that spot (a stacked mall: no way to tell which floor), and it is the only such
     Overture place in that unit;
   - no OSM place with the same name is within TWIN_REACH (it's already mapped next door).
   Added tags: `name`, a `shop` or `amenity` value when the unit had none, `name:source=overture`
   and `ref:overture=<GERS id>`. The unit keeps its OSM id and its OSM floor.
"""
import collections, json, math, os, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch_indoor_venues as fv  # noqa: E402

GATE_REACH, GATE_NEIGHBOURS = 120.0, 3
TWIN_REACH = 40.0
# Overture removes old releases from S3 after a few months: set OVERTURE_RELEASE to a current one
# (https://docs.overturemaps.org/release/latest/) when the pinned one is gone.
OVERTURE_RELEASE = os.environ.get("OVERTURE_RELEASE") or "2026-10-21.0"
OVERTURE_PLACES = f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=places/type=place/*"
# Every licence an Overture places record carried in the bundled venues' boxes (release above):
# 232,173 CDLA-Permissive-2.0 (Meta, Microsoft, Overture, PinMeTo, Uberall, Birdeye), 6,262
# Apache-2.0 (Foursquare OS Places), 654 CC0-1.0 (AllThePlaces). A record with any other licence,
# or none, is skipped.
LICENCES = {"CDLA-Permissive-2.0", "Apache-2.0", "CC0-1.0"}
MIN_CONFIDENCE = 0.8
# Overture basic_category -> the OSM tag a unit gets when it has neither `shop` nor `amenity`.
RETAIL = {
    "restaurant": ("amenity", "restaurant"), "casual_eatery": ("amenity", "fast_food"),
    "fast_food_restaurant": ("amenity", "fast_food"), "bar": ("amenity", "bar"),
    "coffee_shop": ("amenity", "cafe"), "cafe": ("amenity", "cafe"), "juice_bar": ("amenity", "cafe"),
    "bakery": ("shop", "bakery"), "dessert_shop": ("shop", "confectionery"),
    "food_and_beverage_store": ("shop", "confectionery"), "fashion_and_apparel_store": ("shop", "clothes"),
    "shoe_store": ("shop", "shoes"), "jewelry_store": ("shop", "jewelry"), "eyewear_store": ("shop", "optician"),
    "electronics_store": ("shop", "electronics"), "flowers_and_gifts_store": ("shop", "gift"),
    "gift_shop": ("shop", "gift"), "books_music_and_video_store": ("shop", "books"),
    "personal_care_and_beauty_store": ("shop", "cosmetics"), "sporting_goods_store": ("shop", "sports"),
    "toy_store": ("shop", "toys"), "arts_crafts_and_hobby_store": ("shop", "craft"),
    "convenience_store": ("shop", "convenience"), "department_store": ("shop", "department_store"),
    "duty_free_store": ("shop", "yes"), "specialty_store": ("shop", "yes"), "shopping": ("shop", "yes"),
}
ADDED_GATE_TAGS = ("level", "level:inferred")
ADDED_NAME_TAGS = ("name", "name:source", "ref:overture")


def metres(a, b):
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def inside(point, ring):
    lat, lon = point
    hit = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if (a["lat"] > lat) != (b["lat"] > lat) and lon < (b["lon"] - a["lon"]) * (lat - a["lat"]) / (b["lat"] - a["lat"]) + a["lon"]:
            hit = not hit
    return hit


def floors(tags):
    """IndoorMapParsing.floors (the inventory's copy): `level` plus `repeat_on`."""
    import indoor_inventory as inv
    return inv.floors(tags)


def level_text(value):
    return str(int(value)) if value == int(value) else str(value)


# --- 1. gates ------------------------------------------------------------------------------

def strip_gate_floors(elements):
    for e in elements:
        t = e["tags"]
        if t.get("aeroway") == "gate" and t.get("level:inferred"):
            for k in ADDED_GATE_TAGS:
                t.pop(k, None)


def gate_floors(elements):
    """{gate id: floor} for unfloored gate nodes, and the floored gates that disagree with every
    neighbour [(id, label, floor, neighbours' floor)]. Only OSM's own levels vote."""
    gates = [e for e in elements if e["type"] == "node" and e["tags"].get("aeroway") == "gate"]
    known = [(g, floors(g["tags"])) for g in gates]
    voters = [(g, f[0]) for g, f in known if len(f) == 1]

    def vote(g):
        near = [f for h, f in voters if h is not g and metres((g["lat"], g["lon"]), (h["lat"], h["lon"])) <= GATE_REACH]
        return near[0] if len(near) >= GATE_NEIGHBOURS and len(set(near)) == 1 else None

    added, suspect = {}, []
    for g, f in known:
        v = vote(g)
        if v is None:
            continue
        if not f:
            added[g["id"]] = v
        elif v not in f:
            suspect.append((g["id"], g["tags"].get("ref") or g["tags"].get("name"), f, v))
    return added, suspect


# --- 2. Overture names ---------------------------------------------------------------------

def strip_overture_names(elements):
    for e in elements:
        t = e["tags"]
        if t.get("name:source") == "overture":
            for k in ADDED_NAME_TAGS:
                t.pop(k, None)
            added = t.pop("overture:added", None)
            if added:
                t.pop(added, None)


def normal(name):
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("&", "and"))


def clean_name(name, venue):
    """'Gucci - LAX' -> 'Gucci'; "Dippin' Dots: Mall of America" -> "Dippin' Dots"."""
    words = {normal(venue), "airport", "terminal", "mall", "station", "concourse"}
    for sep in (" - ", " – ", ": ", ", ", " @ ", " at "):
        if sep in name:
            head, tail = name.rsplit(sep, 1)
            t = normal(tail)
            if head.strip() and (t in words or any(w and w in t for w in words) or re.fullmatch(r"[a-z]{3}", t)):
                name = head
    return name.strip()


def overture_places(box, path=None):
    """Overture places inside box (s, w, n, e) that pass the licence, status and category rules:
    [(gers id, name, basic_category, lat, lon)]. `path` reads a local extract instead of S3."""
    import duckdb  # pip install duckdb; only this pass needs it
    s, w, n, e = box
    c = duckdb.connect()
    if not path:
        c.execute("install httpfs; load httpfs; set s3_region='us-west-2';")
    source = f"read_parquet('{path or OVERTURE_PLACES}', hive_partitioning=1)"
    rows = c.execute(f"""
        select id, names.primary, basic_category, (bbox.ymin + bbox.ymax) / 2, (bbox.xmin + bbox.xmax) / 2,
               list_transform(sources, lambda x: x.license)
        from {source}
        where bbox.xmin > ? and bbox.xmax < ? and bbox.ymin > ? and bbox.ymax < ?
          and operating_status = 'open' and confidence >= ? and names.primary is not null""",
                     [w, e, s, n, MIN_CONFIDENCE]).fetchall()
    return [r[:5] for r in rows if r[2] in RETAIL and r[5] and all(l in LICENCES for l in r[5])]


def overture_names(elements, places, venue, within=lambda lat, lon: True):
    """{unit id: (name, gers id, basic_category)} for unnamed units an Overture place names."""
    units = [e for e in elements if e["type"] == "way" and len(e.get("geometry") or []) >= 4
             and e["geometry"][0] == e["geometry"][-1] and floors(e["tags"])
             and (e["tags"].get("indoor") in ("room", "area") or "shop" in e["tags"])]
    rooms = [u for u in units if u["tags"].get("indoor") == "room" or "shop" in u["tags"]]
    named = [(normal(e["tags"]["name"]), centre(e)) for e in elements if e["tags"].get("name")]
    picks = collections.defaultdict(list)
    for gers, name, category, lat, lon in places:
        if not within(lat, lon):
            continue
        under = [u for u in units if inside((lat, lon), u["geometry"])]
        if not under or len({f for u in under for f in floors(u["tags"])}) != 1:
            continue  # outside every unit, or units on more than one floor cover the spot
        hit = [u for u in rooms if u in under]
        if len(hit) != 1:
            continue
        t = hit[0]["tags"]
        if ("name" in t or t.get("access") in ("private", "no", "permit") or t.get("shop") == "vacant"
                or t.get("room") not in (None, "shop", "retail")):
            continue
        name = clean_name(name, venue)
        key = normal(name)
        if not key or any(k == key and metres(c, (lat, lon)) <= TWIN_REACH for k, c in named if c):
            continue
        picks[hit[0]["id"]].append((name, gers, category))
    return {uid: p[0] for uid, p in picks.items() if len(p) == 1}


def centre(e):
    pts = [e] if e["type"] == "node" else e.get("geometry") or [p for m in e.get("members") or [] for p in m["geometry"]]
    return (sum(p["lat"] for p in pts) / len(pts), sum(p["lon"] for p in pts) / len(pts)) if pts else None


# --- cells ---------------------------------------------------------------------------------

def areas():
    """name -> (cells, box (s, w, n, e), test(lat, lon), is_airport)"""
    import indoor_inventory as inv
    out = {}
    for name, (kind, test, cells, box) in inv.venue_areas().items():
        if box is None:
            lat, lon = fv.VENUES[name]
            r = inv.POINT_RADIUS.get(name, 200) / 111_320
            box = (lat - r, lon - r / math.cos(math.radians(lat)), lat + r, lon + r / math.cos(math.radians(lat)))
        out[name] = (cells, box, test, kind == "airport")
    return out


def load(cells):
    return {rc: json.loads(p.read_bytes()) for rc in cells if (p := fv.OUT / f"indoor_{rc[0]}_{rc[1]}.json").exists()}


def save(rc, data):
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    path = fv.OUT / f"indoor_{rc[0]}_{rc[1]}.json"
    if path.read_bytes() != body:
        path.write_bytes(body)
        return True
    return False


def enrich_cells(data, venue, is_airport, places, within=lambda lat, lon: True):
    """Both passes over one venue's cells ({(row, col): cell JSON}, changed in place). `places` are
    `overture_places` rows, or None to leave names alone. Returns (gates, suspect, names): what
    was added, and the floored gates every neighbour disagrees with."""
    copies = collections.defaultdict(list)  # an element sits in every cell its bounds touch
    for d in data.values():
        for e in d["elements"]:
            copies[(e["type"], e["id"])].append(e)
    unique = [c[0] for c in copies.values()]
    every = [x for c in copies.values() for x in c]
    strip_gate_floors(every)
    gates, suspect = gate_floors(unique) if is_airport else ({}, [])
    for gid, level in gates.items():
        for e in copies[("node", gid)]:
            e["tags"]["level"] = level_text(level)
            e["tags"]["level:inferred"] = "gates"
    names = {}
    if places is not None:
        strip_overture_names(every)
        names = overture_names(unique, places, venue, within)
        for uid, (label, gers, category) in names.items():
            for e in copies[("way", uid)]:
                t = e["tags"]
                t["name"], t["name:source"], t["ref:overture"] = label, "overture", gers
                if "shop" not in t and "amenity" not in t:
                    k, v = RETAIL[category]
                    t[k], t["overture:added"] = v, k
    return gates, suspect, names


def enrich(names=None, overture=True, overture_path=None, report=None):
    """Runs both passes over the named bundled areas (all when None); returns the changed cells' count."""
    changed = 0
    for name, (cells, box, test, is_airport) in areas().items():
        if names and name not in names:
            continue
        data = load(cells)
        places = overture_places(box, overture_path) if overture else None
        gates, suspect, added = enrich_cells(data, name, is_airport, places, test)
        n = sum(save(rc, d) for rc, d in data.items())
        changed += n
        print(f"{name}: {len(gates)} gates floored from neighbours, {len(added)} units named from Overture, {n} cells changed")
        for gid, label, f, v in suspect:
            print(f"  gate {label or gid} (node {gid}) is on {f} in OSM, every neighbour on {v}: check in OSM")
        if report is not None:
            report[name] = {"gates_floored": {str(k): v for k, v in sorted(gates.items())},
                            "suspect_gate_levels": suspect,
                            "units_named": {str(k): v[0] for k, v in sorted(added.items())}}
    return changed


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--overture-file=")), None)
    report = {} if "--report" in sys.argv else None
    enrich(args or None, overture="--no-overture" not in sys.argv, overture_path=path, report=report)
    if report is not None:
        json.dump(report, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
