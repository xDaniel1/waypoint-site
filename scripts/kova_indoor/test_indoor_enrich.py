"""python3 -m unittest discover -s Scripts -p 'test_indoor_enrich.py'"""
import copy, json, pathlib, sys, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import indoor_enrich as en  # noqa: E402


def gate(i, lat, lon, level=None):
    tags = {"aeroway": "gate", "ref": f"G{i}"}
    if level is not None:
        tags["level"] = level
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}


def square(i, lat, lon, size=0.0001, **tags):
    g = [{"lat": lat, "lon": lon}, {"lat": lat, "lon": lon + size}, {"lat": lat + size, "lon": lon + size},
         {"lat": lat + size, "lon": lon}, {"lat": lat, "lon": lon}]
    return {"type": "way", "id": i, "geometry": g, "tags": tags}


class GateFloors(unittest.TestCase):
    def concourse(self, levels):
        # Gates 30 m apart along a line.
        return [gate(i, 40.0, -73.0 + i * 0.00035, lv) for i, lv in enumerate(levels, 1)]

    def test_unfloored_gate_takes_its_neighbours_floor(self):
        added, suspect, _ = en.gate_floors(self.concourse(["1", "1", None, "1", "1"]))
        self.assertEqual(added, {3: 1.0})
        self.assertEqual(suspect, [])

    def test_too_few_or_split_neighbours_say_nothing(self):
        self.assertEqual(en.gate_floors(self.concourse(["1", None, "1"]))[0], {})
        self.assertEqual(en.gate_floors(self.concourse(["1", "1", None, "2", "2"]))[0], {})

    def test_a_gate_every_neighbour_disagrees_with_is_reported_not_changed(self):
        gates = self.concourse(["1", "1", "0", "1", "1"])
        added, suspect, _ = en.gate_floors(gates)
        self.assertEqual(added, {})
        self.assertEqual([s[0] for s in suspect], [3])
        self.assertEqual(gates[2]["tags"]["level"], "0")

    def test_inferred_floors_do_not_vote_and_the_pass_is_idempotent(self):
        cell = {"elements": self.concourse(["1", "1", None, None, "1", "1"])}
        data = {(0, 0): cell}
        en.enrich_cells(data, "X", True, None)
        first = copy.deepcopy(cell)
        en.enrich_cells(data, "X", True, None)
        self.assertEqual(cell, first)
        self.assertEqual(cell["elements"][2]["tags"].get("level:inferred"), "gates")

    def test_a_gate_in_two_cells_gets_the_floor_in_both(self):
        gates = self.concourse(["1", "1", None, "1", "1"])
        data = {(0, 0): {"elements": copy.deepcopy(gates)}, (0, 1): {"elements": [copy.deepcopy(gates[2])]}}
        en.enrich_cells(data, "X", True, None)
        self.assertEqual(data[(0, 1)]["elements"][0]["tags"]["level"], "1")


class GateFloorsFromWays(unittest.TestCase):
    def gate(self, **tags):
        g = gate(1, 40.0, -73.0)
        g["tags"].update(tags)
        return g

    def room(self, level):
        return square(50, 40.0, -73.0, indoor="room", level=level)

    def test_indoor_ways_on_one_floor_settle_it(self):
        self.assertEqual(en.way_floors([self.gate(**{"level:ways": "2"}), self.room("2")]), {1: 2.0})
        self.assertEqual(en.way_floors([self.gate(**{"level:ways": "2", "level:jet_bridge": "2"}), self.room("2")]),
                         {1: 2.0})

    def test_a_floor_the_venue_does_not_have_is_not_taken(self):
        # DFW: a gate on a footbridge tagged level 3, in a terminal mapped on 0 and 1.
        self.assertEqual(en.way_floors([self.gate(**{"level:ways": "3"}), self.room("1")]), {})

    def test_ways_that_disagree_say_nothing(self):
        self.assertEqual(en.way_floors([self.gate(**{"level:ways": "1;2"})]), {})
        # The jet bridge a floor above the corridor (TPA, LGA): neither is trusted.
        self.assertEqual(en.way_floors([self.gate(**{"level:ways": "1", "level:jet_bridge": "2"})]), {})

    def test_own_levels_overrule_a_way_floor_and_only_own_levels_are_suspects(self):
        # ORD's L gates: the corridor says 1, every neighbouring gate's own tag says 0.
        gates = [gate(i, 40.0, -73.0 + i * 0.00035, "0") for i in range(1, 6)]
        odd = gates[2]
        odd["tags"].pop("level")
        odd["tags"]["level:ways"] = "1"
        cell = {"elements": gates + [square(50, 40.0, -73.0, indoor="room", level="1")]}
        _, suspect, _, by_ways, _ = en.enrich_cells({(0, 0): cell}, "X", True, None)
        self.assertEqual(by_ways, {})
        self.assertEqual(odd["tags"]["level"], "0")
        self.assertEqual(odd["tags"]["level:inferred"], "gates")
        self.assertEqual(suspect, [])

    def test_a_jet_bridge_alone_says_nothing(self):
        # JFK Terminal 4: the bridges are a floor above the gates, shops around them included.
        self.assertEqual(en.way_floors([self.gate(**{"level:jet_bridge": "1"})]), {})

    def test_a_gate_with_its_own_level_keeps_it(self):
        self.assertEqual(en.way_floors([self.gate(level="3", **{"level:ways": "2"})]), {})

    def test_way_floors_vote_for_neighbours_and_the_run_is_idempotent(self):
        gates = [gate(i, 40.0, -73.0 + i * 0.00035) for i in range(1, 6)]
        for g in gates[:4]:
            g["tags"]["level:ways"] = "1"
        cell = {"elements": gates + [square(50, 40.0, -73.0, indoor="room", level="1")]}
        data = {(0, 0): cell}
        _, _, _, by_ways, _ = en.enrich_cells(data, "X", True, None)
        self.assertEqual(sorted(by_ways), [1, 2, 3, 4])
        self.assertEqual(gates[4]["tags"].get("level:inferred"), "gates")
        first = copy.deepcopy(cell)
        en.enrich_cells(data, "X", True, None)
        self.assertEqual(cell, first)


class RecordGateWays(unittest.TestCase):
    def test_records_the_floors_of_the_ways_a_gate_is_on(self):
        import fetch_indoor_venues as fv
        g = gate(7, 40.0, -73.0)
        elements = [g,
                    {"type": "way", "id": 1, "nodes": [6, 7], "tags": {"highway": "corridor", "indoor": "yes", "level": "2"}},
                    {"type": "way", "id": 2, "nodes": [7, 8], "tags": {"aeroway": "jet_bridge", "highway": "footway", "level": "2"}},
                    {"type": "way", "id": 3, "nodes": [7, 9], "tags": {"indoor": "wall", "level": "1"}},
                    {"type": "way", "id": 4, "nodes": [7, 9], "tags": {"indoor": "room", "level": "0;1"}}]
        fv.record_gate_ways(elements)
        self.assertEqual(g["tags"]["level:ways"], "2")
        self.assertEqual(g["tags"]["level:jet_bridge"], "2")
        floored = gate(8, 40.0, -73.0, "3")
        fv.record_gate_ways([floored, {"type": "way", "id": 1, "nodes": [8], "tags": {"indoor": "corridor", "level": "2"}}])
        self.assertNotIn("level:ways", floored["tags"])


class OvertureNames(unittest.TestCase):
    def place(self, lat, lon, name="Starbucks", category="coffee_shop"):
        return ("gers-1", name, category, lat, lon)

    def test_names_an_unnamed_unit_and_keeps_its_osm_floor(self):
        unit = square(10, 40.0, -73.0, indoor="room", level="2")
        data = {(0, 0): {"elements": [unit]}}
        _, _, names, _, _ = en.enrich_cells(data, "ORD", False, [self.place(40.00005, -72.99995)])
        self.assertEqual(names, {10: ("Starbucks", "gers-1", "coffee_shop")})
        self.assertEqual(unit["tags"], {"indoor": "room", "level": "2", "name": "Starbucks", "name:source": "overture",
                                        "ref:overture": "gers-1", "amenity": "cafe", "overture:added": "amenity"})
        en.enrich_cells(data, "ORD", False, [])  # the place is gone from Overture: the unit is as OSM has it
        self.assertEqual(unit["tags"], {"indoor": "room", "level": "2"})

    def test_stacked_units_named_units_twins_and_crowds_are_left_alone(self):
        p = self.place(40.00005, -72.99995)
        stacked = [square(10, 40.0, -73.0, indoor="room", level="1"), square(11, 40.0, -73.0, indoor="room", level="2")]
        self.assertEqual(en.overture_names(stacked, [p], "X"), {})
        named = [square(10, 40.0, -73.0, indoor="room", level="1", name="Gap")]
        self.assertEqual(en.overture_names(named, [p], "X"), {})
        twin = [square(10, 40.0, -73.0, indoor="room", level="1"),
                {"type": "node", "id": 5, "lat": 40.0001, "lon": -73.0, "tags": {"amenity": "cafe", "name": "Starbucks", "level": "1"}}]
        self.assertEqual(en.overture_names(twin, [p], "X"), {})
        two = [square(10, 40.0, -73.0, indoor="room", level="1")]
        self.assertEqual(en.overture_names(two, [p, self.place(40.00006, -72.99994, "Sbarro", "restaurant")], "X"), {})
        private = [square(10, 40.0, -73.0, indoor="room", level="1", access="no")]
        self.assertEqual(en.overture_names(private, [p], "X"), {})

    def test_clean_name_drops_the_venue_suffix_only(self):
        self.assertEqual(en.clean_name("Gucci - LAX", "LAX"), "Gucci")
        self.assertEqual(en.clean_name("Dippin' Dots: Mall of America", "Mall of America"), "Dippin' Dots")
        self.assertEqual(en.clean_name("Burger Federation", "ORD"), "Burger Federation")
        self.assertEqual(en.clean_name("Salt & Straw - Portland", "SEA"), "Salt & Straw - Portland")




class StorePoints(unittest.TestCase):
    """Pass 4: Overture stores as points, only on a floor something real states."""

    def place(self, lat, lon, name="Zumiez", address=None, gers="g-1", category="fashion_and_apparel_store"):
        return (gers, name, category, lat, lon, address)

    def mall(self):
        """The venue's outline, around every test place."""
        return square(39, 39.99, -73.02, size=0.04, shop="mall")

    def shop(self, i, level, lat=40.01, lon=-73.01):
        return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": {"shop": "clothes", "name": f"S{i}", "level": level}}

    def test_reads_the_floor_an_address_states(self):
        self.assertIsNone(en.stated_floor("19575 Biscayne Blvd, Upper Level, #1273"))
        self.assertEqual(en.level_word("19575 Biscayne Blvd, Upper Level, #1273"), "upper")
        self.assertEqual(en.level_word("19501 Biscayne Blvd  Lowr Level"), "lower")
        self.assertEqual(en.stated_floor("20 Hudson Yards Second Floor, Suite RU215C"), 2)
        self.assertIsNone(en.level_word("Upper Level, Level 3"), "a number wins")
        self.assertIsNone(en.level_word("19501 Biscayne Blvd Suite: Lower Level #153, Upper Level #2037"))
        self.assertEqual(en.stated_floor("20 Hudson Yards Suite RU225 2nd Floor"), 2)
        self.assertEqual(en.stated_floor("20 Hudson Yards level 5"), 5)
        self.assertIsNone(en.stated_floor("20 Hudson Yards Suite 131B"))
        self.assertIsNone(en.stated_floor(None))

    def test_upper_level_picks_the_upper_of_two_stacked_units(self):
        # Aventura: a floor-0 and a floor-1 unit under the spot, numbering unsettled.
        units = [square(10, 40.0, -73.0, indoor="room", level="0"), square(11, 40.0, -73.0, indoor="room", level="1")]
        p = self.place(40.00005, -72.99995, address="19575 Biscayne Blvd, Upper Level, #1273")
        self.assertEqual([(x[1], x[5], x[6]) for x in en.store_points(units, [p], "Aventura Mall")], [("Zumiez", 1.0, "address")])
        # No floor stated, and units on two floors: nothing says which.
        self.assertEqual(en.store_points(units, [self.place(40.00005, -72.99995)], "Aventura Mall"), [])

    def test_a_unit_floor_counts_only_where_the_other_floors_are_mapped_at_the_spot(self):
        p = self.place(40.00005, -72.99995)
        shop = square(10, 40.0, -73.0, indoor="room", level="0")
        corridor = square(20, 39.9999, -73.0001, size=0.0003, indoor="corridor", level="1")
        self.assertEqual([x[5] for x in en.store_points([shop, corridor], [p], "X")], [0.0])
        # Floor 1 is mapped nearby but not over the spot: the store could be up there.
        far = square(20, 40.0002, -73.0, indoor="corridor", level="1")
        self.assertEqual(en.store_points([shop, far], [p], "X"), [])
        # Mapped on one floor only, in a mall whose outline has two storeys (Altamonte).
        mall = square(30, 39.99, -73.01, size=0.03, shop="mall", building="yes", **{"building:levels": "2"})
        self.assertEqual(en.store_points([shop, mall], [p], "X"), [])
        mall["tags"]["building:levels"] = "1"
        self.assertEqual([x[5] for x in en.store_points([shop, mall], [p], "X")], [0.0])

    def test_a_numbered_floor_needs_the_venues_numbering_settled(self):
        # Hudson Yards: floors 0-4 and no units; "2nd Floor" is floor 1 or 2 depending on numbering.
        osm = [self.shop(i, lv) for i, lv in enumerate(["0", "1", "2", "3"], 1)] + [self.mall()]
        p = self.place(40.0, -73.0, "Madewell", "20 Hudson Yards Suite RU225 2nd Floor")
        self.assertEqual(en.store_points(osm, [p], "Hudson Yards"), [])
        signs = [{"type": "node", "id": 90 + i, "lat": 40.02, "lon": -73.02,
                  "tags": {"amenity": "theatre", "indoor": "room", "level": lv, "level:ref": lv}} for i, lv in enumerate(["2", "3"])]
        self.assertEqual([x[5] for x in en.store_points(osm + signs, [p], "Hudson Yards")], [2.0])
        # A floor the venue doesn't have is not taken.
        high = self.place(40.0, -73.0, "Milos", "20 Hudson Yards 5th Floor", gers="g-2")
        self.assertEqual(en.store_points(osm + signs, [high], "Hudson Yards"), [])

    def test_twins_and_repeats_are_left_out(self):
        # A venue numbered from 1, like its signs: "Level 2" is floor 2.
        osm = [self.mall(), self.shop(1, "1"), self.shop(2, "2"),
               {"type": "node", "id": 3, "lat": 40.0001, "lon": -73.0, "tags": {"shop": "clothes", "name": "Madewell", "level": "2"}}]
        twin = self.place(40.0, -73.0, "Madewell", "Level 2")
        self.assertEqual(en.store_points(osm, [twin], "X"), [])
        a = self.place(40.0, -72.99, "PIQ", "Level 2", gers="a")
        b = self.place(40.00001, -72.99, "PIQ", "2nd floor", gers="b")
        self.assertEqual(len(en.store_points(osm, [a, b], "X")), 1)

    def test_points_go_in_every_cell_that_holds_them_once_and_strip_again(self):
        import fetch_indoor_venues as fv
        rc = fv.cell(40.0, -73.0)
        data = {rc: {"elements": [self.shop(1, "0")]}}
        point = ("g-9", "PIQ", "gift_shop", 40.0, -73.0, 1.0, "address")
        en.place_points(data, [point])
        en.place_points(data, [point])
        added = [e for e in data[rc]["elements"] if e["tags"].get("level:source")]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["tags"], {"name": "PIQ", "shop": "gift", "level": "1", "level:source": "address",
                                            "name:source": "overture", "ref:overture": "g-9"})
        self.assertLess(added[0]["id"], 0, "never an OSM id")
        en.strip_store_points(data, lambda lat, lon: False)
        self.assertEqual(len(data[rc]["elements"]), 2, "another venue's points stay")
        en.strip_store_points(data)
        self.assertEqual(data[rc]["elements"], [self.shop(1, "0")])

    def test_an_address_the_units_contradict_is_not_overruled_by_them(self):
        # A floor-0 shop under a floor-1 corridor: the unit rule alone says 0, the address says
        # otherwise, so nothing says which.
        shop = square(10, 40.0, -73.0, indoor="room", level="0")
        corridor = square(20, 39.9999, -73.0001, size=0.0003, indoor="corridor", level="1")
        for address in ("Upper Level", "Level 3"):
            self.assertEqual(en.store_points([shop, corridor], [self.place(40.00005, -72.99995, address=address)], "X"), [], address)

    def test_upper_level_alone_needs_a_two_floor_venue(self):
        # Numbered from 1 with three floors and no units: "Upper Level" could be 2 or 3.
        three = [self.shop(i, lv) for i, lv in enumerate(["1", "2", "3"], 1)] + [self.mall()]
        self.assertEqual(en.store_points(three, [self.place(40.0, -73.0, address="Upper Level")], "X"), [])
        two = [self.shop(i, lv) for i, lv in enumerate(["1", "2"], 1)] + [self.mall()]
        self.assertEqual([x[5] for x in en.store_points(two, [self.place(40.0, -73.0, address="Upper Level")], "X")], [2.0])

    def test_only_the_venues_own_elements_count(self):
        # Signs' numbers outside the venue's circle don't settle its numbering.
        osm = [self.shop(i, lv) for i, lv in enumerate(["0", "1", "2", "3"], 1)] + [self.mall()]
        signs = [{"type": "node", "id": 90 + i, "lat": 41.0, "lon": -73.02,
                  "tags": {"amenity": "theatre", "indoor": "room", "level": lv, "level:ref": lv}} for i, lv in enumerate(["2", "3"])]
        p = self.place(40.0, -73.0, "Madewell", "2nd Floor")
        near = lambda lat, lon: lat < 40.5
        self.assertEqual(en.store_points(osm + signs, [p], "X", within=near), [])
        self.assertEqual([x[5] for x in en.store_points(osm + signs, [p], "X")], [2.0], "inside the venue they would")

    def test_an_address_counts_only_inside_the_venues_building(self):
        # The Oculus's circle takes in the hotel beside it, whose restaurant says "3rd floor".
        osm = [self.shop(i, lv) for i, lv in enumerate(["1", "2", "3"], 1)]
        mall = square(30, 40.0, -73.0, size=0.001, shop="mall")
        inside = self.place(40.0005, -72.9995, "PIQ", "Level 2")
        outside = self.place(40.003, -72.9995, "Church & Dey", "3rd floor", gers="g-2")
        self.assertEqual([x[1] for x in en.store_points(osm + [mall], [inside, outside], "X")], ["PIQ"])

    def test_a_mall_outline_with_levels_is_not_a_unit(self):
        # Aventura's outline carries level=0;1;2; counted as a unit, every spot looked three floors deep.
        mall = square(30, 39.99, -73.01, size=0.03, shop="mall", building="yes", level="0;1;2")
        room = square(10, 40.0, -73.0, indoor="room", level="1")
        anchor = square(11, 40.001, -73.0, indoor="room", level="0;1", building="yes", shop="department_store")
        self.assertEqual({u["id"] for u in en.floored_units([mall, room, anchor])}, {10, 11})
        self.assertEqual(en.overture_names([mall, room], [("g", "Zumiez", "fashion_and_apparel_store", 40.00005, -72.99995)], "X"),
                         {10: ("Zumiez", "g", "fashion_and_apparel_store")})

    def test_upper_level_picks_between_two_floors_only(self):
        # Floors 1-3, units of 1 and 2 (the two lowest) over each other at the spot: "upper" is 2.
        low = [square(10, 40.0, -73.0, indoor="room", level="1"), square(11, 40.0, -73.0, indoor="room", level="2"),
               self.shop(1, "3")]
        p = self.place(40.00005, -72.99995, address="Upper Level")
        self.assertEqual([x[5] for x in en.store_points(low, [p], "X")], [2.0])
        # Units of 2 and 3 stacked: in a mall of three, "Upper" could be the middle or the top.
        high = [square(10, 40.0, -73.0, indoor="room", level="2"), square(11, 40.0, -73.0, indoor="room", level="3"),
                self.shop(1, "1")]
        for word in ("Upper Level", "Lower Level"):
            self.assertEqual(en.store_points(high, [self.place(40.00005, -72.99995, address=word)], "X"), [], word)
        # Floors 0-2 and one floor-1 unit under the spot: neither word says which floor.
        one = [square(10, 40.0, -73.0, indoor="room", level="1"), self.shop(1, "0"), self.shop(2, "2")]
        for word in ("Upper Level", "Lower Level"):
            self.assertEqual(en.store_points(one, [self.place(40.00005, -72.99995, address=word)], "X"), [], word)
        # A unit number after the word is not a floor.
        self.assertIsNone(en.stated_floor("Lower Level #2"))
        self.assertEqual(en.level_word("Lower Level #2"), "lower")
        self.assertEqual(en.stated_floor("Upper Level, Level 3"), 3)

    def test_only_the_venues_own_twins_vote_on_its_numbering(self):
        # Two stores named like the mall's places stand in the tower next door, on the floor
        # their "Level N" names: they would settle the mall's numbering at offset 0.
        osm = [self.mall(), self.shop(1, "0"), self.shop(2, "1"), self.shop(3, "2")]
        tower = [{"type": "node", "id": 50 + i, "lat": 40.0001, "lon": -73.0 + i * 0.001,
                  "tags": {"shop": "clothes", "name": n, "level": "2"}} for i, n in enumerate(["Ann", "Bee"])]
        # Overture's listings stand just inside the mall, 11 m from the tower's stores.
        twins = [self.place(40.0, -73.0 + i * 0.001, n, "Level 2", gers=f"t{i}") for i, n in enumerate(["Ann", "Bee"])]
        p = self.place(40.0, -72.99, "Madewell", "2nd Floor", gers="m")
        inside = lambda lat, lon: not 40.00008 < lat < 40.00012     # everything but the tower
        self.assertEqual(en.store_points(osm + tower, twins + [p], "X", within=inside), [])
        self.assertEqual([x[5] for x in en.store_points(osm + tower, twins + [p], "X")], [2.0], "inside the venue they would")

    def test_upper_level_in_a_taller_venue_needs_the_floors_above_mapped_at_the_spot(self):
        # Aventura-like: floors 0-2, a floor-0 and a floor-1 unit stacked at the spot.
        stack = [square(10, 40.0, -73.0, indoor="room", level="0"), square(11, 40.0, -73.0, indoor="room", level="1")]
        p = self.place(40.00005, -72.99995, address="Upper Level")
        floor2_far = self.shop(1, "2", lat=40.01)                   # floor 2 exists, far from the spot
        self.assertEqual([x[5] for x in en.store_points(stack + [floor2_far], [p], "X")], [1.0])
        # Floor 2 mapped beside the spot but not over it: the store could be up there.
        beside = square(20, 40.0002, -73.0, indoor="corridor", level="2")
        self.assertEqual(en.store_points(stack + [floor2_far, beside], [p], "X"), [])
        # ...and mapped over it as a corridor: it isn't.
        over = square(21, 39.9999, -73.0001, size=0.0003, indoor="corridor", level="2")
        self.assertEqual([x[5] for x in en.store_points(stack + [floor2_far, beside, over], [p], "X")], [1.0])
        # A floor-2 shop mapped as a point 5 m away, or a long floor-2 corridor whose edge passes
        # 5 m away with every corner far off: floor 2 is here.
        node2 = {"type": "node", "id": 7, "lat": 40.0001, "lon": -72.99995, "tags": {"shop": "gift", "name": "Up", "level": "2"}}
        self.assertEqual(en.store_points(stack + [floor2_far, node2], [p], "X"), [])
        long2 = {"type": "way", "id": 22, "tags": {"indoor": "corridor", "level": "2"},
                 "geometry": [{"lat": 40.0001, "lon": -73.002}, {"lat": 40.0001, "lon": -72.998}, {"lat": 40.0002, "lon": -72.998},
                              {"lat": 40.0002, "lon": -73.002}, {"lat": 40.0001, "lon": -73.002}]}
        self.assertEqual(en.store_points(stack + [floor2_far, long2], [p], "X"), [])
        # A building OSM gives three storeys has a floor the mapping lacks.
        tall = square(30, 39.99, -73.01, size=0.03, shop="mall", building="yes", **{"building:levels": "3"})
        self.assertEqual(en.store_points(stack + [floor2_far, tall], [p], "X"), [])

    def test_one_label_per_name_and_floor_whatever_order_overture_lists_them(self):
        osm = [self.mall(), self.shop(1, "1"), self.shop(2, "2")]
        a = self.place(40.0, -73.0, "Sunglass Hut", "Level 2", gers="b")
        b = self.place(40.0, -72.998, "Sunglass Hut", "Level 2", gers="a")    # ~170 m away
        first = en.store_points(osm, [a, b], "X")
        self.assertEqual([x[0] for x in first], ["a"], "one point, the lowest GERS id")
        self.assertEqual(en.store_points(osm, [b, a], "X"), first)

    def test_a_store_already_named_on_that_floor_anywhere_in_the_venue_is_not_repeated(self):
        osm = [self.mall(), self.shop(1, "1"),
               {"type": "node", "id": 3, "lat": 40.0015, "lon": -73.0, "tags": {"shop": "clothes", "name": "Madewell", "level": "2"}}]
        far = self.place(40.0, -73.0, "Madewell", "Level 2")       # ~170 m from OSM's, same floor
        self.assertEqual(en.store_points(osm, [far], "X"), [])
        other = self.place(40.0, -73.0, "Madewell", "Level 1")     # another floor, far away: a second store
        self.assertEqual([x[5] for x in en.store_points(osm, [other], "X")], [1.0])

    def test_a_twin_just_outside_the_venue_still_counts(self):
        osm = [self.mall(), self.shop(1, "1"), self.shop(2, "2"),
               {"type": "node", "id": 3, "lat": 40.0001, "lon": -73.0, "tags": {"shop": "clothes", "name": "PIQ", "level": "1"}}]
        p = self.place(40.0, -73.0, "PIQ", "Level 2")
        self.assertEqual(en.store_points(osm, [p], "X", within=lambda lat, lon: lat < 40.00005), [])


class EnrichRuns(unittest.TestCase):
    """`enrich` over overlapping venues, with the cells and Overture stubbed."""

    def setUp(self):
        import fetch_indoor_venues as fv
        self.rc = fv.cell(40.0, -73.0)
        mall = square(39, 39.99, -73.02, size=0.04, shop="mall")
        shops = [{"type": "node", "id": i, "lat": 40.01, "lon": -73.01, "tags": {"shop": "clothes", "name": f"S{i}", "level": lv}}
                 for i, lv in ((1, "1"), (2, "2"))]
        self.fresh = json.dumps({"elements": [mall] + shops})
        self.disk = {self.rc: self.fresh}
        everywhere = lambda lat, lon: True
        self.areas = {"A": ([self.rc], (39.9, -73.1, 40.1, -72.9), everywhere, False),
                      "B": ([self.rc], (39.8, -73.2, 40.2, -72.8), everywhere, False)}
        self.places = {"A": [("g1", "PIQ", "gift_shop", 40.0, -73.0, "Level 2"), ("g2", "Lush", "gift_shop", 40.0, -72.999, "Level 1")],
                       "B": [("g1", "PIQ", "gift_shop", 40.0, -73.0, "Level 2"), ("g2", "Lush", "gift_shop", 40.0, -72.999, "Level 2")]}
        self.patches = [(en, "areas", lambda: self.areas),
                        (en, "load", lambda cells: {rc: json.loads(self.disk[rc]) for rc in cells}),
                        (en, "save", self.save),
                        (en, "overture_places", lambda box, path=None: self.places["A" if box[0] == 39.9 else "B"])]
        self.saved = [getattr(m, n) for m, n, _ in self.patches]
        for m, n, f in self.patches:
            setattr(m, n, f)

    def tearDown(self):
        for (m, n, _), f in zip(self.patches, self.saved):
            setattr(m, n, f)

    def save(self, rc, d):
        self.disk[rc] = json.dumps(d)
        return True

    def run_enrich(self, names):
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            en.enrich(names, overture_path="stub")
        return json.loads(self.disk[self.rc])

    def test_a_floor_one_venue_reads_but_leaves_out_still_disputes_anothers(self):
        # B reads Lush on floor 1 but leaves it out (OSM names Lush on 1 elsewhere in B); A puts
        # it on 2. Nothing says which: no point.
        lush = {"type": "node", "id": 9, "lat": 40.02, "lon": -73.0, "tags": {"shop": "gift", "name": "Lush", "level": "1"}}
        d = json.loads(self.fresh)
        d["elements"].append(lush)
        self.fresh = json.dumps(d)
        self.disk[self.rc] = self.fresh
        self.areas["A"] = (self.areas["A"][0], self.areas["A"][1], lambda lat, lon: lat < 40.015, False)
        self.places["A"] = [("g2", "Lush", "gift_shop", 40.0, -72.999, "Level 2")]
        self.places["B"] = [("g2", "Lush", "gift_shop", 40.0, -72.999, "Level 1")]
        cell = self.run_enrich(None)
        self.assertEqual([e for e in cell["elements"] if e["tags"].get("level:source")], [])

    def test_a_subset_run_writes_what_a_full_run_does_and_floors_in_dispute_are_dropped(self):
        full = self.run_enrich(None)
        points = [e for e in full["elements"] if e["tags"].get("level:source")]
        # PIQ: both venues say floor 2. Lush: one says 1, the other 2, so it's left out.
        self.assertEqual([(e["tags"]["name"], e["tags"]["level"]) for e in points], [("PIQ", "2")])
        self.assertEqual(self.run_enrich(None), full, "a second run changes nothing")
        self.disk[self.rc] = self.fresh
        self.assertEqual(self.run_enrich(["B"]), full, "a subset run from scratch writes what a full run does")


if __name__ == "__main__":
    unittest.main()
