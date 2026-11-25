#!/usr/bin/env python3
"""Fills gaps in the bundled indoor cells that OpenStreetMap leaves, without guessing floors.

    python3 Scripts/indoor_enrich.py                  # every bundled area
    python3 Scripts/indoor_enrich.py ORD "Grand Central"   # only these
    python3 Scripts/indoor_enrich.py --no-overture    # gate floors only (no duckdb, no network)

`fetch_indoor_venues.py` runs this on the areas it rebuilds. `--overture-file=<parquet>` reads a
local Overture extract instead of S3. Every pass is idempotent: what an
earlier run added is taken out first and worked out again from OpenStreetMap's own data.

1. **Gate floors from the gate's own ways.** `fetch_indoor_venues.record_gate_ways` records the
   floors of the ways an unfloored gate node is a vertex of: indoor corridors, footways, rooms
   and areas (`level:ways`), and jet bridges (`level:jet_bridge`). The gate takes the floor of
   its indoor ways when they are all on one floor, no jet bridge of it says otherwise, and the
   venue already has that floor from OSM's own places (a lone footbridge level would otherwise
   add a floor button for one gate, as at DFW).
   Checked against the gates of 12 airports whose own `level` is known and whose ways give one
   (docs/indoor-audit.md, section 9): 683 agree, 16 don't. The 16 are at ORD (8: the L and M
   gates, where at L28 the neighbouring gates side with the corridor), BOS (5, Terminal E), and
   SFO, DEN (C31, a suspect below) and JFK (1 each): OSM's gate and its corridor disagree there.
   Outside ORD and BOS, 425 of 427 agree. A jet bridge alone is not used: its floor matched the
   gate's at BOS but not at JFK, where 21 of 22 Terminal 4 bridges are a floor above the gate,
   even with the shops around on the bridge's floor.
   Added tags: `level`, and `level:inferred=ways`.

2. **Gate floors from neighbouring gates.** A gate with no `level` gets the floor every gate
   with a `level` around it agrees on: at least GATE_NEIGHBOURS of them within GATE_REACH, all
   on one floor. Checked leave-one-out against the 848 gates that do carry a level in the 24
   bundled airports: 844 right, 4 wrong (those 4 disagree with every neighbour and are listed by
   `--report` as likely OpenStreetMap mistakes; they are not changed). Nothing else predicts a
   gate's floor this well: the floor of the rooms around a gate was right for 91% of ORD's gates.
   Added tags: `level`, and `level:inferred=gates` so the inventory and the app can tell. Gates
   floored by pass 1 vote as well (they are run first); those inferred here don't. A pass-1
   floor that a unanimous vote of OSM's own levels contradicts is taken back (the gate then gets
   the vote's floor), and only OSM's own levels are reported as suspects.

3. **Names for unnamed shop units from Overture Maps Places.** OpenStreetMap often maps a unit
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

4. **Store points in malls and stations** (not airports), so a mall's names show the moment its
   plan does instead of waiting for Apple's search. The other Overture shops and eateries (same
   licence, status, confidence and category rules) become points of their own, but only on a
   floor something real states (`store_points`): the address ("Upper Level", "2nd Floor", read in
   the venue's own numbering), or the one floor of the units it stands in where the venue's
   other floors are mapped at that spot as corridor or open area. Anything else is left out:
   nothing is put on a guessed floor. Added as nodes with a stable negative id and the tags
   `name`, `shop`/`amenity`, `level`, `level:source=address|unit`, `name:source=overture` and
   `ref:overture`. All venues' points are taken out and written back together (`enrich`), so
   venues sharing a cell can't undo each other's.
"""
import collections, copy, json, math, os, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch_indoor_venues as fv  # noqa: E402

GATE_REACH, GATE_NEIGHBOURS = 120.0, 3
# Pass 2's fallbacks, from OSM's own gate levels only (docs/indoor-audit.md, section 12):
# a pair of gates within LOOSE_REACH, every OSM-floored gate there on one floor (1,646 of 1,652
# right leave-one-out), or a terminal outline whose OSM-floored gates (at least OUTLINE_GATES)
# are all on one floor (740 of 743).
LOOSE_REACH, LOOSE_NEIGHBOURS, OUTLINE_GATES = 200.0, 2, 3
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
        if t.get("aeroway") == "gate" and t.get("level:inferred") in ("gates", "ways", "outline"):
            for k in ADDED_GATE_TAGS:
                t.pop(k, None)


def way_floors(elements):
    """{gate id: floor} for unfloored gates their recorded indoor ways settle (pass 1)."""
    import indoor_inventory as inv
    # Only a floor the venue has anyway: one a shop, room or gate of OSM's own stands on. A lone
    # footbridge on another level would otherwise give a terminal a floor button for one gate.
    known = {f for e in elements
             if (inv.classify(e) not in (None, "stairs", "elevator", "footprint", "landside", "wall")
                 and not e["tags"].get("level:inferred"))
             for f in floors(e["tags"])}
    out = {}
    for g in elements:
        t = g["tags"]
        if g["type"] != "node" or t.get("aeroway") != "gate" or floors(t):
            continue
        ways, bridges = set(inv.levels(t.get("level:ways"))), set(inv.levels(t.get("level:jet_bridge")))
        if len(ways) == 1 and bridges <= ways and ways <= known:
            out[g["id"]] = next(iter(ways))
    return out


def gate_floors(elements):
    """Pass 2 over gate nodes. Returns (added, suspect, overruled, kind):
    - added: {gate id: floor} for unfloored gates their neighbours agree on;
    - suspect: OSM-floored gates every neighbour disagrees with [(id, label, floor, neighbours')];
    - overruled: ids of gates pass 1 floored from their ways that a unanimous vote of OSM's own
      levels contradicts (ORD's L and M gates): the way-floor is taken back;
    - kind: {gate id: "gates" | "outline"}, the rule that floored each added gate.
    OSM's own levels vote, and so do pass 1's floors that stand."""
    gates = [e for e in elements if e["type"] == "node" and e["tags"].get("aeroway") == "gate"]
    known = [(g, floors(g["tags"])) for g in gates]
    own = [(g, f[0]) for g, f in known if len(f) == 1 and not g["tags"].get("level:inferred")]

    def vote(g, voters):
        near = [f for h, f in voters if h is not g and metres((g["lat"], g["lon"]), (h["lat"], h["lon"])) <= GATE_REACH]
        return near[0] if len(near) >= GATE_NEIGHBOURS and len(set(near)) == 1 else None

    overruled, from_ways = set(), []
    for g, f in known:
        if g["tags"].get("level:inferred") == "ways" and f:
            v = vote(g, own)
            if v is not None and v not in f:
                overruled.add(g["id"])
            else:
                from_ways.append((g, f[0]))
    voters = own + from_ways
    outlines = [e["geometry"] for e in elements if e["type"] == "way" and len(e.get("geometry") or []) >= 4
                and e["geometry"][0] == e["geometry"][-1] and e["tags"].get("aeroway") == "terminal"]

    def loose(g):
        near = [f for h, f in own if h is not g and metres((g["lat"], g["lon"]), (h["lat"], h["lon"])) <= LOOSE_REACH]
        return near[0] if len(near) >= LOOSE_NEIGHBOURS and len(set(near)) == 1 else None

    def outline(g):
        # Every OSM-floored gate in every outline over this gate must agree, small ones too.
        seen, enough = set(), False
        for ring in outlines:
            if inside((g["lat"], g["lon"]), ring):
                fs = [f for h, f in own if h is not g and inside((h["lat"], h["lon"]), ring)]
                seen.update(fs)
                enough = enough or len(fs) >= OUTLINE_GATES
        return next(iter(seen)) if enough and len(seen) == 1 else None

    added, suspect, kind = {}, [], {}
    for g, f in known:
        if g["id"] in overruled or not f:
            v, k = vote(g, voters), "gates"
            if v is None:
                v = loose(g)
            if v is None:
                v, k = outline(g), "outline"
            if v is not None:
                added[g["id"]] = v
                kind[g["id"]] = k
        elif not g["tags"].get("level:inferred"):
            v = vote(g, own)
            if v is not None and v not in f:
                suspect.append((g["id"], g["tags"].get("ref") or g["tags"].get("name"), f, v))
    return added, suspect, overruled, kind


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
    [(gers id, name, basic_category, lat, lon, address)]. `path` reads a local extract instead of S3."""
    import duckdb  # pip install duckdb; only this pass needs it
    s, w, n, e = box
    c = duckdb.connect()
    if not path:
        c.execute("install httpfs; load httpfs; set s3_region='us-west-2';")
    source = f"read_parquet('{path or OVERTURE_PLACES}', hive_partitioning=1)"
    rows = c.execute(f"""
        select id, names.primary, basic_category, (bbox.ymin + bbox.ymax) / 2, (bbox.xmin + bbox.xmax) / 2,
               list_transform(sources, lambda x: x.license), addresses[1].freeform
        from {source}
        where bbox.xmin > ? and bbox.xmax < ? and bbox.ymin > ? and bbox.ymax < ?
          and operating_status = 'open' and confidence >= ? and names.primary is not null""",
                     [w, e, s, n, MIN_CONFIDENCE]).fetchall()
    return [r[:5] + (r[6],) for r in rows if r[2] in RETAIL and r[5] and all(l in LICENCES for l in r[5])]


def floored_units(elements):
    """Closed ways on a floor a place can stand in: rooms, areas and shop outlines. Not the mall's
    own outline: Aventura's `shop=mall` building carries `level=0;1;2`, and counting it put every
    spot in the mall under three floors, so no Overture place there ever looked single-floor."""
    return [e for e in elements if e["type"] == "way" and len(e.get("geometry") or []) >= 4
            and e["geometry"][0] == e["geometry"][-1] and floors(e["tags"])
            and e["tags"].get("shop") != "mall" and ("building" not in e["tags"] or "indoor" in e["tags"])
            and (e["tags"].get("indoor") in ("room", "area") or "shop" in e["tags"])]


def overture_names(elements, places, venue, within=lambda lat, lon: True):
    """{unit id: (name, gers id, basic_category)} for unnamed units an Overture place names."""
    units = floored_units(elements)
    rooms = [u for u in units if u["tags"].get("indoor") == "room" or "shop" in u["tags"]]
    # OSM's own names only: a neighbouring venue's Overture names (in a shared cell) are not twins.
    named = [(normal(e["tags"]["name"]), centre(e)) for e in elements
             if e["tags"].get("name") and e["tags"].get("name:source") != "overture"]
    picks = collections.defaultdict(list)
    for gers, name, category, lat, lon, *_ in places:
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


# --- 4. store points ---------------------------------------------------------------------

FLOOR_PATTERNS = (
    re.compile(r"\b(?:level|lvl|floor|flr|fl)\.?\s*#?\s*(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+(?:floor|level|flr|fl)\b"),
)
# Ordinals count like numbers; "Upper/Lower Level" names a side, not a number (`level_word`).
FLOOR_WORDS = (("ground floor", 1), ("ground level", 1), ("street level", 1), ("first floor", 1),
               ("second floor", 2), ("third floor", 3), ("fourth floor", 4))
SIDE_WORDS = (("upper level", "upper"), ("uppr level", "upper"), ("lower level", "lower"),
              ("lowr level", "lower"))


def stated_floor(text):
    """The floor number an address states ("Level 2", "2nd Floor", "Second Floor"), counted like
    the signs (1 = ground). Not "Upper Level" (`level_word`), nor the unit number after it
    ("Lower Level #2"). Stricter than the app's StatedFloor.floor, which reads "Upper Level" as 2
    for Apple's places at run time."""
    if not text:
        return None
    lower = text.lower()
    for side, _ in SIDE_WORDS:
        lower = lower.replace(side, " ")
    for pattern in FLOOR_PATTERNS:
        m = pattern.search(lower)
        if m and 1 <= int(m.group(1)) <= 60:
            return int(m.group(1))
    return next((n for w, n in FLOOR_WORDS if w in lower), None)


def level_word(text):
    """"upper" or "lower" for an address that names its level that way and gives no number."""
    if not text or stated_floor(text) is not None:
        return None
    lower = text.lower()
    sides = {side for w, side in SIDE_WORDS if w in lower}
    # "Lower Level #153, Upper Level #2037": a store on both, or two listings; either way not one floor.
    return next(iter(sides)) if len(sides) == 1 else None


def calibrated_offset(pairs):
    """StatedFloor.calibratedOffset: level = stated + offset (0 or -1), voted by places that have
    both a stated floor and an OSM twin with levels. None unless two agree and outvote 2:1."""
    same = lower = 0
    for stated, levels in pairs:
        a, b = stated in levels, stated - 1 in levels
        same += a and not b
        lower += b and not a
    win, lose = max(same, lower), min(same, lower)
    if win < 2 or win < 2 * lose:
        return None
    return 0.0 if same > lower else -1.0


def own_floors(elements):
    """The floors OSM's own shops, rooms and gates stand on (not stairs, not anything inferred)."""
    import indoor_inventory as inv
    return {f for e in elements
            if inv.classify(e) not in (None, "stairs", "elevator", "footprint", "landside", "wall")
            and not e["tags"].get("level:inferred") and not e["tags"].get("level:source")
            for f in floors(e["tags"])}


def point_id(gers):
    """A stable negative id (never an OSM one) from the Overture GERS id."""
    import hashlib
    return -int(hashlib.sha1(gers.encode()).hexdigest()[:13], 16)


def store_points(elements, places, venue, within=lambda lat, lon: True, skip=(), floors_seen=None):
    """[(gers, name, category, lat, lon, level, source)]: Overture's shops and eateries in a mall,
    as points on the floor a real source gives them, so a mall's names show without waiting for
    Apple's search. Only a floor that is stated or mapped, never a guessed one:

    - `unit`: every floored unit the place stands in is on one floor, and the venue's other
      floors are mapped at that spot as corridor or open area (`floor_is_settled`);
    - `address`: its address states the floor ("Upper Level", "2nd Floor"), read in the venue's
      own numbering. That numbering is settled by Overture places whose OSM twin has a level, and
      by OSM's own `level:ref` beside `level` (`calibrated_offset`); unsettled, a venue with no
      floor 0 counts like its signs, and in one with a floor 0 the stated floor must pick one of
      the floors the units under it are on. "Upper/Lower Level" picks between the two floors of
      the units under the spot when they are the venue's two lowest (Aventura: floor 1 over floor
      0) and every higher floor mapped within UNIT_REACH (a shape's edge or a shop's point) is
      mapped at the spot as corridor or open area, or a two-floor venue's two; never where the
      outline's `building:levels` (when tagged) isn't 2. Otherwise it says nothing. An address the units under it
      contradict leaves the place out (the unit rule doesn't overrule it);
    - and the floor must be one OSM's own places are on. A floor taken from the address also needs
      the place inside the venue's own outline or a mapped unit (a circle takes in neighbours).

    A place whose floor nothing states is left out: Apple's search names it on the phone, and
    the directory lists it without a floor. So is one OSM already maps by that name within
    TWIN_REACH (any floor, any venue), or anywhere in the venue on the same floor, and one pass 3
    used to name a unit (`skip`).
    """
    # Only what stands in the venue: a neighbour's floors or numbering say nothing about it.
    everything = elements
    elements = [e for e in elements if (c := centre(e)) and within(*c)]
    units = floored_units(elements)
    venue_floors = own_floors(elements)
    if not venue_floors:
        return []
    shapes = [e for e in elements if e["type"] == "way" and len(e.get("geometry") or []) >= 4
              and e["geometry"][0] == e["geometry"][-1] and floors(e["tags"])
              and e["tags"].get("indoor") in ("room", "area", "corridor") and e["tags"].get("shop") != "mall"]
    # The venue's own outlines (a mall, a station building): a venue's circle takes in its
    # neighbours too (the Millennium Hilton beside the Oculus, whose restaurant's address says
    # "3rd floor").
    outlines = [e["geometry"] for e in elements if e["type"] == "way" and "indoor" not in e["tags"]
                and len(e.get("geometry") or []) >= 4 and e["geometry"][0] == e["geometry"][-1]
                and (e["tags"].get("shop") == "mall" or fv.is_hall(e["tags"]) or e["tags"].get("aeroway") == "terminal"
                     or e["tags"].get("building") in ("train_station", "transportation"))]
    spots = [e for e in elements if e["type"] == "node" and ("shop" in e["tags"] or "amenity" in e["tags"])
             and not e["tags"].get("level:source") and floors(e["tags"])]
    storeys = {int(e["tags"]["building:levels"]) for e in elements
               if e["tags"].get("shop") == "mall" and e["type"] != "node"
               and re.fullmatch(r"\d{1,2}", e["tags"].get("building:levels", "").strip())
               and within(*centre(e))}
    # OSM's names (and pass 3's), near the place whatever venue they're in, and anywhere in this
    # venue on the place's own floor: a second label for a store already named on that floor
    # would most likely stand in the wrong spot (Overture's position is the likelier error).
    named = [(normal(e["tags"]["name"]), centre(e), floors(e["tags"])) for e in everything
             if e["tags"].get("name") and not e["tags"].get("level:source")]
    named_here = [n for n in named if n[1] and within(*n[1])]
    on_floor = {(normal(e["tags"]["name"]), f) for e in elements
                if e["tags"].get("name") and not e["tags"].get("level:source") for f in floors(e["tags"])}
    rows = sorted((p for p in places if p[0] not in skip and within(p[3], p[4])), key=lambda p: p[0])
    votes, voted = [], set()
    for gers, name, category, lat, lon, *rest in rows:
        stated = stated_floor(rest[0] if rest else None)
        key = normal(clean_name(name, venue))
        if stated is None or not key:
            continue
        # Only the venue's own twins vote on its numbering: the hotel next door counts its own way.
        twin = next((n for n in named_here if n[0] == key and n[2] and metres(n[1], (lat, lon)) <= TWIN_REACH), None)
        if twin and id(twin) not in voted:
            voted.add(id(twin))
            votes.append((stated, set(twin[2])))
    # OSM's own `level:ref` (the sign's number) beside `level` is the same evidence: Hudson Yards'
    # Shed tags "6" on level 6.
    for e in elements:
        ref, own = e["tags"].get("level:ref"), floors(e["tags"])
        if ref and re.fullmatch(r"\d{1,2}", ref.strip()) and len(own) == 1 and not e["tags"].get("level:source"):
            votes.append((int(ref), set(own)))
    offset = calibrated_offset(votes)
    out, seen = [], []
    for gers, name, category, lat, lon, *rest in rows:
        label = clean_name(name, venue)
        key = normal(label)
        if not key or any(k == key and c and metres(c, (lat, lon)) <= TWIN_REACH for k, c, _ in named):
            continue
        under = {f for u in units if inside((lat, lon), u["geometry"]) for f in floors(u["tags"])}
        level, source = None, None
        address = rest[0] if rest else None
        stated, side = stated_floor(address), level_word(address)
        if (stated is not None or side) and not under and not any(inside((lat, lon), o) for o in outlines):
            continue                # not in the venue's building: its floors aren't the venue's
        if stated is not None:
            if offset is not None:
                level = stated + offset
            elif 0 not in venue_floors:
                level = float(stated)
            else:
                picks = {stated - 1.0, float(stated)} & under
                level = next(iter(picks)) if len(picks) == 1 else None
            if level is not None and under and level not in under:
                level = None        # the address and the units under it disagree
        elif side:
            # "Upper Level" says which of two: the two floors of the units over each other at the
            # spot (Aventura), else the venue's two floors. With three or more, it doesn't say.
            # Only the venue's two lowest floors: in a mall of three, "Lower" could be the floor
            # below the pair the units happen to map, and "Upper" the middle or the top.
            lowest = set(sorted(venue_floors)[:2])
            pair = lowest if len(venue_floors) == 2 or (len(under) == 2 and under == lowest) else None
            # A building OSM says has another number of storeys has floors its mapping lacks.
            if storeys and storeys != {2}:
                pair = None
            # And in a venue of three or more, every higher floor mapped near the spot must be
            # mapped at it, as corridor or open area: else the store could be up there.
            if pair and len(venue_floors) > 2:
                above = {f for f in venue_floors if f > max(pair)}
                near = {f for e in shapes for f in floors(e["tags"])
                        if f in above and edge_metres((lat, lon), e["geometry"]) <= UNIT_REACH}
                # A shop or eatery mapped as a point on a floor above, nearby, says that floor is here.
                near |= {f for e in spots for f in floors(e["tags"])
                         if f in above and metres((e["lat"], e["lon"]), (lat, lon)) <= UNIT_REACH}
                covering = {f for e in shapes if inside((lat, lon), e["geometry"]) for f in floors(e["tags"])}
                if near - covering:
                    pair = None
            if pair:
                level = max(pair) if side == "upper" else min(pair)
                if under and level not in under:
                    level = None
        if stated is not None or side:
            if level is None:
                continue            # a stated floor we can't read is not overruled by a unit
            source = "address"
        if level is None and len(under) == 1 and floor_is_settled((lat, lon), next(iter(under)), shapes, venue_floors, storeys):
            level, source = next(iter(under)), "unit"
        if level is not None and floors_seen is not None:
            floors_seen[gers].add(level)    # even when left out below: another venue may disagree
        if level is None or level not in venue_floors or (key, level) in on_floor:
            continue
        if any(k == key and f == level for k, c, f in seen):
            # One store listed twice by Overture, or a chain's second store on that floor: one
            # label per name and floor, the listing with the lowest GERS id (so its spot is
            # arbitrary between two real stores, but never its floor).
            continue
        seen.append((key, (lat, lon), level))
        out.append((gers, label, category, lat, lon, level, source))
    return out


UNIT_REACH = 50.0


def edge_metres(point, ring):
    """Metres from a point to the nearest edge of a ring (0 inside it is not implied)."""
    lat0 = math.radians(point[0])
    def xy(q):
        return ((q["lon"] - point[1]) * 111_320 * math.cos(lat0), (q["lat"] - point[0]) * 111_320)
    best = float("inf")
    for a, b in zip(ring, ring[1:]):
        (ax, ay), (bx, by) = xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def floor_is_settled(point, floor, shapes, venue_floors, storeys):
    """Whether a place in a unit on `floor` (and in no unit of another floor) is on that floor,
    and not on a floor OpenStreetMap hasn't mapped there. Every other floor the venue's mapping
    has within UNIT_REACH must be mapped at the spot itself, as a corridor or an open area (a
    unit there would have made the spot ambiguous): an upper level's walkway over a ground-floor
    shop. A venue mapped on one floor only counts when its outline says it has one storey
    (`building:levels=1`): Altamonte Mall's OSM units are all on 0, but the mall has two levels."""
    if len(venue_floors) == 1:
        return storeys == {1}
    near = {f for e in shapes for f in floors(e["tags"]) if edge_metres(point, e["geometry"]) <= UNIT_REACH}
    if not near - {floor}:
        return False        # nothing of another floor mapped around: no way to tell it isn't there
    covering = {f for e in shapes if inside(point, e["geometry"]) for f in floors(e["tags"])}
    return (near - {floor}) <= covering


def strip_store_points(data, within=lambda lat, lon: True):
    """Takes out this venue's pass-4 points; a neighbouring venue's in a shared cell stay."""
    for d in data.values():
        d["elements"] = [e for e in d["elements"]
                         if not (e["type"] == "node" and e["tags"].get("level:source") and within(e["lat"], e["lon"]))]


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


def place_points(data, points):
    """Writes pass 4's points into every cell whose box holds them (as the fetch writes an element
    into every cell it touches), once each, after the cell's OSM elements and in id order."""
    for gers, label, category, lat, lon, level, source, *_ in points:
        k, v = RETAIL[category]
        node = {"type": "node", "id": point_id(gers), "lat": round(lat, 6), "lon": round(lon, 6),
                "tags": {"name": label, k: v, "level": level_text(level), "level:source": source,
                         "name:source": "overture", "ref:overture": gers}}
        for rc, d in data.items():
            s, w, n, e = fv.cell_box(*rc)
            # Two venues whose circles overlap (Penn Station, Moynihan) find the same place.
            if s <= lat <= n and w <= lon <= e and not any(
                    x["type"] == "node" and x["id"] == node["id"] for x in d["elements"]):
                d["elements"].append(copy.deepcopy(node))
    for d in data.values():
        rest = [e for e in d["elements"] if not (e["type"] == "node" and e["tags"].get("level:source"))]
        mine = sorted((e for e in d["elements"] if e["type"] == "node" and e["tags"].get("level:source")),
                      key=lambda e: e["id"])
        d["elements"] = rest + mine


def enrich_cells(data, venue, is_airport, places, within=lambda lat, lon: True, add_points=True, standalone=True,
                 with_points=True):
    """Every pass over one venue's cells ({(row, col): cell JSON}, changed in place). `places` are
    `overture_places` rows, or None to leave names and points alone. Returns (gates, suspect,
    names, by_ways, points): what was added, the floored gates every neighbour disagrees with, the
    gates floored from their ways, and pass 4's points (written into the cells unless
    `add_points` is false: `enrich` writes every venue's at once, so two venues sharing a cell
    can't undo each other's). `standalone=False`: `enrich` has already taken the earlier passes'
    names and points out of every cell."""
    if places is not None and add_points and standalone:
        strip_store_points(data, within)  # pass 4's points are worked out again below
    copies = collections.defaultdict(list)  # an element sits in every cell its bounds touch
    for d in data.values():
        for e in d["elements"]:
            copies[(e["type"], e["id"])].append(e)
    unique = [c[0] for c in copies.values()]
    every = [x for c in copies.values() for x in c]
    strip_gate_floors(every)
    by_ways = way_floors(unique) if is_airport else {}
    for gid, level in by_ways.items():
        for e in copies[("node", gid)]:
            e["tags"]["level"] = level_text(level)
            e["tags"]["level:inferred"] = "ways"
    gates, suspect, overruled, kinds = gate_floors(unique) if is_airport else ({}, [], set(), {})
    for gid in overruled:
        by_ways.pop(gid, None)
        for e in copies[("node", gid)]:
            for k in ADDED_GATE_TAGS:
                e["tags"].pop(k, None)
    for gid, level in gates.items():
        for e in copies[("node", gid)]:
            e["tags"]["level"] = level_text(level)
            e["tags"]["level:inferred"] = kinds.get(gid, "gates")
    names, points = {}, []
    if places is not None:
        if standalone:
            strip_overture_names(every)
        names = overture_names(unique, places, venue, within)
        for uid, (label, gers, category) in names.items():
            for e in copies[("way", uid)]:
                t = e["tags"]
                t["name"], t["name:source"], t["ref:overture"] = label, "overture", gers
                if "shop" not in t and "amenity" not in t:
                    k, v = RETAIL[category]
                    t[k], t["overture:added"] = v, k
        if not is_airport and with_points:
            used = {gers for _, gers, _ in names.values()}
            points = [p + (venue,) for p in store_points(unique, places, venue, within, skip=used)]
            if add_points:
                place_points(data, points)
    return gates, suspect, names, by_ways, points


def enrich(names=None, overture=True, overture_path=None, report=None):
    """Runs every pass over the named bundled areas (all when None); returns the changed cells' count.

    The cells are loaded once and shared: venues whose boxes overlap (Penn Station and Moynihan,
    the Oculus and Brookfield Place) see one copy of a cell, and with Overture every venue sharing
    a cell with a chosen one runs too. Pass 3's names and pass 4's points are taken out of every
    cell first; then every venue's passes 1-3 run, then every venue's pass 4 (so a neighbour's
    names are all in place), and the points are written back together. A subset run writes what
    a full run does, in any order."""
    every_area = areas()
    chosen = {n: a for n, a in every_area.items() if not names or n in names}
    if overture:
        while True:
            cells = {rc for c, *_ in chosen.values() for rc in c}
            more = {n: a for n, a in every_area.items() if n not in chosen and cells & set(a[0])}
            if not more:
                break
            chosen.update(more)
    data = load(sorted({rc for cells, *_ in chosen.values() for rc in cells}))
    before = {rc: json.dumps(d, sort_keys=True) for rc, d in data.items()}
    if overture:
        for cells, box, test, is_airport in chosen.values():
            if not is_airport:
                strip_store_points(data, test)
        strip_overture_names([e for d in data.values() for e in d["elements"]])
    results, every_point = {}, []
    for name, (cells, box, test, is_airport) in sorted(chosen.items()):
        mine = {rc: data[rc] for rc in cells if rc in data}
        places = overture_places(box, overture_path) if overture else None
        results[name] = (places,) + enrich_cells(mine, name, is_airport, places, test, add_points=False,
                                                 standalone=not overture, with_points=False)
    # Pass 4 after every venue's names: a unit any venue named from Overture is not a point too.
    used = {e["tags"]["ref:overture"] for d in data.values() for e in d["elements"]
            if e["type"] == "way" and e["tags"].get("name:source") == "overture"}
    disputes = collections.defaultdict(set)  # every floor any venue read for a place
    for name, (cells, box, test, is_airport) in sorted(chosen.items()):
        places, gates, suspect, added, by_ways, _ = results[name]
        points = []
        if places is not None and not is_airport:
            mine = {rc: data[rc] for rc in cells if rc in data}
            unique = list({(e["type"], e["id"]): e for d in mine.values() for e in d["elements"]}.values())
            points = [p + (name,) for p in store_points(unique, places, name, test, skip=used, floors_seen=disputes)]
        every_point += points
        print(f"{name}: {len(by_ways)} gates floored from their ways, {len(gates)} from neighbours, {len(added)} units named from Overture, "
              f"{len(points)} store points ({sum(p[6] == 'address' for p in points)} by address)")
        for gid, label, f, v in suspect:
            print(f"  gate {label or gid} (node {gid}) is on {f} in OSM, every neighbour on {v}: check in OSM")
        if report is not None:
            report[name] = {"gates_floored": {str(k): v for k, v in sorted(gates.items())},
                            "gates_floored_from_ways": {str(k): v for k, v in sorted(by_ways.items())},
                            "suspect_gate_levels": suspect,
                            "units_named": {str(k): v[0] for k, v in sorted(added.items())},
                            "store_points": [{"name": p[1], "level": p[5], "source": p[6], "ref:overture": p[0]} for p in points]}
    if overture:
        # Two overlapping venues can find one place. Floored differently (each settles its own
        # numbering), nothing says which is right and it's left out; else the venue first by name
        # keeps it (a name cleaned of either venue's suffix).
        place_points(data, sorted((p for p in every_point if len(disputes[p[0]]) == 1), key=lambda p: (p[0], p[-1])))
    changed = sum(save(rc, d) for rc, d in data.items() if json.dumps(d, sort_keys=True) != before[rc])
    print(f"{changed} cells changed")
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
