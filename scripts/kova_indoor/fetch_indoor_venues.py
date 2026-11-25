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
Then `indoor_prune.py` runs over every venue: a floor with no named place on it (only stairs,
lifts or unnamed rooms) is taken off the data, so it gets no floor button.

Airports are fetched with one Overpass request for the whole terminal area, then cut into cells
locally: one busy-server round trip instead of twenty. Each cell keeps every element whose
bounds touch the cell widened by MARGIN, the same box the app asks Overpass for.

Only the tags the app reads are kept and coordinates are rounded to 6 decimals (about 10 cm),
which roughly halves the bundle against a raw Overpass response.
"""
import collections, json, math, pathlib, sys, time, urllib.error, urllib.parse, urllib.request

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
    # The rest of the top US airports whose terminals OpenStreetMap maps inside (probed 2026-11-23:
    # floored gates, shops or indoor ways). Boxes: every passenger terminal and concourse outline
    # (not cargo, FBOs or hangars) plus ~280 m for the curbs and garages.
    "SAN": (32.728, -117.209, 32.737, -117.187),
    "IAD": (38.94, -77.458, 38.956, -77.437),
    "LAS": (36.077, -115.158, 36.09, -115.13),
    "SLC": (40.782, -111.996, 40.793, -111.976),
    "DCA": (38.844, -77.048, 38.861, -77.038),
    "HNL": (21.327, -157.929, 21.339, -157.908),
    "DTW": (42.2, -83.367, 42.232, -83.342),
    "BWI": (39.174, -76.677, 39.186, -76.659),
    "SJC": (37.359, -121.935, 37.374, -121.918),
    "SNA": (33.672, -117.868, 33.685, -117.857),
    "SMF": (38.689, -121.598, 38.698, -121.583),
    "PHX": (33.431, -112.015, 33.441, -111.991),
    "AUS": (30.199, -97.674, 30.205, -97.66),
    "BNA": (36.123, -86.676, 36.138, -86.663),
    "PDX": (45.585, -122.603, 45.594, -122.584),
    "RDU": (35.871, -78.8, 35.882, -78.786),
    "MCI": (39.294, -94.727, 39.303, -94.715),
    "CLE": (41.402, -81.846, 41.416, -81.833),
    # The remaining large and medium US hubs (and three small ones) whose terminals OSM maps
    # inside, probed 2026-11-24 (branch indoor/more-venues). Boxes: every passenger terminal and
    # concourse outline and every gate (not cargo, FBOs or hangars), plus ~280 m.
    "PHL": (39.87, -75.256, 39.885, -75.232), "CLT": (35.215, -80.952, 35.228, -80.936), "MDW": (41.781, -87.749, 41.792, -87.737),
    "STL": (38.734, -90.372, 38.748, -90.35), "PIT": (40.491, -80.252, 40.501, -80.238), "IND": (39.711, -86.303, 39.721, -86.29),
    "SJU": (18.433, -66.011, 18.443, -65.998), "CMH": (39.994, -82.891, 40.002, -82.877), "HOU": (29.65, -95.283, 29.658, -95.272),
    "SDF": (38.18, -85.746, 38.189, -85.735), "ABQ": (35.034, -106.622, 35.052, -106.611), "BOI": (43.565, -116.227, 43.572, -116.215),
    "TUS": (32.116, -110.943, 32.126, -110.932), "TUL": (36.187, -95.894, 36.195, -95.884), "DAL": (32.841, -96.855, 32.849, -96.845),
    "BHM": (33.558, -86.758, 33.566, -86.747), "MHT": (42.924, -71.444, 42.931, -71.433), "OAK": (37.706, -122.22, 37.716, -122.207),
    "SAT": (29.522, -98.477, 29.532, -98.467), "MSY": (29.991, -90.263, 29.999, -90.251), "MKE": (42.943, -87.908, 42.954, -87.895),
    "CVG": (39.047, -84.662, 39.058, -84.649), "ONT": (34.057, -117.608, 34.064, -117.582), "BDL": (41.927, -72.694, 41.936, -72.678),
}

# Malls where OpenStreetMap has stores tagged with their floor. A mall with a `shop=mall` outline
# draws our plan (its floors and stores); without one, these still give the directory its floors.
MALLS = {
    "Dolphin Mall": (25.7845, -80.3850, 25.7912, -80.3765), "Dadeland Mall": (25.6878, -80.3160, 25.6936, -80.3092),
    "Brickell City Centre": (25.7660, -80.1953, 25.7689, -80.1920), "International Plaza": (27.9622, -82.5238, 27.9672, -82.5177),
    "Altamonte Mall": (28.6626, -81.3823, 28.6700, -81.3737), "Aventura Mall": (25.9527, -80.1478, 25.9615, -80.1378),
    # The two largest US malls with their stores mapped on floors in OSM (2026-11-23 audit).
    "Mall of America": (44.8520, -93.2465, 44.8580, -93.2380), "King of Prussia": (40.0850, -75.3975, 40.0910, -75.3865),
    # More large US malls with 40+ stores mapped on their floors and a `shop=mall` outline (probed
    # 2026-11-23). Boxes: the outline plus ~150 m.
    "South Coast Plaza": (33.6862, -117.896, 33.696, -117.8839),
    "Tysons Corner Center": (38.914, -77.2264, 38.9202, -77.2162),
    "Del Amo Fashion Center": (33.8262, -118.353, 33.836, -118.3453),
    "Ala Moana Center": (21.2878, -157.8492, 21.2952, -157.8384),
    "Fashion Show": (36.1243, -115.1768, 36.1302, -115.1663),
    "NorthPark Center": (32.8652, -96.7774, 32.8718, -96.7695),
    "Westfield Valley Fair": (37.3226, -121.9505, 37.328, -121.9403),
    "Westfield Century City": (34.0552, -118.4229, 34.0624, -118.4151),
    "Prudential Center": (42.3442, -71.0852, 42.35, -71.0742),
    "Fashion Centre at Pentagon City": (38.8601, -77.0636, 38.8667, -77.0574),
    "Somerset Collection": (42.5577, -83.1884, 42.565, -83.1798),
    "Bellevue Square": (47.6125, -122.2075, 47.6188, -122.1997),
    # The largest and busiest US malls with their stores mapped on floors in OSM (20+ named stores
    # with a floor inside a `shop=mall` outline), probed 2026-11-24 (branch indoor/more-venues).
    # The last seven are regional malls with 90+ such stores in OSM (picked by size, not from any
    # other map's data).
    # Boxes: the outline plus ~150 m.
    "Westfield Southcenter": (47.4562, -122.2633, 47.4616, -122.2536),
    "Gurnee Mills": (42.3843, -87.9638, 42.3942, -87.9514),
    "The Shops at Santa Anita": (34.1317, -118.0546, 34.1382, -118.0465),
    "Easton Town Center": (40.0468, -82.9205, 40.0589, -82.9014),
    "Westfield Galleria at Roseville": (38.7702, -121.2733, 38.7776, -121.2658),
    "Fashion Outlets of Chicago": (41.9725, -87.8703, 41.9778, -87.8638),
    "Twelve Oaks Mall": (42.4888, -83.4745, 42.4953, -83.4648),
    "The Mall at Green Hills": (36.1049, -86.8208, 36.1093, -86.812),
    "Chandler Fashion Center": (33.2985, -111.903, 33.3049, -111.8944),
    "Perimeter Mall": (33.92, -84.3451, 33.9269, -84.3375),
    "Memorial City Mall": (29.7782, -95.5447, 29.7843, -95.5351),
    "Mayfair": (43.0604, -88.0477, 43.0673, -88.041),
    "Jordan Creek Town Center": (41.5671, -93.8085, 41.5721, -93.8007),
    "Washington Square": (45.447, -122.7838, 45.453, -122.7772),
    "Galleria Dallas": (32.927, -96.8219, 32.9334, -96.8173),
    "Montclair Place": (34.0841, -117.6981, 34.0895, -117.6893),
    "Clackamas Town Center": (45.432, -122.5809, 45.4396, -122.5658),
    "Rosedale Center": (45.0103, -93.1767, 45.0157, -93.1676),
    "Eastridge": (37.3216, -121.8178, 37.3287, -121.81),
    "Springfield Town Center": (38.7719, -77.1776, 38.7772, -77.1713),
    "The Mall in Columbia": (39.2131, -76.8641, 39.2185, -76.8576),
    "Short Pump Town Center": (37.6532, -77.6246, 37.659, -77.6142),
    "City Creek Center": (40.766, -111.8956, 40.7706, -111.8866),
    "Arden Fair": (38.5985, -121.432, 38.6046, -121.4229),
    "Ross Park Mall": (40.5393, -80.0113, 40.5469, -80.0037),
    "Stonestown Galleria": (37.7257, -122.4797, 37.7303, -122.4747),
    "Hillsdale Shopping Center": (37.5347, -122.3033, 37.5398, -122.2974),
    "Danbury Fair": (41.3746, -73.4878, 41.3842, -73.4733),
    "Fashion Island": (33.6114, -117.8801, 33.6205, -117.8709),
    "Westfield Plaza Bonita": (32.6524, -117.0686, 32.6598, -117.063),
    "San Francisco Ferry Building": (37.7933, -122.3961, 37.7978, -122.3908),
    "Westfield Oakridge": (37.2497, -121.8677, 37.2553, -121.858),
    "Westfield Culver City": (33.9825, -118.3971, 33.9887, -118.3893),
    "Crabtree Valley Mall": (35.8372, -78.6842, 35.8437, -78.6758),
    "Willow Grove Park": (40.1374, -75.1275, 40.143, -75.1194),
    "Scottsdale Fashion Square": (33.4988, -111.9363, 33.5073, -111.9246),
    "CambridgeSide": (42.3657, -71.0795, 42.3706, -71.073),
    "Beverly Center": (34.0722, -118.3809, 34.0777, -118.375),
    "International Market Place": (21.276, -157.8287, 21.2807, -157.8236),
    "Santa Monica Place": (34.0114, -118.4969, 34.0159, -118.4906),
    "The Forum Shops at Caesars": (36.1167, -115.1805, 36.1212, -115.1711),
    "Providence Place": (41.8245, -71.4196, 41.8306, -71.4135),
    "Walden Galleria": (42.9102, -78.7658, 42.9174, -78.7583),
    "The Shops at Crystals": (36.1055, -115.1774, 36.11, -115.1716),
    "CoolSprings Galleria": (35.9536, -86.8175, 35.961, -86.8105),
    "Flatiron Crossing": (39.9306, -105.1378, 39.9357, -105.1298),
    "Royal Hawaiian Center": (21.2757, -157.8318, 21.2812, -157.826),
    "Coronado Center": (35.1045, -106.5775, 35.1098, -106.5682),
    "Woodland Hills Mall": (36.0596, -95.8879, 36.067, -95.8758),
    "Park City Center": (40.0637, -76.3439, 40.0706, -76.3341),
    "The Mall at Fairfield Commons": (39.7639, -84.0602, 39.7707, -84.053),
    "Boise Towne Square": (43.6057, -116.2825, 43.6117, -116.2737),
    "Pheasant Lane Mall": (42.6986, -71.4397, 42.704, -71.4342),
    "Dayton Mall": (39.631, -84.226, 39.637, -84.2151),
}

# Stations outside New York with their halls mapped inside (rooms, shops and services on floors).
# Boxes: the station building plus ~150 m.
STATIONS = {
    "Denver Union Station": (39.7512, -105.003, 39.7553, -104.9975),
    "Washington Union Station": (38.8953, -77.0093, 38.8999, -77.003),
    "Los Angeles Union Station": (34.0538, -118.2386, 34.0584, -118.2331),
    "Ogilvie Transportation Center": (41.8805, -87.6431, 41.8845, -87.6379),
    # Added 2026-11-24 (indoor/more-venues): stations whose outline holds shops or services on floors.
    "Salesforce Transit Center": (37.7863, -122.4004, 37.7919, -122.3933),
    "Chicago Union Station": (41.877, -87.6416, 41.8804, -87.6368),
    "South Station": (42.3479, -71.0585, 42.3536, -71.0525),
    "North Station": (42.3643, -71.0648, 42.3683, -71.0594),
    "Back Bay Station": (42.3458, -71.0777, 42.349, -71.0729),
}

# Stadiums and arenas whose concourses OSM maps inside (named stands and eateries on floors),
# probed 2026-11-24 (indoor/more-mapping). Boxes: the `leisure=stadium` outline plus ~70 m.
# Kind `other` (KnownVenue). Left out: Madison Square Garden, Barclays Center and TD Garden,
# built over Penn Station, Atlantic Terminal and North Station (their circles would take the
# stations' halls), and stadiums whose bundle keeps no named place on a floor (Allegiant,
# Lumen Field, U.S. Bank Stadium, Ball Arena, Climate Pledge Arena, SoFi, AT&T, MetLife).
HALLS = {
    "Capital One Arena": (38.8969, -77.0226, 38.8993, -77.0193),
    "Fenway Park": (42.3449, -71.0996, 42.3478, -71.0947),
}

# Tags the app reads (IndoorMapParsing); everything else is dropped from the bundle.
KEEP_TAGS = {
    "name", "ref", "level", "level:ref", "building:levels", "repeat_on", "indoor", "shop", "amenity", "aeroway", "highway", "building",
    "parking", "railway", "public_transport", "station", "type", "leisure", "operator", "cuisine", "room", "access", "bus", "train",
    # `level:ref` is the floor as the building's signs number it, and `building:levels` how many
    # storeys a mall's outline has: indoor_enrich.py's pass 4 reads the venue's floor numbering
    # and whether a mall mapped on one floor really has one.
    # Recorded by record_gate_ways (the floors of the ways a gate stands on), for indoor_enrich.py.
    "level:ways", "level:jet_bridge",
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
    """Keep in step with IndoorMapService.fetchFromOverpass (the gate ways' clause is the bundle's own)."""
    b = "{:.6f},{:.6f},{:.6f},{:.6f}".format(*box)
    return (f"[out:json][timeout:{timeout}];("
            f"way[indoor]({b});nwr[level][shop]({b});nwr[level][amenity]({b});"
            f"nwr[repeat_on][shop]({b});nwr[repeat_on][amenity]({b});"
            f"way[highway=steps][level]({b});node[highway=elevator][level]({b});nwr[aeroway=gate]({b});"
            # Bundle only: the floored ways each gate node stands on (`gate_way_floors`).
            f"node[aeroway=gate]({b});way(bn)[level];"
            f"way[aeroway=terminal]({b});way[building=train_station]({b});way[building=transportation]({b});"
            f"rel[aeroway=terminal]({b});rel[building=train_station]({b});rel[building=transportation]({b});"
            f"way[shop=mall]({b});rel[shop=mall]({b});"
            f"way[leisure=stadium]({b});way[building=stadium]({b});rel[leisure=stadium]({b});"
            f"way[amenity~\"^(conference_centre|exhibition_centre)$\"]({b});"
            f"nwr[amenity~\"^(parking|car_rental|taxi|bus_station)$\"]({b});nwr[aeroway=passenger_pickup]({b});"
            f"nwr[railway=station]({b});nwr[public_transport=station]({b});"
            f");out body geom;")


def is_hall(tags):
    """A stadium, arena or convention centre outline: a venue the app treats like a mall
    (IndoorMapParsing.isHall), its concourses' stands and eateries its tenants."""
    return (tags.get("leisure") == "stadium" or tags.get("building") == "stadium"
            or tags.get("amenity") in ("conference_centre", "exhibition_centre"))


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
             or tags.get("shop") == "mall" or is_hall(tags))
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
        # The API answers 429 when a client asks too fast, and 509/503 when it is busy: wait as
        # long as it says (Retry-After) or back off, rather than write a cell with a part missing.
        req = urllib.request.Request("https://api.openstreetmap.org/api/0.6/" + path, headers={"User-Agent": USER_AGENT})
        for attempt in range(6):
            try:
                return ET.fromstring(urllib.request.urlopen(req, timeout=120).read())
            except urllib.error.HTTPError as error:
                if error.code not in (429, 503, 509) or attempt == 5:
                    raise
                wait = error.headers.get("Retry-After")
                pause = min(300, float(wait) if wait and wait.isdigit() else 15 * 2 ** attempt)
                print(f"  api.openstreetmap.org {error.code}; waiting {pause:.0f}s", file=sys.stderr)
                time.sleep(pause)

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
            elements.append({"type": "way", "id": int(wid), "geometry": geo, "tags": t,
                             "nodes": [int(r) for r in ways[wid][1]]})
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


# Ways that say which floor a gate node on them is on: an indoor corridor, footway, room or area,
# or the jet bridge leading off the gate. Walls and building outlines don't.
def gate_way_kind(tags):
    if "level" not in tags or tags.get("indoor") == "wall":
        return None
    if tags.get("aeroway") == "jet_bridge":
        return "jet_bridge"
    if (tags.get("highway") == "corridor" or (tags.get("highway") == "footway" and "indoor" in tags)
            or tags.get("indoor") in ("corridor", "area", "room", "yes")):
        return "ways"
    return None


def record_gate_ways(elements):
    """Tags each gate with no floor with the floors of the ways it is a vertex of.

    A gate node is often a vertex of the indoor corridor or footway it opens onto, or of its jet
    bridge (MCO, MIA, SEA, SFO and BOS map them this way), and those ways carry a floor. Only the
    facts are recorded here, as `level:ways` (indoor ways) and `level:jet_bridge`, each a `;` list
    of single-floor ways' floors; `indoor_enrich.py` decides whether they settle the gate's floor.
    Done here because the cells keep no way's node list. A way on two floors says nothing.
    """
    import indoor_inventory as inv
    gates = {e["id"]: e for e in elements if e["type"] == "node" and (e.get("tags") or {}).get("aeroway") == "gate"
             and not inv.floors(e.get("tags") or {})}
    found = collections.defaultdict(lambda: collections.defaultdict(set))
    for e in elements:
        kind = e["type"] == "way" and gate_way_kind(e.get("tags") or {})
        if not kind:
            continue
        f = inv.floors(e["tags"])
        if len(f) != 1:
            continue
        for ref in set(e.get("nodes") or []):
            if ref in gates:
                found[ref][kind].add(f[0])
    for gid, kinds in found.items():
        tags = dict(gates[gid]["tags"])
        for kind, fl in kinds.items():
            tags["level:" + kind] = ";".join(level_text(f) for f in sorted(fl))
        gates[gid]["tags"] = tags


def level_text(value):
    return str(int(value)) if value == int(value) else str(value)


def write(row, col, elements, label):
    box = cell_box(row, col)
    record_gate_ways(elements)
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
    areas = {**AIRPORTS, **MALLS, **STATIONS, **HALLS}
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
                time.sleep(2)
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
        # After enriching (an Overture name can make a floor named), over every venue: a rebuilt
        # cell can hold another venue's elements.
        import indoor_prune
        indoor_prune.prune()


if __name__ == "__main__":
    main()
