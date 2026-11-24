#!/usr/bin/env python3
"""Pre-fetches OpenStreetMap indoor data for airports and major venues into the app bundle.

Writes one file per map cell, named and bounded exactly as IndoorMapService requests them, so
these venues load instantly and offline.

    python3 Scripts/fetch_indoor_venues.py                 # fill in cells that are missing
    python3 Scripts/fetch_indoor_venues.py --refresh       # rebuild every cell
    python3 Scripts/fetch_indoor_venues.py --refresh JFK SFO   # rebuild only these areas
    python3 Scripts/fetch_indoor_venues.py --refresh "Roosevelt Field"   # or venues (VENUES)
    python3 Scripts/fetch_indoor_venues.py --osm-api       # skip Overpass, read OpenStreetMap's own API
    python3 Scripts/fetch_indoor_venues.py --no-overture   # skip the Overture names (no duckdb needed)

Every area it writes is then run through `indoor_enrich.py`: gates with no floor get the one
their neighbouring gates agree on, and unnamed shop units get a name from Overture Maps Places.

Airports are fetched with one Overpass request for the whole terminal area, then cut into cells
locally: one busy-server round trip instead of twenty. Each cell keeps every element whose
bounds touch the cell widened by MARGIN, the same box the app asks Overpass for.

Only the tags the app reads are kept and coordinates are rounded to 6 decimals (about 10 cm),
which roughly halves the bundle against a raw Overpass response.
"""
import json, math, pathlib, sys, time, urllib.parse, urllib.request

CELL, MARGIN = 0.006, 0.002
OUT = pathlib.Path(__file__).resolve().parent.parent / "Waypoint/Resources/IndoorVenues"
# Public Overpass instances, tried in turn. They are volunteer-run and often busy (504, refused,
# timeouts), so each round goes through all of them before backing off.
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# Generic: no personal or project details go to third-party servers.
USER_AGENT = "indoor-map-prefetch/1.0"

VENUES = {
    "Grand Central": (40.7527, -73.9772), "Oculus": (40.7114, -74.0112), "Brookfield Place": (40.7126, -74.0152),
    "Fulton Center": (40.7102, -74.0091), "Penn Station": (40.7506, -73.9935), "Moynihan Train Hall": (40.7517, -73.9963),
    "Port Authority": (40.7570, -73.9903), "Hudson Yards": (40.7537, -74.0011),
    # Malls and stations with real floor plans in OSM. Kings Plaza, Queens Center, American Dream,
    # Garden State Plaza, Palisades, Cross County, Westchester Airport and the ferry terminals were
    # tried and have no indoor data worth shipping yet.
    "Staten Island Mall": (40.5810, -74.1660), "Roosevelt Field": (40.7395, -73.6130),
    "Newport Centre": (40.7270, -74.0355), "Jamaica Station": (40.6999, -73.8080),
    "Atlantic Terminal": (40.6843, -73.9772), "Macy's Herald Square": (40.7508, -73.9895),
    # Listed (KnownVenue.bundled) but had no bundled cell until the 2026-11-23 audit: 30 shops with floors.
    "Columbus Circle": (40.7684, -73.9827),
}

# Airports, as south, west, north, east around the terminals and their landside (garages, rental
# cars, rail stations, pick-up curbs), not the airfield. Each is fetched whole and cut into cells.
AIRPORTS = {
    "JFK": (40.636, -73.797, 40.652, -73.771),
    "LGA": (40.767, -73.889, 40.779, -73.858),
    "EWR": (40.684, -74.188, 40.700, -74.172),
    "MCO": (28.410, -81.326, 28.442, -81.294),
    "ATL": (33.634, -84.449, 33.646, -84.416),
    "LAX": (33.938, -118.414, 33.949, -118.393),
    "ORD": (41.970, -87.913, 41.987, -87.886),
    "SFO": (37.609, -122.396, 37.622, -122.380),
    "MIA": (25.790, -80.295, 25.801, -80.271),
    "DFW": (32.888, -97.048, 32.914, -97.030),
    "BOS": (42.358, -71.033, 42.372, -71.013),
    # Florida airports Mapbox has no floor plans for; OpenStreetMap maps their gates, shops and rooms.
    "FLL": (26.064, -80.153, 26.080, -80.135), "JAX": (30.487, -81.693, 30.499, -81.680),
    "PBI": (26.684, -80.096, 26.693, -80.085), "RSW": (26.520, -81.764, 26.535, -81.747),
    "SFB": (28.771, -81.249, 28.781, -81.239), "SRQ": (27.383, -82.559, 27.392, -82.548),
    "PIE": (27.899, -82.699, 27.911, -82.686), "DAB": (29.180, -81.063, 29.189, -81.053),
    # Tampa: Mapbox draws its plan; ours gives the directory its gates' terminals and floors
    # (added by the 2026-11-23 indoor audit: 6 terminal outlines, 60 gates, 230 indoor features).
    "TPA": (27.970, -82.544, 27.988, -82.526),
    # Top-30 US airports outside NY/FL with good indoor mapping (gates on floors, rooms, shops),
    # added after the 2026-11-23 audit. Boxes from their terminal outlines plus the garages.
    "DEN": (39.845, -104.684, 39.866, -104.662),
    "SEA": (47.436, -122.307, 47.451, -122.294),
    "MSP": (44.870, -93.230, 44.893, -93.193),
    "IAH": (29.982, -95.355, 29.992, -95.329),
}

# Malls where OpenStreetMap has stores tagged with their floor. A mall with a `shop=mall` outline
# draws our plan (its floors and stores); without one, these still give the directory its floors.
MALLS = {
    "Dolphin Mall": (25.7845, -80.3850, 25.7912, -80.3765), "Dadeland Mall": (25.6878, -80.3160, 25.6936, -80.3092),
    "Brickell City Centre": (25.7660, -80.1953, 25.7689, -80.1920), "International Plaza": (27.9622, -82.5238, 27.9672, -82.5177),
    "Altamonte Mall": (28.6626, -81.3823, 28.6700, -81.3737), "Aventura Mall": (25.9527, -80.1478, 25.9615, -80.1378),
    # The two largest US malls with their stores mapped on floors in OSM (2026-11-23 audit).
    "Mall of America": (44.8520, -93.2465, 44.8580, -93.2380), "King of Prussia": (40.0850, -75.3975, 40.0910, -75.3865),
}

# Tags the app reads (IndoorMapParsing); everything else is dropped from the bundle.
KEEP_TAGS = {
    "name", "ref", "level", "repeat_on", "indoor", "shop", "amenity", "aeroway", "highway", "building",
    "parking", "railway", "public_transport", "station", "type", "operator", "cuisine", "room", "access", "bus", "train",
}
# Amenities with a floor that are clutter on a floor plan, not places people look for.
NOISE_AMENITIES = {
    "waste_basket", "recycling", "bench", "clock", "vending_machine", "parking_entrance", "waste_disposal",
    "bicycle_parking", "smoking_area", "dog_toilet", "fire_extinguisher", "letter_box", "post_box",
    "telephone", "shelter", "trolley_bay", "luggage_trolley",
}


# Cells whose OSM-API fallback couldn't complete a relation: written without the complete flag.
INCOMPLETE = set()


def cell(lat, lon):
    return math.floor(lat / CELL), math.floor(lon / CELL)


def cells_in(box):
    s, w, n, e = box
    (r0, c0), (r1, c1) = cell(s, w), cell(n, e)
    return [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


def cell_box(row, col):
    """The box the app asks Overpass for: the cell widened by MARGIN on every side."""
    s, w = row * CELL - MARGIN, col * CELL - MARGIN
    return s, w, s + CELL + 2 * MARGIN, w + CELL + 2 * MARGIN


def query(box, timeout=25):
    """Keep in step with IndoorMapService.fetchFromOverpass."""
    b = "{:.6f},{:.6f},{:.6f},{:.6f}".format(*box)
    return (f"[out:json][timeout:{timeout}];("
            f"way[indoor]({b});nwr[level][shop]({b});nwr[level][amenity]({b});"
            f"nwr[repeat_on][shop]({b});nwr[repeat_on][amenity]({b});"
            f"way[highway=steps][level]({b});node[highway=elevator][level]({b});nwr[aeroway=gate]({b});"
            f"way[aeroway=terminal]({b});way[building=train_station]({b});way[building=transportation]({b});"
            f"rel[aeroway=terminal]({b});rel[building=train_station]({b});rel[building=transportation]({b});"
            f"way[shop=mall]({b});rel[shop=mall]({b});"
            f"nwr[amenity~\"^(parking|car_rental|taxi|bus_station)$\"]({b});nwr[aeroway=passenger_pickup]({b});"
            f"nwr[railway=station]({b});nwr[public_transport=station]({b});"
            f");out body geom;")


def landside(tags):
    a = tags.get("amenity")
    return (a in ("parking", "car_rental", "taxi", "bus_station") or tags.get("aeroway") == "passenger_pickup"
            or tags.get("railway") == "station" or tags.get("public_transport") == "station")


def wanted(kind, tags):
    """The same picks as `query`, for data from OpenStreetMap's own API, and the bundle filter.

    Mirrors IndoorMapParsing: outlines first (a station building stays an outline even unnamed),
    then gates, then what's outside, then what's on a floor.
    """
    floored = "level" in tags or "repeat_on" in tags
    place = (tags.get("aeroway") == "terminal" or tags.get("building") in ("train_station", "transportation")
             or tags.get("shop") == "mall")
    if place and kind != "node" and "indoor" not in tags:
        return True
    if tags.get("aeroway") == "gate":
        return True
    if tags.get("shop") == "mall" and kind == "node":
        return False
    if tags.get("amenity") in NOISE_AMENITIES and "indoor" not in tags:
        return False
    # A shop or amenity the public can't use (a tower's staff café): IndoorMapParsing.isPrivatePlace.
    if (tags.get("access") in ("private", "no", "permit") and ("shop" in tags or "amenity" in tags)
            and not landside(tags) and "indoor" not in tags):
        return False
    if landside(tags):
        if tags.get("access") in ("private", "no", "permit"):
            return False
        if tags.get("amenity") == "parking":
            # Unnamed surface lots have nothing to label; garages and named lots do.
            return "name" in tags or tags.get("parking") == "multi-storey"
        return ("name" in tags or floored or tags.get("amenity") in ("taxi", "car_rental")
                or tags.get("aeroway") == "passenger_pickup")
    if kind == "node":
        return (floored and ("shop" in tags or "amenity" in tags)) or ("level" in tags and tags.get("highway") == "elevator")
    return ("indoor" in tags
            or (floored and ("shop" in tags or "amenity" in tags))
            or (kind == "way" and "level" in tags and tags.get("highway") == "steps"))


def rounded(points):
    return [{"lat": round(p["lat"], 6), "lon": round(p["lon"], 6)} for p in points]


# Misspelt names in OpenStreetMap that would reach a label ("Gate 1 · Terminl 8"), corrected
# here until they're fixed upstream: (type, id) -> name.
NAME_FIXES = {
    ("way", 284907690): "Terminal 8",  # JFK, the west half of Terminal 8's outline
}


def compact(element):
    """Only what the app decodes: type, id, kept tags, geometry or point, outline members."""
    tags = {k: v for k, v in (element.get("tags") or {}).items() if k in KEEP_TAGS}
    fixed = NAME_FIXES.get((element["type"], element["id"]))
    if fixed and "name" in tags:
        tags["name"] = fixed
    kind = element["type"]
    if not tags or not wanted(kind, tags):
        return None
    out = {"type": kind, "id": element["id"], "tags": tags}
    if kind == "node":
        out["lat"], out["lon"] = round(element["lat"], 6), round(element["lon"], 6)
    elif kind == "way":
        if len(element.get("geometry") or []) < 2:
            return None
        out["geometry"] = rounded(element["geometry"])
    else:
        members = [{"role": m.get("role") or "", "geometry": rounded(m["geometry"])}
                   for m in element.get("members") or []
                   if m.get("type") == "way" and m.get("geometry") and None not in m["geometry"]]
        if not members:
            return None
        out["members"] = members
    return out


def points(element):
    if element["type"] == "node":
        return [element]
    if element["type"] == "way":
        return element["geometry"]
    return [p for m in element["members"] for p in m["geometry"]]


def touches(element, box):
    s, w, n, e = box
    pts = points(element)
    return not (max(p["lat"] for p in pts) < s or min(p["lat"] for p in pts) > n
                or max(p["lon"] for p in pts) < w or min(p["lon"] for p in pts) > e)


def overpass(q, timeout=180):
    body = ("data=" + urllib.parse.quote(q, safe="")).encode()
    for attempt in range(5):
        for endpoint in ENDPOINTS:
            try:
                req = urllib.request.Request(endpoint, data=body, headers={"User-Agent": USER_AGENT})
                data = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
                # Any remark means Overpass stopped early (time or memory): the answer is partial.
                if "elements" not in data or "remark" in data:
                    raise ValueError(data.get("remark", "no elements"))
                return data["elements"]
            except Exception as error:
                print(f"  {endpoint.split('/')[2]} failed: {error}", file=sys.stderr)
        pause = min(120, 10 * 2 ** attempt)
        print(f"  all servers busy; waiting {pause}s", file=sys.stderr)
        time.sleep(pause)
    return None


def fetch_osm_api(row, col):
    """The cell straight from api.openstreetmap.org, reshaped into what Overpass would have sent.

    The Overpass servers are run by volunteers and can all be down at once (they were for two
    days in November 2026). OpenStreetMap's own API always answers for a box this small. A
    terminal relation reaching outside the box is completed with /relation/<id>/full.
    """
    import xml.etree.ElementTree as ET
    s, w, n, e = cell_box(row, col)

    def get(path):
        req = urllib.request.Request("https://api.openstreetmap.org/api/0.6/" + path, headers={"User-Agent": USER_AGENT})
        return ET.fromstring(urllib.request.urlopen(req, timeout=120).read())

    try:
        roots = [get(f"map?bbox={w:.6f},{s:.6f},{e:.6f},{n:.6f}")]
    except Exception as error:
        print(f"  api.openstreetmap.org failed: {error}", file=sys.stderr)
        return None

    def tags(el): return {t.get("k"): t.get("v") for t in el.findall("tag")}

    for rel in roots[0].findall("relation"):
        if wanted("relation", tags(rel)):
            try:
                roots.append(get(f"relation/{rel.get('id')}/full"))
            except Exception as error:
                # The cell is still written, but without the "landside" (complete) flag, so the
                # app tops it up from Overpass instead of trusting it.
                INCOMPLETE.add((row, col))
                print(f"  relation {rel.get('id')} incomplete: {error}", file=sys.stderr)
    nodes, ways, elements, seen = {}, {}, [], set()
    for root in roots:
        for node in root.findall("node"):
            nodes[node.get("id")] = {"lat": float(node.get("lat")), "lon": float(node.get("lon")), "tags": tags(node)}
        for way in root.findall("way"):
            ways[way.get("id")] = (way, [r.get("ref") for r in way.findall("nd")])
    for nid, node in nodes.items():
        if node["tags"] and ("node", nid) not in seen:
            seen.add(("node", nid))
            elements.append({"type": "node", "id": int(nid), "lat": node["lat"], "lon": node["lon"], "tags": node["tags"]})

    def geometry(wid):
        refs = ways[wid][1]
        pts = [nodes[r] for r in refs if r in nodes]
        return [{"lat": p["lat"], "lon": p["lon"]} for p in pts] if len(pts) == len(refs) else None

    for wid, (way, _) in ways.items():
        t, geo = tags(way), geometry(wid)
        if t and geo and ("way", wid) not in seen:
            seen.add(("way", wid))
            elements.append({"type": "way", "id": int(wid), "geometry": geo, "tags": t})
    for root in roots:
        for rel in root.findall("relation"):
            if ("relation", rel.get("id")) in seen:
                continue
            seen.add(("relation", rel.get("id")))
            members = [{"type": "way", "role": m.get("role") or "", "geometry": geometry(m.get("ref"))}
                       for m in rel.findall("member") if m.get("type") == "way" and m.get("ref") in ways]
            members = [m for m in members if m["geometry"]]
            if members:
                elements.append({"type": "relation", "id": int(rel.get("id")), "members": members, "tags": tags(rel)})
    return elements


def write(row, col, elements, label):
    box = cell_box(row, col)
    kept = [c for c in (compact(e) for e in elements) if c and touches(c, box)]
    kept.sort(key=lambda c: ({"relation": 0, "way": 1, "node": 2}[c["type"]], c["id"]))
    # "landside": the cell answers the airport query too (parking, stations, pick-up), so the app
    # doesn't ask Overpass for it again when the cell happens to have none.
    header = {"version": 0.6, "generator": "Scripts/fetch_indoor_venues.py"}
    if (row, col) not in INCOMPLETE:
        header["landside"] = True
    data = json.dumps({**header, "elements": kept},
                      separators=(",", ":"), ensure_ascii=False).encode()
    (OUT / f"indoor_{row}_{col}.json").write_bytes(data)
    print(f"{row},{col}: {len(kept)} elements, {len(data) // 1024}KB  ({label})")


def needs(row, col, refresh):
    return refresh or not (OUT / f"indoor_{row}_{col}.json").exists()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    refresh, osm_api = "--refresh" in sys.argv, "--osm-api" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    areas = {**AIRPORTS, **MALLS}
    done, touched = set(), set()
    for name, box in areas.items():
        if args and name not in args:
            continue
        todo = [rc for rc in cells_in(box) if needs(*rc, refresh)]
        done.update(cells_in(box))
        if not todo:
            continue
        touched.add(name)
        elements = None
        if not osm_api:
            # One request for the union of every cell's box.
            boxes = [cell_box(*rc) for rc in todo]
            union = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
            print(f"{name}: {len(todo)} cells", file=sys.stderr)
            elements = overpass(query(union, timeout=170))
        for row, col in todo:
            cell_elements = elements if elements is not None else fetch_osm_api(row, col)
            if cell_elements is None:
                print(f"skip {row},{col} ({name})")
                continue
            write(row, col, cell_elements, name)
            if elements is None:
                time.sleep(1)
        time.sleep(3)
    for name, (lat, lon) in VENUES.items():
        if args and name not in args:
            continue
        row, col = cell(lat, lon)
        if (row, col) in done or not needs(row, col, refresh):
            continue
        done.add((row, col))
        touched.add(name)
        elements = None if osm_api else overpass(query(cell_box(row, col)), timeout=60)
        if elements is None:
            elements = fetch_osm_api(row, col)
        if elements is None:
            print(f"skip {row},{col} ({name})")
            continue
        write(row, col, elements, name)
        time.sleep(2)
    if touched:
        import indoor_enrich
        indoor_enrich.enrich(touched, overture="--no-overture" not in sys.argv)


if __name__ == "__main__":
    main()
