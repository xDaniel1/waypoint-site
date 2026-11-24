"""python3 -m unittest discover -s Scripts -p 'test_indoor_prune.py'"""
import copy, pathlib, sys, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import indoor_prune as pr  # noqa: E402

LAT, LON = 40.0, -73.0


def node(i, lat=LAT, lon=LON, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}


def square(i, lat=LAT, lon=LON, size=0.0001, **tags):
    g = [{"lat": lat, "lon": lon}, {"lat": lat, "lon": lon + size}, {"lat": lat + size, "lon": lon + size},
         {"lat": lat + size, "lon": lon}, {"lat": lat, "lon": lon}]
    return {"type": "way", "id": i, "geometry": g, "tags": tags}


def area(s, w, n, e, cells=((0, 0),)):
    """An inventory venue area: (kind, inside, cells, box)."""
    return ("airport", lambda la, lo: s <= la <= n and w <= lo <= e, list(cells), (s, w, n, e))


EVERYWHERE = area(-90, -180, 90, 180)


def found(problems):
    """{"area" or "area/outline": [floors]} from `prune_cells`' problems."""
    return {(a if o is None else f"{a}/{o}"): g for a, o, g in problems}


def run(elements, areas=None):
    cells = {(0, 0): {"elements": elements}}
    problems, dropped = pr.prune_cells(cells, areas or {"V": EVERYWHERE})
    return cells[(0, 0)]["elements"], found(problems), dropped


def terminal(i, lat, lon, size=0.002, **tags):
    return square(i, lat, lon, size=size, **({"aeroway": "terminal", "name": f"T{i}"} | tags))


def filler(start, lat, lon, level, n=8):
    """n unnamed rooms on a floor: enough inside an outline to make it a venue."""
    return [square(start + k, lat + k * 0.0001, lon, size=0.00005, indoor="room", level=level) for k in range(n)]


def by_id(elements):
    return {e["id"]: e for e in elements}


class NamedPlace(unittest.TestCase):
    def test_what_counts(self):
        yes = [
            node(1, aeroway="gate", level="1"),  # no name or ref: the app still lists it as a gate
            node(2, amenity="toilets", level="1"),  # the app calls it "Restroom"
            node(3, shop="clothes", name="Gap", level="1"),
            node(4, amenity="parking_section", ref="E", level="1"),  # labelled by its ref
            square(5, indoor="room", name="Delta Sky Club", level="1"),
            square(6, indoor="corridor", name="Silver Line Busway", level="-2"),
        ]
        no = [
            node(10, shop="electronics", level="0"),  # unnamed
            square(11, indoor="room", level="0"),
            square(12, indoor="area", name="Escalator", level="0-1"),  # a stair, by name
            square(13, indoor="room", name="Emergency Stairs West", level="0"),
            square(14, indoor="room", name="Cafe", level="0;1;2;3;4"),  # a shaft: five floors
            node(15, highway="elevator", name="Lift", level="0;1"),
            node(16, shop="clothes", name="Gap"),  # no floor
            square(17, indoor="wall", name="Wall", level="0"),
        ]
        for el in yes:
            self.assertTrue(pr.named_place(el), el)
        for el in no:
            self.assertFalse(pr.named_place(el), el)

    def test_a_stair_word_inside_a_real_name_is_a_name(self):
        self.assertTrue(pr.named_place(square(1, indoor="room", name="Stairway Coffee", level="1")))


class Prune(unittest.TestCase):
    def test_a_floor_with_only_unnamed_rooms_goes(self):
        els, report, dropped = run([
            node(1, shop="books", name="Hudson", level="1"),
            square(2, indoor="room", level="1"),
            square(3, indoor="room", level="0"),
            square(4, indoor="corridor", level="0;1"),
            square(5, indoor="wall", level="0"),
        ])
        self.assertEqual(report["V"], [0.0])
        self.assertEqual(dropped, 2)
        els = by_id(els)
        self.assertEqual(set(els), {1, 2, 4})
        self.assertEqual(els[4]["tags"]["level"], "1")
        self.assertEqual(els[4]["tags"]["level:pruned"], "0")
        self.assertEqual(els[2]["tags"], {"indoor": "room", "level": "1"})

    def test_repeat_on_and_ranges_are_rewritten(self):
        els, _, _ = run([
            node(1, shop="books", name="Hudson", level="-1"),
            square(2, indoor="room", level="-1--2"),
            square(3, indoor="room", level="-1", repeat_on="-2;-3"),
        ])
        els = by_id(els)
        self.assertEqual(els[2]["tags"]["level"], "-1")
        self.assertEqual(els[2]["tags"]["level:pruned"], "-2")
        self.assertNotIn("repeat_on", els[3]["tags"])
        self.assertEqual(els[3]["tags"]["level:pruned"], "-3;-2")

    def test_gates_keep_their_floor_even_unnamed(self):
        els, report, _ = run([
            node(1, aeroway="gate", level="2"),
            node(2, shop="books", name="Hudson", level="1"),
            square(3, indoor="room", level="2"),
        ])
        self.assertNotIn("V", report)
        self.assertEqual(set(by_id(els)), {1, 2, 3})

    def test_stairs_and_shafts_stay_whole_while_the_venue_keeps_a_floor(self):
        stairs = square(2, highway="steps", level="0;1")
        shaft = square(3, indoor="room", level="0;1;2;3;4;5")
        lift = node(4, highway="elevator", level="0;1")
        els, report, _ = run([node(1, shop="books", name="Hudson", level="1"), stairs, shaft, lift])
        self.assertNotIn("V", report)  # stairs and shafts list no floor
        els = by_id(els)
        self.assertEqual(els[2]["tags"], {"highway": "steps", "level": "0;1"})
        self.assertEqual(els[3]["tags"]["level"], "0;1;2;3;4;5")

    def test_a_venue_with_nothing_named_keeps_no_floor(self):
        # CVG, OAK, SAT: gates with no floor, stairs, lifts and unnamed rooms.
        els, report, dropped = run([
            node(1, aeroway="gate", ref="B1"),
            square(2, highway="steps", level="0;1"),
            node(3, highway="elevator", level="0;1;2"),
            square(4, indoor="room", level="2"),
            square(5, aeroway="terminal", name="Terminal"),
        ])
        # Only the room's floor is listed; the stairs' and lift's would be once it went.
        self.assertEqual(report["V"], [2.0])
        self.assertEqual(dropped, 3)
        self.assertEqual(set(by_id(els)), {1, 5})
        self.assertEqual(pr.listed_floors(els), [])

    def test_every_copy_of_an_element_is_pruned_alike(self):
        room = square(2, indoor="room", level="0;1")
        cells = {(0, 0): {"elements": [node(1, shop="books", name="Hudson", level="1"), room]},
                 (0, 1): {"elements": [copy.deepcopy(room)]}}
        pr.prune_cells(cells, {"V": area(-90, -180, 90, 180, cells=((0, 0), (0, 1)))})
        self.assertEqual(cells[(0, 0)]["elements"][1], cells[(0, 1)]["elements"][0])
        self.assertEqual(cells[(0, 1)]["elements"][0]["tags"]["level"], "1")

    def test_idempotent(self):
        elements = [
            node(1, shop="books", name="Hudson", level="1"),
            square(2, indoor="room", level="0;1"),
            square(3, indoor="room", level="0"),
            square(4, highway="steps", level="0;1"),
        ]
        once, _, _ = run(elements)
        twice, report, dropped = run(copy.deepcopy(once))
        self.assertEqual(once, twice)
        self.assertEqual((report, dropped), ({}, 0))

    def test_named_places_are_never_changed(self):
        elements = [node(1, shop="books", name="Hudson", level="1"),
                    square(2, indoor="room", name="Lounge", level="0;1"),
                    node(3, amenity="toilets", level="2")]
        before = copy.deepcopy(elements)
        els, report, _ = run(elements)
        self.assertEqual(els, before)
        self.assertEqual(report, {})


class Overlap(unittest.TestCase):
    """Back Bay Station's circle takes in Copley Place corridors on Prudential Center's floors."""

    def setUp(self):
        self.areas = {"Station": area(39.9, -73.1, 40.0005, -72.9995), "Mall": area(39.9995, -73.0005, 40.1, -72.9)}
        self.elements = [
            node(1, LAT - 0.001, LON - 0.001, shop="books", name="News", level="0"),  # station only
            node(2, LAT + 0.001, LON + 0.001, shop="clothes", name="Gap", level="2"),  # mall only
            square(3, LAT - 0.00005, LON - 0.00005, indoor="corridor", level="0;2"),  # in both
            square(4, LAT - 0.001, LON - 0.001, indoor="room", level="2"),  # station only, unnamed
        ]

    def test_an_element_in_two_venues_keeps_either_venues_floors(self):
        cells = {(0, 0): {"elements": self.elements}}
        problems, dropped = pr.prune_cells(cells, self.areas)
        report = found(problems)
        els = by_id(cells[(0, 0)]["elements"])
        self.assertEqual(els[3]["tags"]["level"], "0;2")  # the mall keeps 2
        self.assertNotIn(4, els)  # the station keeps no 2 of its own
        self.assertEqual(report["Station"], [2.0])
        b = pr.Bundle(cells, self.areas)
        self.assertEqual(b.listed("Station"), [0.0])  # the corridor's 2 is credited to the mall
        self.assertEqual(b.listed("Mall"), [2.0])  # and its 0 to the station
        self.assertEqual((b.unnamed("Station"), b.unnamed("Mall")), ([], []))

    def test_a_floor_neither_venue_keeps_counts_for_both(self):
        self.elements[2]["tags"]["level"] = "0;5"
        b = pr.Bundle({(0, 0): {"elements": self.elements}}, self.areas)
        self.assertIn(5.0, b.unnamed("Station"))
        self.assertIn(5.0, b.unnamed("Mall"))


class Stations(unittest.TestCase):
    """IndoorMapParsing.isIndoorLandside: a floored station is an amenity on its floor."""

    def test_a_named_station_inside_a_terminal_keeps_its_floor(self):
        # SEA's satellite trains ("S Gates") on B1.
        els, report, _ = run([terminal(100, LAT, LON), *filler(1, LAT + 0.0001, LON + 0.0001, "1"),
                              node(20, LAT + 0.001, LON + 0.001, shop="books", name="Hudson", level="1"),
                              node(21, LAT + 0.001, LON + 0.0012, railway="station", name="S Gates", level="-1"),
                              square(22, LAT + 0.0012, LON + 0.0012, indoor="area", level="-1")])
        self.assertEqual(report, {})
        self.assertEqual(by_id(els)[22]["tags"]["level"], "-1")

    def test_an_unnamed_one_loses_its_floor_but_stays(self):
        # Washington Union Station's bus station on 1.
        els, report, dropped = run([node(1, shop="books", name="Hudson", level="0"),
                                    node(2, amenity="bus_station", public_transport="station", level="1")])
        self.assertEqual(report["V"], [1.0])
        self.assertEqual(dropped, 0)
        self.assertEqual(by_id(els)[2]["tags"], {"amenity": "bus_station", "public_transport": "station",
                                                 "level:pruned": "1"})

    def test_one_outside_the_terminals_names_its_floor_only_outside_them(self):
        # The Blue Line's "O'Hare" under ORD's car park: B2 stays in the fallback plan (no
        # outline under the camera), but a terminal with nothing named on B2 loses it.
        els, report, _ = run([terminal(100, LAT, LON), *filler(1, LAT + 0.0001, LON + 0.0001, "1"),
                              node(20, LAT + 0.001, LON + 0.001, shop="books", name="Hudson", level="1"),
                              square(21, LAT + 0.0015, LON + 0.0015, indoor="room", level="-2"),  # in T100
                              node(30, LAT + 0.01, LON + 0.01, railway="station", name="O'Hare", level="-2"),
                              square(31, LAT + 0.01, LON + 0.0101, indoor="room", level="-2")])  # outside
        self.assertEqual(report, {"V/T100": [-2.0]})
        els = by_id(els)
        self.assertNotIn(21, els)
        self.assertEqual(els[31]["tags"]["level"], "-2")


class Outlines(unittest.TestCase):
    """The app's venue is one outline: its floor button lists what's inside it (and its piers)."""

    def test_each_terminal_answers_for_its_own_floors(self):
        t1 = [terminal(100, LAT, LON), *filler(1, LAT + 0.0001, LON + 0.0001, "1"),
              node(20, LAT + 0.001, LON + 0.001, shop="books", name="Hudson", level="1"),
              square(21, LAT + 0.0015, LON + 0.0015, indoor="room", level="2")]
        t2 = [terminal(200, LAT + 0.01, LON), *filler(201, LAT + 0.0101, LON + 0.0001, "2"),
              node(220, LAT + 0.011, LON + 0.001, shop="books", name="Relay", level="2"),
              square(221, LAT + 0.0115, LON + 0.0015, indoor="room", level="1")]
        els, report, _ = run(t1 + t2)
        self.assertEqual(report, {"V/T100": [2.0], "V/T200": [1.0]})  # the airport as a whole: fine
        self.assertEqual(set(by_id(els)) & {21, 221}, set())

    def test_nested_outlines_keep_only_what_both_name(self):
        # SFO's Boarding Area C inside Harvey Milk Terminal 1: the camera may pick either, and
        # each shows everything inside it.
        outer, inner = terminal(100, LAT, LON, size=0.004), terminal(200, LAT + 0.001, LON + 0.001, size=0.001)
        els, report, _ = run([outer, inner, *filler(1, LAT + 0.0011, LON + 0.0011, "1"),
                              node(20, LAT + 0.003, LON + 0.003, shop="books", name="Hudson", level="0"),  # T100 only
                              node(21, LAT + 0.0015, LON + 0.0018, shop="books", name="Relay", level="1"),
                              square(22, LAT + 0.0012, LON + 0.0016, size=0.00005, indoor="area", level="0;1")])
        self.assertEqual(report, {"V/T200": [0.0]})
        self.assertEqual(by_id(els)[22]["tags"]["level"], "1")
        self.assertEqual(by_id(els)[22]["tags"]["level:pruned"], "0")

    def test_an_outline_outside_every_venue_area_is_still_checked(self):
        # Powell Street Station, in the Salesforce Transit Center's cells but outside its box.
        cells = {(0, 0): {"elements": [terminal(100, LAT, LON, railway="station"),
                                       *filler(1, LAT + 0.0001, LON + 0.0001, "-1"),
                                       node(20, LAT + 0.01, LON + 0.01, shop="books", name="Hudson", level="0")]}}
        areas = {"Far": area(LAT + 0.005, LON + 0.005, LAT + 0.02, LON + 0.02)}
        self.assertEqual(found(pr.Bundle(cells, areas).problems()), {"Far/T100": [-1.0]})
        problems, dropped = pr.prune_cells(cells, areas, names=["Far"])  # a named run reaches it too
        self.assertEqual((found(problems), dropped), ({"Far/T100": [-1.0]}, 8))

    def test_a_named_room_on_many_floors_is_never_stripped(self):
        room = square(2, indoor="room", name="Food Hall", level="0;1;2;3;4")
        els, _, _ = run([square(1, highway="steps", level="0;1"), room])
        self.assertEqual(by_id(els)[2]["tags"]["level"], "0;1;2;3;4")

    def test_a_pier_gate_keeps_its_terminals_floor(self):
        # ORD B22, 55 m outside Terminal 1's wall, on floor 3.
        els, report, _ = run([terminal(100, LAT, LON), *filler(1, LAT + 0.0001, LON + 0.0001, "3"),
                              node(20, LAT + 0.0025, LON + 0.001, aeroway="gate", ref="B22", level="3")])
        self.assertEqual(report, {})
        self.assertEqual(len(els), 10)

    def test_a_terminal_with_only_stairs_lists_nothing(self):
        # DTW's McNamara Terminal: the fallback lists the stairs' floors.
        stairs = [square(k, LAT + 0.0001 * k, LON + 0.0001, size=0.00005, highway="steps", level="0;1")
                  for k in range(1, 9)]
        els, report, dropped = run([terminal(100, LAT, LON), *stairs,
                                    node(20, LAT + 0.01, LON + 0.01, shop="books", name="Hudson", level="0")])
        self.assertEqual(report, {"V/T100": [0.0, 1.0]})
        self.assertEqual(dropped, 8)

    def test_a_small_outline_is_no_venue(self):
        els, report, _ = run([terminal(100, LAT, LON), *filler(1, LAT + 0.0001, LON + 0.0001, "1", n=7),
                              node(20, LAT + 0.01, LON + 0.01, shop="books", name="Hudson", level="1")])
        self.assertEqual(report, {})

    def test_a_mall_needs_three_tenants(self):
        mall = square(100, LAT, LON, size=0.002, shop="mall", name="Mall")
        els, report, _ = run([mall, *filler(1, LAT + 0.0001, LON + 0.0001, "1"),
                              square(20, LAT + 0.001, LON + 0.001, size=0.00005, indoor="room", name="Escalator", level="2"),
                              node(30, LAT + 0.01, LON + 0.01, shop="books", name="Hudson", level="1")])
        self.assertEqual(report, {"V": [2.0]})  # the mall isn't a venue, so only the area says so

    def test_a_relation_outline_counts(self):
        ring = square(0, LAT, LON, size=0.002)["geometry"]
        rel = {"type": "relation", "id": 100, "tags": {"type": "multipolygon", "aeroway": "terminal", "name": "T"},
               "members": [{"role": "outer", "geometry": ring[:3]}, {"role": "outer", "geometry": ring[2:]}]}
        els, report, _ = run([rel, *filler(1, LAT + 0.0001, LON + 0.0001, "1"),
                              node(20, LAT + 0.01, LON + 0.01, shop="books", name="Hudson", level="1"),
                              square(21, LAT + 0.0015, LON + 0.0015, indoor="room", level="2")])
        self.assertEqual(report, {"V": [2.0], "V/T": [1.0, 2.0]})  # Hudson, outside T, names 1

    def test_a_toilet_room_the_app_drops_names_nothing(self):
        bare = square(1, room="toilets", level="1")
        self.assertFalse(pr.named_place(bare, pr.plan_kind(bare)))
        self.assertTrue(pr.named_place(square(1, room="toilets", indoor="room", level="1")))


class Edges(unittest.TestCase):
    def test_a_named_shop_on_many_floors_is_never_stripped(self):
        shop = node(2, shop="department_store", name="Macy's", level="0;1;2;3;4")
        els, _, dropped = run([square(1, highway="steps", level="0;1"), shop])
        self.assertEqual(by_id(els)[2]["tags"]["level"], "0;1;2;3;4")
        self.assertEqual(dropped, 1)

    def test_a_lift_node_lists_no_floor(self):
        els = [square(1, indoor="room", name="Lounge", level="1"), node(2, highway="elevator", level="0;1;2")]
        self.assertEqual(pr.listed_floors(els), [1.0])

    def test_names_limit_what_is_changed(self):
        cells = {(0, 0): {"elements": [node(1, LAT, LON, shop="books", name="A", level="1"),
                                       square(2, LAT, LON, indoor="room", level="0"),
                                       node(3, LAT + 1, LON, shop="books", name="B", level="1"),
                                       square(4, LAT + 1, LON, indoor="room", level="0")]}}
        areas = {"A": area(39.5, -73.5, 40.5, -72.5), "B": area(40.5, -73.5, 41.5, -72.5)}
        report = found(pr.prune_cells(cells, areas, names=["A"])[0])
        self.assertEqual(list(report), ["A"])
        self.assertEqual(set(by_id(cells[(0, 0)]["elements"])), {1, 3, 4})

    def test_enrich_after_prune_changes_nothing(self):
        import indoor_enrich as en
        gates = [node(i, LAT, LON + i * 0.00035, aeroway="gate", ref=f"G{i}", level="1") for i in range(1, 5)]
        gates.append(node(9, LAT, LON + 0.0035, aeroway="gate", ref="G9", **{"level:ways": "0"}))
        elements = gates + [square(10, indoor="room", level="0;1"), square(11, indoor="corridor", level="0")]
        cells = {(0, 0): {"elements": elements}}
        pr.prune_cells(cells, {"V": EVERYWHERE})
        before = copy.deepcopy(cells)
        en.enrich_cells(cells, "V", True, None)
        self.assertEqual(cells, before)
        self.assertEqual(pr.prune_cells(cells, {"V": EVERYWHERE}), ([], 0))


class Listed(unittest.TestCase):
    def test_mirrors_floor_values(self):
        # Stairs, lifts and shafts list no floor while anything else does; walls do.
        els = [square(1, indoor="room", level="1"), square(2, highway="steps", level="0;1;2"),
               square(3, indoor="room", level="1;2;3;4;5"), square(4, indoor="wall", level="7")]
        self.assertEqual(pr.listed_floors(els), [1.0, 7.0])
        self.assertEqual(pr.listed_floors(els[1:2]), [0.0, 1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
