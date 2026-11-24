"""python3 -m unittest discover -s Scripts -p 'test_indoor_enrich.py'"""
import copy, pathlib, sys, unittest

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
        added, suspect = en.gate_floors(self.concourse(["1", "1", None, "1", "1"]))
        self.assertEqual(added, {3: 1.0})
        self.assertEqual(suspect, [])

    def test_too_few_or_split_neighbours_say_nothing(self):
        self.assertEqual(en.gate_floors(self.concourse(["1", None, "1"]))[0], {})
        self.assertEqual(en.gate_floors(self.concourse(["1", "1", None, "2", "2"]))[0], {})

    def test_a_gate_every_neighbour_disagrees_with_is_reported_not_changed(self):
        gates = self.concourse(["1", "1", "0", "1", "1"])
        added, suspect = en.gate_floors(gates)
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


class OvertureNames(unittest.TestCase):
    def place(self, lat, lon, name="Starbucks", category="coffee_shop"):
        return ("gers-1", name, category, lat, lon)

    def test_names_an_unnamed_unit_and_keeps_its_osm_floor(self):
        unit = square(10, 40.0, -73.0, indoor="room", level="2")
        data = {(0, 0): {"elements": [unit]}}
        _, _, names = en.enrich_cells(data, "ORD", False, [self.place(40.00005, -72.99995)])
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


if __name__ == "__main__":
    unittest.main()
