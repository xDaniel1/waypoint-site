#!/usr/bin/env python3
"""Takes floors with no named place off the bundled indoor cells, so they get no floor button.

    python3 Scripts/indoor_prune.py            # every bundled venue
    python3 Scripts/indoor_prune.py --dry-run  # say what would go, change nothing

The rule: a floor gets a button only when at least one named place is on it. The app lists a
floor when anything but stairs, lifts and outlines is on it (`IndoorViewModel.floorValues`), so
a floor holding only unnamed rooms, corridors or walls, or reached only by stairs, lifts and
escalators, shows a button for an empty plan. Such floors are taken out of the data:

- A **named place** (`named_place`) is a gate (named or not: the app labels it by its `ref`), a
  restroom (the app calls an unnamed one "Restroom"), or any shop, amenity, room, area or
  corridor with a `name` or `ref`, spanning at most `MAX_FLOORS` floors (a shaft's floors are no
  floor of the plan). A room or area named only for a stair, lift or escalator ("Escalator" at
  ABQ) is a stair, not a place. A station, bus station, taxi or rental desk with a floor is an
  amenity on that floor, as in the app (`plan_kind`, `IndoorMapParsing.isIndoorLandside`).
- Floors are judged where the app lists them (`Bundle`): per venue outline (a terminal, station
  or mall ring the app can pick, with the gates on its piers) for what's inside one, keeping the
  floors every such outline has named places on; per inventory venue area for the rest (a
  stricter stand-in: the app offers no floors away from an outline).
- Every other element that lists floors (rooms, corridors, areas, walls, amenities) loses the
  floors its venues don't keep: `level` and `repeat_on` are rewritten to the kept ones and the
  removed ones are recorded in `level:pruned`. An element left on no floor is dropped from every
  cell it sits in. Stairs, lifts and shafts (more than `MAX_FLOORS` floors) list no floor while
  anything else does, so they're left whole, except in a venue that keeps no floor at all
  (CVG, DTW's McNamara Terminal): there they'd be all it lists, and go. Gates, outlines, ground
  places and named places are never changed; an unnamed station loses its floor but stays.

`fetch_indoor_venues.py` runs this after `indoor_enrich.py` (whose Overture names can make a floor
named), over every venue, since a rebuilt cell can hold more than one venue's elements. Pruning is
idempotent: run again, it changes nothing. `indoor_inventory.py --check` fails on a floor a venue
area or outline lists with no named place.
"""
import collections, json, math, re, sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch_indoor_venues as fv  # noqa: E402
import indoor_inventory as inv  # noqa: E402

MAX_FLOORS = 4  # IndoorViewModel.maxFloorsSpanned
# Never touched: gates keep every floor they're on, outlines' and street-level places' floors
# aren't plan floors.
UNTOUCHED = {None, "footprint", "landside", "gate"}
# A room or area named for nothing but a way up or down: "Escalator", "Emergency Stairs West".
_SIDE = r"(emergency|east|west|north|south|main|central|[a-z]|\d+)"
CONVEYANCE = re.compile(rf"^\s*({_SIDE}\s+)*(escalators?|elevators?|lifts?|stairs?|stairways?|stairwells?|steps)"
                        rf"(\s+{_SIDE})*\s*$", re.I)


def label(tags):
    return (tags.get("name") or "").strip() or (tags.get("ref") or "").strip()


def indoor_landside(tags):
    """IndoorMapParsing.isIndoorLandside: a station, bus station, taxi or rental desk or pick-up
    point (not a car park) with a floor. Inside a terminal the app puts it on that plan floor
    (DFW's Skylink stops, SEA's satellite trains); outside every outline it is a ground place."""
    a = tags.get("amenity")
    return (fv.landside(tags) and a not in ("parking",) and "indoor" not in tags
            and ("level" in tags or "repeat_on" in tags))


def stitch(segments):
    """Closed rings from a relation's outline ways (IndoorMap.swift's `stitch`, roughly)."""
    segs = [[(p["lat"], p["lon"]) for p in g] for g in segments if g and len(g) >= 2]
    rings = []
    while segs:
        ring = segs.pop(0)
        while ring[0] != ring[-1]:
            for i, sg in enumerate(segs):
                if sg[0] == ring[-1]:
                    ring += sg[1:]
                elif sg[-1] == ring[-1]:
                    ring += sg[::-1][1:]
                elif sg[-1] == ring[0]:
                    ring = sg[:-1] + ring
                elif sg[0] == ring[0]:
                    ring = sg[::-1][:-1] + ring
                else:
                    continue
                segs.pop(i)
                break
            else:
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def outline_rings(el):
    if el["type"] == "way":
        return stitch([el.get("geometry") or []])
    return stitch([m.get("geometry") for m in el.get("members") or [] if (m.get("role") or "") in ("outer", "")])


def in_ring(point, ring):
    lat, lon = point
    hit = False
    for (alat, alon), (blat, blon) in zip(ring, ring[1:]):
        if (alat > lat) != (blat > lat) and lon < (blon - alon) * (lat - alat) / (blat - alat) + alon:
            hit = not hit
    return hit


def plan_kind(el):
    """What the app makes of an element: the inventory's class, except that a station or desk with
    a floor is an amenity on it (`isIndoorLandside`), and a `room=toilets` with neither `indoor`
    nor `amenity` is nothing (`IndoorMapParsing.kind` drops it)."""
    kind = inv.classify(el)
    t = el.get("tags", {})
    if kind == "landside" and indoor_landside(t):
        return "amenity"
    if kind == "restroom" and "indoor" not in t and "amenity" not in t:
        return None
    return kind


def ring_metres(point, ring):
    """Distance from a point to a ring's edge, in metres (IndoorViewModel.boundaryDistance)."""
    lat0 = point[0]
    k = math.cos(math.radians(lat0)) * 111_320

    def xy(p):
        return ((p[1] - point[1]) * k, (p[0] - lat0) * 111_320)
    best = float("inf")
    for a, b in zip(ring, ring[1:]):
        (ax, ay), (bx, by) = xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / max(dx * dx + dy * dy, 1e-12)))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def ring_area(ring):
    return abs(sum(a[1] * b[0] - b[1] * a[0] for a, b in zip(ring, ring[1:]))) / 2


class Outline:
    """One ring of a terminal, station or mall outline: a venue when the app would make it one."""

    def __init__(self, el, ring):
        t = el.get("tags", {})
        self.key, self.ring, self.is_mall = (el["type"], el["id"]), ring, t.get("shop") == "mall"
        self.name = t.get("name") or f"{el['type']} {el['id']}"
        lats, lons = [p[0] for p in ring], [p[1] for p in ring]
        self.bbox = (min(lats), min(lons), max(lats), max(lons))
        self.area = ring_area(ring)
        self.contents, self.piers = [], []

    def holds(self, el, c):
        """IndoorViewModel.isInside: the centroid `c` inside, or a point on the wall (within 1.5 m)."""
        if not c:
            return False
        s, w, n, e = self.bbox
        pad = 0.00003
        if not (s - pad <= c[0] <= n + pad and w - pad <= c[1] <= e + pad):
            return False
        return in_ring(c, self.ring) or (el["type"] == "node" and ring_metres(c, self.ring) < 1.5)


_UNSET = object()


def named_place(el, kind=_UNSET):
    """Whether this element makes its floors worth a button (see the module docstring)."""
    kind = plan_kind(el) if kind is _UNSET else kind
    t = el.get("tags", {})
    fl = inv.floors(t)
    if not fl or len(fl) > MAX_FLOORS:
        return False
    if kind == "gate" or kind == "restroom":
        return True
    if kind in ("shop", "food", "amenity"):
        return bool(label(t))
    if kind in ("room", "area", "corridor"):
        return bool(label(t)) and not CONVEYANCE.match(label(t))
    return False


def listed_floors(els, credited=lambda el, f: True, kind=None):
    """IndoorViewModel.floorValues over a venue's elements: what the floor button lists.
    `credited(el, f)` says whether an element's floor counts for this venue (see `Bundle`)."""
    kind = kind or plan_kind
    every, specific = set(), set()
    for el in els:
        k = kind(el)
        if k in (None, "footprint", "landside"):
            continue
        fl = [f for f in inv.floors(el.get("tags", {})) if credited(el, f)]
        every.update(fl)
        if k not in ("stairs", "elevator") and len(inv.floors(el.get("tags", {}))) <= MAX_FLOORS:
            specific.update(fl)
    return sorted(specific or every)


def kept_floors(els, kind=None):
    kind = kind or plan_kind
    return sorted({f for el in els if named_place(el, kind(el)) for f in inv.floors(el.get("tags", {}))})


def venue_elements(cells, area):
    """The unique elements whose centre is inside a venue's area (as the inventory counts them)."""
    _, inside, area_cells, _ = area
    seen, out = set(), []
    for rc in area_cells:
        for el in cells.get(rc, {}).get("elements", []):
            key = (el["type"], el["id"])
            if key in seen:
                continue
            seen.add(key)
            c = inv.centre(el)
            if c and inside(*c):
                out.append(el)
    return out


def level_text(values):
    return ";".join(fv.level_text(v) for v in sorted(values))


def strip(tags, keep):
    """Rewrites `level` and `repeat_on` to the floors in `keep`; returns the floors removed."""
    removed = set()
    for key in ("level", "repeat_on"):
        if key not in tags:
            continue
        values = inv.levels(tags[key])
        if not values:
            continue  # unreadable: the app doesn't list it either
        gone = [v for v in values if v not in keep]
        if not gone:
            continue
        removed.update(gone)
        left = [v for v in values if v in keep]
        if left:
            tags[key] = level_text(left)
        else:
            del tags[key]
    if removed:
        before = set(inv.levels(tags.get("level:pruned")))
        tags["level:pruned"] = level_text(before | removed)
    return removed


MIN_CONTENTS = 8  # IndoorViewModel.minimumFeatures
MIN_TENANTS = 3  # IndoorViewModel.minimumMallTenants
PIER_REACH = 150.0  # IndoorViewModel.owner(of:among:)
PASSAGES = {"gate", "entrance", "exit", "door", "doors", "parking", "stairs", "escalator", "escalators",
            "elevator", "elevators"}


def mall_tenant(el, kind):
    """IndoorViewModel.isMallTenant, roughly: a store, eatery or named room, not a lift or station."""
    t = el.get("tags", {})
    if kind == "room":
        name = (t.get("name") or "").strip()
        words = set(re.split(r"[^\w']+", name.lower()))
        return (bool(name) and not name.replace(" ", "").isdigit() and not words & PASSAGES
                and not words & {"restroom", "restrooms", "toilet", "toilets", "wc"} and "information" not in name.lower())
    return kind in ("shop", "food", "amenity", "restroom") and not fv.landside(t)


class Bundle:
    """Which floors each venue lists and keeps, as the app works them out.

    The app's venue is one outline (a terminal, station or mall ring with at least MIN_CONTENTS
    things inside, MIN_TENANTS stores for a mall): its floor button lists the floors of what's
    inside it plus the gates on its piers (`IndoorViewModel.venueOutlines`, `makeNeighbourhood`).
    So an element inside venue outlines keeps only the floors every one of them has named places
    on: the camera may pick any of them (SFO's Boarding Area C, inside Harvey Milk Terminal 1).

    With no outline under the camera the app offers no floors. Elements inside no venue outline
    are judged by their inventory venue area instead, a stricter stand-in that also catches a
    floor nothing in the whole venue names (STL 0, Dadeland 2). Areas overlap where the app's
    venues don't (Back Bay Station's circle takes in Copley Place corridors on Prudential Center's
    floors): such an element keeps the floors any of its areas keeps, and a floor of it counts
    toward an area only when that area keeps the floor or the element may not keep it at all."""

    def __init__(self, cells, areas):
        self.cells, self.areas = cells, areas
        self.els = {name: venue_elements(cells, area) for name, area in areas.items()}
        self.area_of = collections.defaultdict(list)
        for name, els in self.els.items():
            for el in els:
                self.area_of[(el["type"], el["id"])].append(name)
        unique, self.cell_of = {}, {}
        for rc, d in cells.items():
            for el in d["elements"]:
                key = (el["type"], el["id"])
                if key not in unique:
                    unique[key], self.cell_of[key] = el, rc
        self.kinds = {key: plan_kind(el) for key, el in unique.items()}
        outlines = [Outline(el, ring) for key, el in unique.items() if self.kinds[key] == "footprint"
                    for ring in outline_rings(el)]
        placed = [(key, el) for key, el in unique.items() if self.kinds[key] not in (None, "footprint", "landside")]
        centres = [inv.centre(el) for _, el in placed]
        for o in outlines:
            o.contents = [el for (_, el), c in zip(placed, centres) if o.holds(el, c)]
        self.outlines = [o for o in outlines
                         if sum(self.kinds[(e["type"], e["id"])] != "elevator" for e in o.contents) >= MIN_CONTENTS
                         and (not o.is_mall or sum(mall_tenant(e, self.kinds[(e["type"], e["id"])]) for e in o.contents)
                              >= MIN_TENANTS)]
        self.outline_of = collections.defaultdict(list)
        for i, o in enumerate(self.outlines):
            for el in o.contents:
                self.outline_of[(el["type"], el["id"])].append(i)
        # A gate outside its building, nearest its wall: on its plan and floor list too.
        for key, el in placed:
            if self.kinds[key] != "gate" or el["type"] != "node":
                continue
            c = (el["lat"], el["lon"])
            inside = [i for i, o in enumerate(self.outlines) if in_ring(c, o.ring)]
            if inside:
                owner = min(inside, key=lambda i: self.outlines[i].area)
            else:
                dlat = PIER_REACH / 111_320
                dlon = dlat / math.cos(math.radians(c[0]))
                near = [(ring_metres(c, o.ring), i) for i, o in enumerate(self.outlines)
                        if o.bbox[0] - dlat <= c[0] <= o.bbox[2] + dlat and o.bbox[1] - dlon <= c[1] <= o.bbox[3] + dlon]
                near = [(d, i) for d, i in near if d < PIER_REACH]
                owner = min(near)[1] if near else None
            if owner is not None and owner not in self.outline_of[key]:
                self.outlines[owner].piers.append(el)
        self.outline_keep = [set(kept_floors(o.contents + o.piers, self.kind)) for o in self.outlines]
        self.area_keep = {name: set(kept_floors(els, self.kind)) for name, els in self.els.items()}

    def kind(self, el):
        key = (el["type"], el["id"])
        return self.kinds[key] if key in self.kinds else plan_kind(el)

    def allowed(self, el):
        """The floors an element may keep: those every venue outline it's in keeps (the app shows
        whichever one the camera picks, all of its contents), or, inside none, those any of its
        venue areas keeps."""
        key = (el["type"], el["id"])
        if self.outline_of.get(key):
            return set.intersection(*(self.outline_keep[i] for i in self.outline_of[key]))
        return set().union(*(self.area_keep[a] for a in self.area_of.get(key, []))) if self.area_of.get(key) else None

    def _credit(self, keep):
        return lambda el, f: f in keep or f not in (self.allowed(el) or set())

    def listed(self, venue):
        """What a venue area would list taken whole: the inventory's Floors column. The app never
        lists a whole area (with no outline under the camera it offers no floors), so this is a
        stricter stand-in: it catches a floor nothing names anywhere in the venue."""
        return listed_floors(self.els[venue], self._credit(self.area_keep[venue]), self.kind)

    def unnamed(self, venue):
        return [f for f in self.listed(venue) if f not in self.area_keep[venue]]

    def outline_unnamed(self, i):
        """Floors an outline's button lists with nothing named inside it on them: exactly the app's
        list (`floorValues` over its contents and pier gates), no element credited elsewhere."""
        o, keep = self.outlines[i], self.outline_keep[i]
        return [f for f in listed_floors(o.contents + o.piers, kind=self.kind) if f not in keep]

    def outline_area(self, i):
        """The venue area an outline's centre (or first corner) is in, for reports."""
        ring = self.outlines[i].ring
        cen = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
        for name, (_, inside, _, _) in self.areas.items():
            if inside(*cen) or inside(*ring[0]):
                return name
        # Outside every area but in a bundled cell (Montgomery and Powell Street Stations, in the
        # Salesforce Transit Center's cells): the venue whose cells hold it, else the nearest.
        rcs = {fv.cell(*cen), self.cell_of.get(self.outlines[i].key)}
        owner = next((name for name, (_, _, cells, _) in self.areas.items() if rcs & set(cells)), None)
        if owner or not self.areas:
            return owner

        def gap(name):
            box = self.areas[name][3]
            mid = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) if box else fv.VENUES[name]
            return inv.metres(cen, mid)
        return min(self.areas, key=gap)

    def problems(self):
        """[(venue area, outline name or None, [floors listed with no named place])]."""
        out = [(name, None, gone) for name in self.areas if (gone := self.unnamed(name))]
        out += [(self.outline_area(i), o.name, gone) for i, o in enumerate(self.outlines)
                if (gone := self.outline_unnamed(i))]
        return out


def prune_cells(cells, areas, names=None):
    """Prunes {(row, col): cell JSON} in place over `areas` (inventory.venue_areas()); only
    elements of the named venue areas when `names` is given. Returns (the problems found before,
    as `Bundle.problems`, the number of elements dropped)."""
    b = Bundle(cells, areas)
    found = [p for p in b.problems() if names is None or p[0] in names]
    dropped = set()
    # An outline is the named venue's when its problems are reported under it (`outline_area`).
    outline_venue = {i: b.outline_area(i) for i in range(len(b.outlines))} if names is not None else {}
    for d in cells.values():
        for el in d["elements"]:
            key = (el["type"], el["id"])
            if names is not None and not (set(b.area_of.get(key, [])) | {outline_venue[i] for i in b.outline_of.get(key, [])}) & set(names):
                continue
            kind = b.kind(el)
            if kind in UNTOUCHED:
                continue
            t = el.get("tags", {})
            fl = inv.floors(t)
            keep = b.allowed(el)
            if not fl or keep is None or named_place(el, kind) or (label(t) and not CONVEYANCE.match(label(t))):
                continue  # a named place keeps every floor it's on, even a shaft's
            # Stairs, lifts and shafts list no floor while anything else does: they're left
            # whole, unless one of their venues keeps no floor at all (then they'd list theirs).
            units = ([b.outline_keep[i] for i in b.outline_of[key]] if b.outline_of.get(key)
                     else [b.area_keep[a] for a in b.area_of[key]])
            if (kind in ("stairs", "elevator") or len(fl) > MAX_FLOORS) and all(units):
                continue
            strip(t, keep)
            # A station or desk left with no floor is still a ground place: it stays, unfloored.
            if not inv.floors(t) and t.get("level:pruned") and not fv.landside(t):
                dropped.add(key)
    for d in cells.values():
        d["elements"] = [el for el in d["elements"] if (el["type"], el["id"]) not in dropped]
    return found, len(dropped)


def problems():
    """`Bundle.problems` over the bundle, for `indoor_inventory --check`."""
    return Bundle(inv.load_cells(), inv.venue_areas()).problems()


def save(rc, data):
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    path = fv.OUT / f"indoor_{rc[0]}_{rc[1]}.json"
    if path.read_bytes() != body:
        path.write_bytes(body)
        return True
    return False


def describe(found):
    lines = []
    for area, outline, gone in found:
        where = f"{area or '(no venue)'}" + (f" / {outline}" if outline else "")
        lines.append(f"{where}: no named place on {', '.join(inv.level_label(f) for f in gone)}")
    return lines


def prune(names=None, dry_run=False):
    """Prunes the bundle (only elements of the named venue areas when given); returns what it found."""
    cells = inv.load_cells()
    found, dropped = prune_cells(cells, inv.venue_areas(), names)
    changed = 0 if dry_run else sum(save(rc, d) for rc, d in cells.items())
    print("\n".join(describe(found)))
    print(f"{len(found)} venues or outlines with unnamed floors, {dropped} elements dropped, {changed} cells changed"
          f"{' (dry run)' if dry_run else ''}")
    return found


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    prune(args or None, dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    main()
