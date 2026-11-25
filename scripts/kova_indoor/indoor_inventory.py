#!/usr/bin/env python3
"""Inventory of the bundled indoor venues, and checks of their data.

    python3 Scripts/indoor_inventory.py            # table per venue (markdown)
    python3 Scripts/indoor_inventory.py --json     # the same, as JSON (for the Apple Maps diff)
    python3 Scripts/indoor_inventory.py --issues   # also list every data problem found
    python3 Scripts/indoor_inventory.py --apple-diff [path]   # summarise IndoorAppleAuditTests' report
    python3 Scripts/indoor_inventory.py --check    # exit 1 on lost coverage (missing cells, outlines)
                                                   # or a floor button with no named place on it

Reads only `Waypoint/Resources/IndoorVenues`. Venues are the areas `fetch_indoor_venues.py`
builds (AIRPORTS and MALLS boxes, VENUES points with the radius the app lists them with); a
bundled cell inside none of them is reported as an orphan.

The classification mirrors `IndoorMapParsing` (Waypoint/Models/IndoorMap.swift) closely enough
to count what the app will show; it is not a second parser to keep in step.
"""
import collections, json, math, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch_indoor_venues as fv  # noqa: E402  (AIRPORTS, MALLS, VENUES, cell maths)

DIR = fv.OUT
FOOD = {"restaurant", "cafe", "fast_food", "bar", "pub", "food_court", "ice_cream", "biergarten"}
# Radii the app lists the point venues with (KnownVenue.bundled); 200 m where it has none.
POINT_RADIUS = {
    "Grand Central": 230, "Oculus": 170, "Brookfield Place": 200, "Fulton Center": 90, "Penn Station": 220,
    "Moynihan Train Hall": 120, "Port Authority": 160, "Hudson Yards": 140, "Staten Island Mall": 320,
    "Roosevelt Field": 320, "Newport Centre": 180, "Jamaica Station": 160, "Atlantic Terminal": 150,
    "Macy's Herald Square": 110, "Columbus Circle": 110,
}
KIND = {**{k: "airport" for k in fv.AIRPORTS}, **{k: "mall" for k in fv.MALLS}, **{k: "station" for k in fv.STATIONS},
        **{k: "other" for k in fv.HALLS},
        "Grand Central": "station", "Oculus": "mall", "Brookfield Place": "mall", "Fulton Center": "station",
        "Penn Station": "station", "Moynihan Train Hall": "station", "Port Authority": "station",
        "Hudson Yards": "mall", "Staten Island Mall": "mall", "Roosevelt Field": "mall", "Newport Centre": "mall",
        "Jamaica Station": "station", "Atlantic Terminal": "station", "Macy's Herald Square": "mall",
        "Columbus Circle": "mall"}


def levels(raw):
    """IndoorMapParsing.levels: `;` parts, each a number, a comma list or a range."""
    if not raw:
        return []
    out = set()
    for piece in raw.split(";"):
        piece = piece.strip()
        commas = [c.strip() for c in piece.split(",")]
        is_list = (len(commas) >= 2 and all(c.lstrip("+-").isdigit() for c in commas)
                   and not (len(commas) == 2 and commas[1] == "5"))
        for t in (commas if is_list else [piece]):
            t = t.replace(",", ".")
            try:
                out.add(float(t))
                continue
            except ValueError:
                pass
            if len(t) > 2 and "-" in t[1:]:
                i = t.index("-", 1)
                try:
                    a, b = float(t[:i]), float(t[i + 1:])
                except ValueError:
                    continue
                lo, hi = min(a, b), max(a, b)
                if hi - lo <= 60:
                    v = lo
                    while v <= hi:
                        out.add(v)
                        v += 1
    return sorted(out)


def floors(tags):
    return sorted(set(levels(tags.get("level")) + levels(tags.get("repeat_on"))))


def centre(el):
    if el["type"] == "node":
        pts = [el]
    elif el["type"] == "way":
        pts = el.get("geometry") or []
    else:
        pts = [p for m in el.get("members") or [] for p in (m.get("geometry") or []) if p]
    if not pts or "lat" not in pts[0]:
        return None
    return sum(p["lat"] for p in pts) / len(pts), sum(p["lon"] for p in pts) / len(pts)


def metres(a, b):
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def closed(el):
    g = el.get("geometry") or []
    return el["type"] == "relation" or (len(g) >= 4 and g[0] == g[-1])


def classify(el):
    """What the app makes of an element: footprint, gate, shop, food, restroom, amenity, room,
    corridor, area, stairs, elevator, landside, or None."""
    t = el.get("tags", {})
    if el["type"] != "node" and closed(el) and "indoor" not in t and (
            t.get("aeroway") == "terminal" or t.get("building") in ("train_station", "transportation")
            or t.get("shop") == "mall" or fv.is_hall(t)):
        return "footprint"
    if t.get("access") in ("private", "no", "permit") and ("shop" in t or "amenity" in t) and not fv.landside(t) \
            and "indoor" not in t:
        return None
    if t.get("aeroway") == "gate":
        return "gate"
    if t.get("highway") == "elevator":
        return "elevator"
    if t.get("highway") == "steps":
        return "stairs"
    if "shop" in t:
        return "shop" if (el["type"] == "node" or closed(el)) else None
    if fv.landside(t) and "indoor" not in t:
        return "landside"
    a = t.get("amenity")
    if a and a not in fv.NOISE_AMENITIES and ("level" in t or "repeat_on" in t or "indoor" in t):
        if not (el["type"] == "node" or closed(el)):
            return None
        if a in FOOD:
            return "food"
        if a == "toilets":
            return "restroom"
        return "amenity"
    if t.get("room") in ("toilets", "toilet", "bathroom"):
        return "restroom"
    ind = t.get("indoor")
    if ind in ("room", "yes"):
        return "room" if closed(el) else None
    if ind in ("corridor", "area"):
        return ind if closed(el) else None
    if ind == "wall":
        return "wall"
    return None


def load_cells():
    cells = {}
    for f in sorted(DIR.glob("indoor_*.json")):
        r, c = f.stem.split("_")[1:]
        cells[(int(r), int(c))] = json.loads(f.read_text())
    return cells


def venue_areas():
    """name -> (kind, test(lat, lon) -> bool, [cells])"""
    areas = {}
    for name, box in {**fv.AIRPORTS, **fv.MALLS, **fv.STATIONS, **fv.HALLS}.items():
        s, w, n, e = box
        areas[name] = (KIND[name], (lambda s=s, w=w, n=n, e=e: lambda la, lo: s <= la <= n and w <= lo <= e)(),
                       fv.cells_in(box), box)
    for name, (lat, lon) in fv.VENUES.items():
        r = POINT_RADIUS.get(name, 200)
        areas[name] = (KIND[name], (lambda lat=lat, lon=lon, r=r: lambda la, lo: metres((la, lo), (lat, lon)) <= r)(),
                       [fv.cell(lat, lon)], None)
    return areas


def inventory():
    import indoor_prune
    cells = load_cells()
    areas = venue_areas()
    # What the floor button lists, as the app works it out (walls count, stairs and lifts only when
    # nothing else has a floor), with an element in two venues' areas credited to the one that
    # keeps its floor, and each terminal, station or mall outline the app can pick checked on its
    # own (indoor_prune.Bundle).
    bundle = indoor_prune.Bundle(cells, areas)
    by_outline = collections.defaultdict(list)
    for area, outline, gone in bundle.problems():
        if outline:
            by_outline[area].append((outline, gone))
    used = set()
    report = []
    for name, (kind, inside, area_cells, box) in areas.items():
        present = [rc for rc in area_cells if rc in cells]
        used.update(present)
        seen, els = set(), []
        for rc in present:
            for el in cells[rc]["elements"]:
                key = (el["type"], el["id"])
                if key in seen:
                    continue
                seen.add(key)
                c = centre(el)
                if c and inside(*c):
                    els.append(el)
        counts = collections.Counter()
        per_level = collections.defaultdict(collections.Counter)
        outlines, issues = [], []
        named_places = []
        for el in els:
            k = classify(el)
            if not k:
                continue
            counts[k] += 1
            t = el.get("tags", {})
            fl = floors(t)
            if k == "footprint":
                outlines.append(t.get("name") or f"(unnamed {el['type']} {el['id']})")
                continue
            if k in ("landside", "wall"):
                continue
            for f in fl:
                per_level[f][k] += 1
            if k == "gate" and fl:
                counts["gate_with_floor"] += 1
                if t.get("level:inferred") in ("gates", "outline"):
                    counts["gate_floor_inferred"] += 1  # indoor_enrich.py, from neighbouring gates or the terminal outline
                elif t.get("level:inferred") == "ways":
                    counts["gate_floor_ways"] += 1  # fetch_indoor_venues.py, from the ways the gate is on
            if t.get("name:source") == "overture":
                counts["overture_named"] += 1  # indoor_enrich.py: an OSM unit named from Overture
            if k in ("shop", "food", "amenity", "restroom") and t.get("name"):
                counts[k + "_named"] += 1
                named_places.append((t["name"], tuple(fl), centre(el), el))
            if k in ("shop", "food", "amenity", "restroom", "room") and not fl:
                if k == "room" and not t.get("name"):
                    continue
                issues.append(f"no floor: {k} {t.get('name') or ''} ({el['type']}/{el['id']})")
            if "level" in t and not levels(t["level"]):
                issues.append(f"unreadable level {t['level']!r}: {t.get('name') or k} ({el['type']}/{el['id']})")
        # The same name on the same floor twice within 25 m: a duplicate (node + outline, or a stale copy).
        by_name = collections.defaultdict(list)
        for nm, fl, c, el in named_places:
            by_name[(nm.lower(), fl)].append((c, el))
        dupes = 0
        for (nm, fl), group in by_name.items():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if metres(group[i][0], group[j][0]) < 25:
                        dupes += 1
                        issues.append(f"duplicate {nm!r} on {list(fl)}: {group[i][1]['type']}/{group[i][1]['id']} "
                                      f"and {group[j][1]['type']}/{group[j][1]['id']} "
                                      f"{metres(group[i][0], group[j][0]):.0f} m apart")
        # A floor with places but no rooms, corridors or areas drawn: labels floating on an empty plan.
        structural = {"room", "corridor", "area"}
        bare = sorted(f for f, c in per_level.items()
                      if sum(c[k] for k in ("shop", "food", "amenity", "restroom")) >= 3
                      and not any(c[k] for k in structural))
        # A floor holding only a stray feature or two next to well-mapped floors: usually a typo.
        total = sum(sum(c.values()) for c in per_level.values())
        stray = sorted(f for f, c in per_level.items() if sum(c.values()) <= 2 and total >= 40)
        missing_cells = [rc for rc in area_cells if rc not in cells]
        report.append({
            "name": name, "kind": kind, "cells": len(present), "cells_expected": len(area_cells),
            "missing_cells": missing_cells, "landside_flag": all(cells[rc].get("landside") for rc in present) if present else False,
            "generators": sorted({cells[rc].get("generator", "?").split(" 0")[0] for rc in present}),
            "outlines": sorted(set(outlines)), "levels": bundle.listed(name),
            "unnamed_levels": bundle.unnamed(name), "unnamed_outline_levels": by_outline.get(name, []),
            "all_levels": sorted(per_level),
            "counts": dict(counts), "per_level": {str(k): dict(v) for k, v in sorted(per_level.items())},
            "bare_levels": bare, "stray_levels": stray, "duplicates": dupes, "issues": issues,
            "box": box, "center": None if box else list(fv.VENUES[name]),
            "radius": None if box else POINT_RADIUS.get(name, 200),
        })
    orphans = sorted(set(cells) - used)
    return report, orphans


def level_label(v):
    if v < 0:
        return f"B{abs(v):g}"
    return f"{v:g}"


def apple_diff_summary(path):
    """Markdown for docs/indoor-audit.md from IndoorAppleAuditTests' apple-diff.json.

    Counts, distances and our own (OpenStreetMap) ids and names only: Apple's place names,
    positions and addresses stay in the git-ignored JSON on the machine that ran the audit.
    """
    import re
    ours = re.compile(r"((?:osm|gate):\d+)(?: “([^”]*)”)?")
    rows = json.loads(pathlib.Path(path).read_text())
    order = {name: i for i, name in enumerate(venue_areas())}
    rows.sort(key=lambda r: order.get(r["venue"], 999))
    print("| Venue | Apple places | Our places (+gates) | Matched (loose) | Apple-only shops/food | Ours not at Apple | Position > 25 m | Floor disagrees (old rule) | Dupes | Apple places floored by: twin / stated / room / neighbour / guess |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        fs = r.get("floorSources", {})
        apple_only_sf = sum(1 for f in r["appleOnly"] if f["category"] in ("shop", "food"))
        print(f"| {r['venue']} | {r['applePlaces']} | {r['ourPlaces']} (+{r['ourGates']}) | {r['matched']} ({r['matchedLoosely']}) | "
              f"{apple_only_sf} | {len(r['ourOnly'])} | {len(r['positionOff'])} | {len(r['floorDisagrees'])}"
              f"{' (' + str(r['floorDisagreesByOldRule']) + ')' if 'floorDisagreesByOldRule' in r else ''} | {len(r['duplicates'])} | "
              f"{fs.get('twin', 0)} / {fs.get('stated', 0)} / {fs.get('room', 0)} / {fs.get('neighbour', 0)} / {fs.get('mainFloor', 0)} |")
    for r in rows:
        notes = []
        for f in r["positionOff"][:8]:
            m, metres = ours.search(f["note"]), f["note"].split(" m ")[0]
            if m:
                notes.append(f"  - {metres} m from Apple's: our {m.group(1)} “{m.group(2) or ''}”")
        for f in r["floorDisagrees"][:8]:
            m = ours.search(f["note"])
            if m:
                notes.append(f"  - floor disagrees with its stated address: our {m.group(1)}")
        for f in r["duplicates"][:5]:
            notes.append(f"  - duplicate: {f['name']} — {f['note']}")
        stale = [f["name"] for f in r["ourOnly"]][:12]
        if stale:
            notes.append(f"  - ours, not at Apple (closed or renamed?): {', '.join(stale)}" + (" …" if len(r["ourOnly"]) > 12 else ""))
        if notes:
            print(f"\n- **{r['venue']}**")
            print("\n".join(notes))


# Gaps in OpenStreetMap itself, known and reported in docs/indoor-audit.md, that `--check` doesn't fail on.
KNOWN_GAPS = {
    ("Moynihan Train Hall", "outline"): "OSM maps the hall as an indoor=area; the Farley Building has no station tag",
}


# Floors a venue must keep listing, each backed by mapped places or outlines (not just stairs):
# Grand Central's B2 holds the lower-level track platforms (102-115) and the 42nd Street subway
# platforms, reached by three stairs from B1 30 m from the concourse (2026-11-23).
REQUIRED_FLOORS = {"Grand Central": [-2.0]}


def check():
    """Exit non-zero when the bundle has lost coverage: a listed venue with cells missing or
    without its outline, a cell that isn't valid or wasn't built by the pipeline with the
    airport (landside) query, a cell no venue owns, or a floor the venue, or one of its terminal,
    station or mall outlines, lists with no named place on it. For CI or before committing a
    refresh."""
    report, orphans = inventory()
    problems = []
    for v in report:
        if v["missing_cells"]:
            problems.append(f"{v['name']}: cells not bundled {v['missing_cells']}")
        if v["kind"] in ("airport", "station") and not v["outlines"] and (v["name"], "outline") not in KNOWN_GAPS:
            problems.append(f"{v['name']}: no terminal or station outline")
        # indoor_prune.py: a floor gets a button only when a named place is on it.
        for f in v["unnamed_levels"]:
            problems.append(f"{v['name']}: floor {level_label(f)} is listed with no named place (run indoor_prune.py)")
        for outline, gone in v["unnamed_outline_levels"]:
            problems.append(f"{v['name']}: {outline} lists {', '.join(level_label(f) for f in gone)} "
                            "with no named place inside it (run indoor_prune.py)")
        for f in REQUIRED_FLOORS.get(v["name"], []):
            if f not in v["levels"]:
                problems.append(f"{v['name']}: floor {level_label(f)} is no longer listed")
        if not v["landside_flag"]:
            problems.append(f"{v['name']}: a cell lacks the landside flag (not rebuilt by the pipeline)")
    if orphans:
        problems.append(f"cells in no venue: {orphans}")
    for line in problems:
        print("FAIL", line)
    print(f"{len(report)} venues, {len(problems)} problems")
    return 1 if problems else 0


def main():
    if "--check" in sys.argv:
        sys.exit(check())
    if "--apple-diff" in sys.argv:
        apple_diff_summary(sys.argv[sys.argv.index("--apple-diff") + 1] if len(sys.argv) > sys.argv.index("--apple-diff") + 1
                           else DIR.parent.parent.parent / "docs/indoor-audit/apple-diff.json")
        return
    report, orphans = inventory()
    if "--json" in sys.argv:
        print(json.dumps({"venues": report, "orphans": orphans}, indent=1))
        return
    print("| Venue | Kind | Cells | Outlines | Floors | Gates (with floor) | Shops | Food | Restrooms | Other amenities | Rooms/areas | Lifts/stairs | Dupes | Bare floors |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for v in report:
        c = v["counts"]
        cells = f"{v['cells']}/{v['cells_expected']}"
        print(f"| {v['name']} | {v['kind']} | {cells} | {len(v['outlines'])} | "
              f"{', '.join(level_label(x) for x in v['levels']) or '—'} | {c.get('gate', 0)} ({c.get('gate_with_floor', 0)}"
              f"{', ' + str(c['gate_floor_ways']) + ' from ways' if c.get('gate_floor_ways') else ''}"
              f"{', ' + str(c['gate_floor_inferred']) + ' from neighbours' if c.get('gate_floor_inferred') else ''}) | "
              f"{c.get('shop', 0)} | {c.get('food', 0)} | {c.get('restroom', 0)} | {c.get('amenity', 0)} | "
              f"{c.get('room', 0) + c.get('corridor', 0) + c.get('area', 0)} | {c.get('elevator', 0) + c.get('stairs', 0)} | "
              f"{v['duplicates']} | {', '.join(level_label(x) for x in v['bare_levels']) or '—'} |")
    if orphans:
        print(f"\nCells in no listed venue: {', '.join(f'{r}_{c}' for r, c in orphans)}")
    if "--issues" in sys.argv:
        for v in report:
            if v["issues"] or v["stray_levels"] or v["missing_cells"]:
                print(f"\n## {v['name']}")
                if v["missing_cells"]:
                    print(f"- cells not bundled: {v['missing_cells']}")
                if v["stray_levels"]:
                    print(f"- floors with 1–2 features only: {[level_label(x) for x in v['stray_levels']]}")
                for i in v["issues"]:
                    print(f"- {i}")


if __name__ == "__main__":
    main()
