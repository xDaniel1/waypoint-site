"""Checks for the indoor publisher. Run: python3 -m unittest discover -s scripts -p 'test_*.py'"""
import json, pathlib, sys, tempfile, time, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import build_indoor_plans as b

DAY = 86400


class Freshness(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.now = time.time()

    def cell(self, body):
        path = self.dir / "indoor_1_2.json"
        path.write_text(body if isinstance(body, str) else json.dumps(body))
        return path

    def test_recent_cell_from_current_query_is_fresh(self):
        rel = {"type": "relation", "members": [{"role": "outer", "geometry": [{"lat": 0, "lon": 0}, {"lat": 0, "lon": 1}]}]}
        path = self.cell({"landside": True, "elements": [rel, {"type": "way"}]})
        self.assertTrue(b.is_fresh(path, self.now - DAY, self.now))

    def test_empty_cell_is_fresh(self):
        self.assertTrue(b.is_fresh(self.cell({"landside": True, "elements": []}), self.now - DAY, self.now))

    def test_cell_without_the_landside_flag_is_from_the_old_query(self):
        # Before the app's query (landside, repeat_on, lifts, shop=mall): rebuilt however recent.
        self.assertFalse(b.is_fresh(self.cell({"elements": []}), self.now - DAY, self.now))
        self.assertFalse(b.is_fresh(self.cell({"landside": "yes", "elements": []}), self.now - DAY, self.now))

    def test_old_or_unknown_fetch_time_is_stale(self):
        path = self.cell({"elements": []})
        self.assertFalse(b.is_fresh(path, self.now - b.MAX_AGE_DAYS * DAY, self.now))
        self.assertFalse(b.is_fresh(path, None, self.now))

    def test_impossible_fetch_times_are_stale(self):
        path = self.cell({"elements": []})
        for t in (float("nan"), float("inf"), self.now + DAY, True):
            self.assertFalse(b.is_fresh(path, t, self.now), t)

    def test_cell_from_old_query_is_stale(self):
        # `out tags geom` left relations without members, so no outlines.
        path = self.cell({"elements": [{"type": "relation", "tags": {"aeroway": "terminal"}}]})
        self.assertFalse(b.is_fresh(path, self.now - DAY, self.now))
        path = self.cell({"elements": [{"type": "relation", "members": None}]})
        self.assertFalse(b.is_fresh(path, self.now - DAY, self.now))

    def test_missing_or_broken_file_is_stale(self):
        self.assertFalse(b.is_fresh(self.dir / "nope.json", self.now, self.now))
        self.assertFalse(b.is_fresh(self.cell("not json"), self.now, self.now))
        self.assertFalse(b.is_fresh(self.cell([1]), self.now, self.now))


class Answers(unittest.TestCase):
    def test_whole_answer_is_accepted(self):
        self.assertEqual(b.check_answer(b'{"elements":[{"type":"way","id":2}]}'), [{"type": "way", "id": 2}])

    def test_partial_answer_is_rejected(self):
        with self.assertRaises(ValueError):
            b.check_answer(b'{"elements":[],"remark":"runtime error: Query timed out"}')

    def test_answer_without_elements_is_rejected(self):
        for body in (b'{}', b'[]', b'{"elements":null}'):
            with self.assertRaises(ValueError):
                b.check_answer(body)

    def test_relation_without_members_is_rejected(self):
        # What the old `out tags geom` query returned.
        for body in (b'{"elements":[{"type":"relation","id":1}]}', b'{"elements":[{"type":"relation","id":1,"members":null}]}'):
            with self.assertRaises(ValueError):
                b.check_answer(body)

    def test_members_without_points_are_kept(self):
        # Map data as it is: every retry would get the same, so rejecting would block the cell for good.
        for body in (b'{"elements":[{"type":"relation","id":1,"members":[]}]}',
                     b'{"elements":[{"type":"relation","id":1,"members":[{"type":"way","role":"outer"}]}]}',
                     b'{"elements":[{"type":"relation","id":1,"members":[{"type":"way","geometry":[null,null]}]}]}',
                     b'{"elements":[{"type":"relation","id":1,"members":[{"type":"relation","id":1,"ref":5,"role":""}]}]}'):
            self.assertEqual(len(b.check_answer(body)), 1)

    def test_malformed_elements_are_rejected(self):
        for body in (b'{"elements":[{}]}', b'{"elements":[{"type":"area","id":1}]}', b'{"elements":[{"type":"way"}]}', b'{"elements":[1]}',
                     b'{"elements":[{"type":"way","id":1,"tags":{"level":1}}]}',
                     b'{"elements":[{"type":"way","id":99999999999999999999}]}',
                     b'{"elements":[{"type":"relation","id":1,"members":{"a":1}}]}',
                     b'{"elements":[{"type":"node","id":1,"lat":NaN,"lon":0}]}',
                     b'{"elements":[{"type":"relation","id":1,"members":[{"type":"node","lat":1e999,"lon":0}]}]}',
                     b'{"elements":[{"type":"relation","id":1,"members":[{"type":"way","role":3}]}]}'):
            with self.assertRaises(ValueError):
                b.check_answer(body)

    def test_slim_leaves_no_null_points(self):
        p = lambda lat, lon: {"lat": lat, "lon": lon}
        els = [
            {"type": "relation", "id": 1, "members": [{"type": "way", "role": "", "geometry": [p(0, 0), p(0, 1), None, p(1, 1), p(1, 0), None, p(2, 2)]}]},
            {"type": "way", "id": 2, "geometry": [p(0, 0), None, p(1, 1)]},
            {"type": "node", "id": 3, "lat": None, "lon": None},
            {"type": "way", "id": 4, "geometry": [p(0, 0), p(1, 1)]},
        ]
        out = b.slim(els)
        self.assertEqual([e["id"] for e in out], [1, 4])
        self.assertEqual([len(m["geometry"]) for m in out[0]["members"]], [2, 2])
        self.assertNotIn("null", json.dumps(out))

    def test_slim_drops_node_ids_and_keeps_geometry(self):
        way = {"type": "way", "nodes": [1, 2], "geometry": [{"lat": 0, "lon": 0}]}
        rel = {"type": "relation", "members": [{"role": "", "geometry": []}]}
        self.assertEqual(b.slim([way, rel]), [{"type": "way", "geometry": [{"lat": 0, "lon": 0}]}, rel])


class Scheduling(unittest.TestCase):
    def test_least_recently_attempted_first(self):
        index = {"fetched": {"1_1": 300, "1_2": 100}, "tried": {"1_2": 500, "1_3": 200}}
        # never tried (0) < 1_3 tried at 200 < 1_1 fetched at 300 < 1_2 tried at 500
        self.assertEqual(b.order([(1, 1), (1, 2), (1, 3), (9, 9)], index), [(9, 9), (1, 3), (1, 1), (1, 2)])

    def test_a_cut_off_run_resumes_after_the_cells_it_did(self):
        cells = [(0, c) for c in range(6)]
        index = {"fetched": {}, "tried": {}}
        for cell in b.order(cells, index)[:3]:  # first run gets through three
            index["tried"][f"{cell[0]}_{cell[1]}"] = 1000
        self.assertEqual(b.order(cells, index)[:3], [(0, 3), (0, 4), (0, 5)])

    def test_stuck_cells_are_listed(self):
        week = 7 * DAY
        index = {"fetched": {"a": 0, "b": 10 * DAY}, "tried": {"a": 10 * DAY, "b": 10 * DAY, "c": 10 * DAY}}
        self.assertEqual(b.stuck_cells(index, week=week), ["a"])  # "c" has no first try recorded
        self.assertEqual(b.stuck_cells({"fetched": {}, "tried": {"1_2": 10 * DAY, "9_9": 10 * DAY}, "first_tried": {"1_2": 1 * DAY, "9_9": DAY}}, [(1, 2)], week), ["1_2"])
        # a new venue whose first try just failed is not "a week" stuck
        self.assertEqual(b.stuck_cells({"fetched": {}, "tried": {"5_5": 10 * DAY}, "first_tried": {"5_5": 10 * DAY}}, None, week), [])

    def test_many_failures_turn_the_run_red(self):
        self.assertEqual(b.outcome(written=500, failed=3), 0)
        self.assertEqual(b.outcome(written=500, failed=40), 0)
        self.assertEqual(b.outcome(written=500, failed=60), 1)
        self.assertEqual(b.outcome(written=1, failed=300), 1)
        self.assertEqual(b.outcome(written=0, failed=0), 0)
        self.assertEqual(b.outcome(written=0, failed=1), 1)  # the only stale cell failed
        self.assertEqual(b.outcome(written=1, failed=3), 1)  # a small retry run that mostly failed
        self.assertEqual(b.outcome(written=4, failed=1), 0)


class Files(unittest.TestCase):
    def test_atomic_write_replaces_and_leaves_no_temp(self):
        d = pathlib.Path(tempfile.mkdtemp())
        path = d / "indoor_1_2.json"
        path.write_text("old")
        b.write_atomic(path, b'{"elements":[]}')
        self.assertEqual(path.read_text(), '{"elements":[]}')
        self.assertEqual([p.name for p in d.iterdir()], ["indoor_1_2.json"])

    def test_failed_write_leaves_old_file_and_no_temp(self):
        d = pathlib.Path(tempfile.mkdtemp())
        path = d / "indoor_1_2.json"
        path.write_text("old")
        real = b.os.replace
        b.os.replace = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                b.write_atomic(path, b"new")
        finally:
            b.os.replace = real
        self.assertEqual(path.read_text(), "old")
        self.assertEqual([p.name for p in d.iterdir()], ["indoor_1_2.json"])

    def test_no_request_past_the_budget(self):
        old = b.BUDGET_SECONDS
        calls = []
        real = b.urllib.request.urlopen
        b.urllib.request.urlopen = lambda *a, **k: calls.append(1)
        try:
            b.BUDGET_SECONDS = time.time() - b.STARTED + 10  # 10 s left, less than one request
            with self.assertRaises(b.BudgetSpent):
                b.fetch(1, 2)
        finally:
            b.BUDGET_SECONDS = old
            b.urllib.request.urlopen = real
        self.assertEqual(calls, [])

    def test_read_is_capped_in_total_not_per_chunk(self):
        class Trickle:
            def read(self, n):
                time.sleep(0.02)
                return b"x"
        with self.assertRaises(TimeoutError):
            b.read_capped(Trickle(), 0.1)

    def test_hard_limit_stops_a_blocked_read(self):
        import socket
        a, c = socket.socketpair()
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                with b.hard_limit(0.2):
                    a.recv(1)  # nothing is ever sent
            self.assertLess(time.monotonic() - started, 1)
        finally:
            a.close(); c.close()

    def test_read_returns_whole_body(self):
        class Body:
            def __init__(self):
                self.parts = [b"ab", b"cd", b""]
            def read(self, n):
                return self.parts.pop(0)
        self.assertEqual(b.read_capped(Body(), 5), b"abcd")

    def test_budget_stop_is_not_a_try(self):
        cells = {(1, 2): ["X"]}
        index = {"fetched": {}, "tried": {}, "first_tried": {}, "osm": {}}
        old = b.BUDGET_SECONDS
        try:
            b.BUDGET_SECONDS = time.time() - b.STARTED + b.REQUEST_TIMEOUT + 0.5  # passes run's check, not fetch's
            real = b.time_left
            calls = iter([b.REQUEST_TIMEOUT + 1, 1])
            b.time_left = lambda: next(calls, 0)
            self.assertEqual(b.run(cells, index, time.time()), 1)  # nothing tried at all is red
        finally:
            b.BUDGET_SECONDS = old
            b.time_left = real
        self.assertEqual(index["tried"], {})

    def test_older_snapshot_from_a_lagging_mirror_is_refused(self):
        answers = iter([b'{"osm3s":{"timestamp_osm_base":"2026-11-20T00:00:00Z"},"elements":[]}',
                        b'{"elements":[]}',  # no snapshot at all: can't tell, so not taken
                        b'{"osm3s":{"timestamp_osm_base":"2026-11-22T00:00:00Z"},"elements":[]}'])
        class Resp:
            def __init__(self, body): self.parts = [body, b""]
            def read(self, n): return self.parts.pop(0)
            def __enter__(self): return self
            def __exit__(self, *a): return False
        real = b.urllib.request.urlopen
        b.urllib.request.urlopen = lambda *a, **k: Resp(next(answers))
        try:
            data = b.fetch(1, 2, not_before="2026-11-21T00:00:00Z")
        finally:
            b.urllib.request.urlopen = real
        self.assertEqual(b.snapshot_of(data), "2026-11-22T00:00:00Z")  # the lagging one was skipped

    def test_a_stalled_replica_is_only_a_fallback(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * DAY))
        bodies = [json.dumps({"osm3s": {"timestamp_osm_base": old}, "elements": []}).encode(),
                  json.dumps({"osm3s": {"timestamp_osm_base": now}, "elements": []}).encode()]
        class Resp:
            def __init__(self, body): self.parts = [body, b""]
            def read(self, n): return self.parts.pop(0)
            def __enter__(self): return self
            def __exit__(self, *a): return False
        real = b.urllib.request.urlopen
        calls = iter(bodies)
        b.urllib.request.urlopen = lambda *a, **k: Resp(next(calls))
        try:
            self.assertEqual(b.snapshot_of(b.fetch(1, 2)), now)  # skipped the stalled one
            calls = iter([bodies[0]] * 6)
            b.urllib.request.urlopen = lambda *a, **k: Resp(next(calls))
            self.assertEqual(b.snapshot_of(b.fetch(1, 2)), old)  # all stalled: the newest of them
        finally:
            b.urllib.request.urlopen = real

    def test_only_a_current_answer_restarts_the_clock(self):
        d = pathlib.Path(tempfile.mkdtemp())
        old_out, old_index, old_fetch = b.OUT, b.INDEX, b.fetch
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            b.OUT, b.INDEX = d, d / "_data" / "i.json"
            b.fetch = lambda row, col, not_before=None: json.dumps({"osm3s": {"timestamp_osm_base": now}, "elements": []}).encode()
            index = b.load_index()
            b.run({(1, 2): ["X"]}, index, time.time())
            self.assertGreater(index["fetched"]["1_2"], 0)
        finally:
            b.OUT, b.INDEX, b.fetch = old_out, old_index, old_fetch

    def test_impossible_snapshots_are_ignored(self):
        for v in ("9999-99-99T99:99:99Z", "2999-01-01T00:00:00Z", "2026-11-22 07:20:03", 5):
            self.assertFalse(b.is_snapshot(v), v)
        self.assertTrue(b.is_snapshot("2026-11-22T07:20:03Z"))

    def test_cell_carries_its_snapshot(self):
        self.assertEqual(json.loads(b.encode([], "2026-11-22T00:00:00Z")),
                         {"landside": True, "generator": b.GENERATOR, "elements": [], "osm_base": "2026-11-22T00:00:00Z"})
        self.assertIsNone(b.snapshot_of(b'{"elements":[]}'))

    def test_a_confirmed_cell_refuses_an_older_mirror(self):
        # Accept X at snapshot 1, confirm X unchanged at snapshot 3: the guard is now 3, so Y from a
        # mirror at snapshot 2 is refused.
        d = pathlib.Path(tempfile.mkdtemp())
        old_out, old_index, old_fetch = b.OUT, b.INDEX, b.fetch
        snapshots = iter(["2026-11-01T00:00:00Z", "2026-11-03T00:00:00Z"])
        seen = []
        def fake_fetch(row, col, not_before=None):
            seen.append(not_before)
            return json.dumps({"osm3s": {"timestamp_osm_base": next(snapshots)}, "elements": []}).encode()
        try:
            b.OUT, b.INDEX, b.fetch = d, d / "_data" / "i.json", fake_fetch
            index = b.load_index()
            b.run({(1, 2): ["X"]}, index, time.time())
            index["fetched"]["1_2"] = 0  # make it due again
            b.run({(1, 2): ["X"]}, index, time.time())
            self.assertEqual(seen, [None, "2026-11-01T00:00:00Z"])
            self.assertEqual(index["fetched"].get("1_2", 0), 0)  # old snapshots: not a refresh
            self.assertEqual(index["osm"]["1_2"], "2026-11-03T00:00:00Z")
            self.assertEqual(json.loads((d / "indoor_1_2.json").read_text())["osm_base"], "2026-11-03T00:00:00Z")
        finally:
            b.OUT, b.INDEX, b.fetch = old_out, old_index, old_fetch

    def test_index_round_trips_and_ignores_junk(self):
        d = pathlib.Path(tempfile.mkdtemp())
        old = b.INDEX
        try:
            b.INDEX = d / "_data" / "indoor_fetched.json"
            self.assertEqual(b.load_index(), {"fetched": {}, "tried": {}, "first_tried": {}, "osm": {}})
            b.save_index({"fetched": {"1_2": 5}, "tried": {"1_2": 6}, "osm": {"1_2": "2026-11-22T07:20:03Z"}})
            self.assertEqual(b.load_index(), {"fetched": {"1_2": 5}, "tried": {"1_2": 6}, "first_tried": {}, "osm": {"1_2": "2026-11-22T07:20:03Z"}})
            b.INDEX.write_text('{"fetched": {"a": "x", "b": 1, "c": NaN, "d": Infinity}, "tried": []}')
            self.assertEqual(b.load_index(), {"fetched": {"b": 1}, "tried": {}, "first_tried": {}, "osm": {}})
        finally:
            b.INDEX = old


class AppFormat(unittest.TestCase):
    """The CDN cell is the app bundle's format: same query, same trimming, same fixes."""

    def test_query_is_the_apps(self):
        q = b.query(6792, -12329)
        for clause in ("nwr[repeat_on][shop]", "node[highway=elevator][level]", "way[shop=mall]", "rel[shop=mall]",
                       "nwr[aeroway=passenger_pickup]", "nwr[railway=station]", "out body geom"):
            self.assertIn(clause, q)
        self.assertIn("[timeout:60]", q)

    def test_trim_keeps_what_the_apps_read(self):
        s, w, n, e = b.fv.cell_box(1, 2)
        lat, lon = (s + n) / 2, (w + e) / 2
        shop = {"type": "node", "id": 1, "lat": lat + 1e-9, "lon": lon, "tags": {"shop": "gift", "level": "1", "name": "A", "phone": "+1"}}
        bench = {"type": "node", "id": 2, "lat": lat, "lon": lon, "tags": {"amenity": "bench", "level": "1"}}
        away = {"type": "node", "id": 3, "lat": n + 1, "lon": lon, "tags": {"shop": "gift", "level": "1"}}
        unfloored = {"type": "node", "id": 4, "lat": lat, "lon": lon, "tags": {"shop": "gift", "name": "B"}}
        kept = b.trim([shop, bench, away, unfloored], 1, 2)
        self.assertEqual(kept, [{"type": "node", "id": 1, "tags": {"shop": "gift", "level": "1", "name": "A"},
                                 "lat": round(lat, 6), "lon": round(lon, 6)}])

    def test_enrich_floors_gates_and_leaves_the_index_alone(self):
        d = pathlib.Path(tempfile.mkdtemp())
        old_out = b.OUT
        try:
            b.OUT = d / "indoor"
            b.OUT.mkdir()
            venue = {"short_name": "XYZ", "latitude": 0.003, "longitude": 0.003, "radius_m": 200, "kind": "airport"}
            gates = [{"type": "node", "id": i, "lat": 0.003, "lon": 0.001 + i * 0.0003,
                      "tags": {"aeroway": "gate", "ref": f"G{i}", **({"level": "1"} if i != 3 else {})}} for i in range(1, 6)]
            (b.OUT / "indoor_0_0.json").write_bytes(b.encode(gates, "2026-11-22T00:00:00Z"))
            (b.OUT / "indoor_0_1.json").write_bytes(json.dumps({"elements": [dict(gates[2])]}).encode())  # old format
            self.assertEqual(b.enrich([venue], overture=False), 1)
            cell = json.loads((b.OUT / "indoor_0_0.json").read_bytes())
            self.assertEqual(cell["elements"][2]["tags"], {"aeroway": "gate", "ref": "G3", "level": "1", "level:inferred": "gates"})
            self.assertEqual(cell["osm_base"], "2026-11-22T00:00:00Z")
            self.assertNotIn("level", json.loads((b.OUT / "indoor_0_1.json").read_bytes())["elements"][0]["tags"])
            self.assertEqual(b.enrich([venue], overture=False), 0, "a second pass changes nothing")
        finally:
            b.OUT = old_out


class BundleFixes(unittest.TestCase):
    """enrich(): the iOS bundle's enrich and prune over the CDN cells, with Overture cached."""

    def setUp(self):
        import contextlib, io
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.old = (b.OUT, b.OVERTURE_CACHE, b.indoor_enrich.overture_places)
        b.OUT, b.OVERTURE_CACHE = self.dir / "indoor", self.dir / "_data" / "overture_places.json"
        b.OUT.mkdir()
        self.mall = {"short_name": "M", "latitude": 0.003, "longitude": 0.003, "radius_m": 200, "kind": "mall"}
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        b.OUT, b.OVERTURE_CACHE, b.indoor_enrich.overture_places = self.old

    def write(self, elements):
        (b.OUT / "indoor_0_0.json").write_bytes(b.encode(elements, "2026-11-22T00:00:00Z"))

    def read(self):
        return json.loads((b.OUT / "indoor_0_0.json").read_bytes())

    def room(self, i, level, **tags):
        lat, lon = 0.003, 0.001 + i * 0.0003
        g = [{"lat": lat, "lon": lon}, {"lat": lat, "lon": lon + 1e-4}, {"lat": lat + 1e-4, "lon": lon + 1e-4}, {"lat": lat, "lon": lon}]
        return {"type": "way", "id": i, "geometry": g, "tags": {"indoor": "room", "level": level, **tags}}

    def test_a_floor_with_no_named_place_is_taken_off(self):
        shop = {"type": "node", "id": 1, "lat": 0.003, "lon": 0.003, "tags": {"shop": "gift", "name": "A", "level": "0"}}
        self.write([self.room(2, "1"), shop])
        b.indoor_enrich.overture_places = lambda box, path=None: []
        self.assertEqual(b.enrich([self.mall], overture=False), 0, "without tonight's Overture names: not pruned")
        self.assertEqual(b.enrich([self.mall], overture=True), 1)
        cell = self.read()
        self.assertEqual([e["id"] for e in cell["elements"]], [1], "the unnamed room on 1 goes; the named shop on 0 stays")
        self.assertEqual(cell["osm_base"], "2026-11-22T00:00:00Z")
        self.assertEqual(b.enrich([self.mall], overture=True), 0, "a second pass changes nothing")

    def test_overture_rows_are_cached_by_release_shape_and_box(self):
        calls = []
        def fetch(box, path=None):
            calls.append(box)
            return [("g1", "PIQ", "gift_shop", 0.003, 0.003, "Level 2")]
        box = (0.0, 0.0, 0.006, 0.006)
        cache = {b.box_key(box): {"release": "r1", "box": list(box), "places": [["g0", "Old", "gift_shop", 0.0, 0.0]]}}  # no schema
        places = b.cached_places(cache, "r1", fetch)
        self.assertEqual(places(box), [("g1", "PIQ", "gift_shop", 0.003, 0.003, "Level 2")])
        self.assertEqual(len(calls), 1, "rows cached without the schema (no address) are fetched again")
        self.assertEqual(cache[b.box_key(box)]["schema"], b.OVERTURE_SCHEMA)
        self.assertEqual(places(box), [("g1", "PIQ", "gift_shop", 0.003, 0.003, "Level 2")])
        self.assertEqual(len(calls), 1, "same release, shape and box: from the cache")
        b.cached_places(cache, "r2", fetch)(box)
        self.assertEqual(len(calls), 2, "a new release is fetched")

    def test_failed_fetch_uses_another_release_in_the_current_shape_only(self):
        def down(box, path=None):
            raise OSError("S3 down")
        box = (0.0, 0.0, 0.006, 0.006)
        rows = [["g1", "PIQ", "gift_shop", 0.003, 0.003, None]]
        cache = {b.box_key(box): {"release": "r1", "schema": b.OVERTURE_SCHEMA, "box": list(box), "places": rows}}
        self.assertEqual(b.cached_places(cache, "r2", down)(box), [tuple(rows[0])])
        cache[b.box_key(box)].pop("schema")
        with self.assertRaises(b.OvertureMissing):
            b.cached_places(cache, "r2", down)(box)

    def test_without_overture_names_and_points_stay(self):
        def down(box, path=None):
            raise OSError("no duckdb")
        b.indoor_enrich.overture_places = down
        named = self.room(2, "0", shop="gift", name="PIQ", **{"name:source": "overture", "ref:overture": "g1"})
        point = {"type": "node", "id": -5, "lat": 0.003, "lon": 0.004,
                 "tags": {"name": "Lush", "shop": "gift", "level": "0", "level:source": "address", "name:source": "overture", "ref:overture": "g2"}}
        self.write([named, point])
        self.assertEqual(b.enrich([self.mall], overture=True), 0)
        self.assertEqual([e["tags"]["name"] for e in self.read()["elements"]], ["PIQ", "Lush"],
                         "an earlier run's Overture name and store point are kept")
        self.assertEqual(json.loads(b.OVERTURE_CACHE.read_bytes()), {}, "nothing cached")

    def test_one_venues_failed_fetch_leaves_the_others_their_overture(self):
        # Mall M (cell 0,0) gets its places; mall F, far away in its own cells, can't: F keeps
        # its old name, M gets its store point, and M's rows are cached.
        self.mall["radius_m"] = 300  # takes in the mapped shops that settle the floor numbering
        far = dict(self.mall, short_name="F", latitude=1.003, longitude=1.003)
        frc = b.fv.cell(1.003, 1.003)
        old_name = self.room(2, "0", shop="gift", name="Old", **{"name:source": "overture", "ref:overture": "g9"})
        old_name["geometry"] = [{"lat": p["lat"] + 1, "lon": p["lon"] + 1} for p in old_name["geometry"]]
        (b.OUT / f"indoor_{frc[0]}_{frc[1]}.json").write_bytes(b.encode([old_name], None))
        mall = {"type": "way", "id": 50, "tags": {"shop": "mall", "building:levels": "2"},
                "geometry": [{"lat": 0.0005, "lon": 0.0005}, {"lat": 0.0005, "lon": 0.0055}, {"lat": 0.0055, "lon": 0.0055},
                             {"lat": 0.0055, "lon": 0.0005}, {"lat": 0.0005, "lon": 0.0005}]}
        shops = [{"type": "node", "id": i, "lat": 0.001, "lon": 0.001 * i, "tags": {"shop": "gift", "name": f"S{i}", "level": lv}}
                 for i, lv in ((1, "1"), (2, "2"))]
        self.write([mall] + shops)
        def fetch(box, path=None):
            if box[0] > 0.5:
                raise OSError("S3 timed out")
            return [("g1", "PIQ", "gift_shop", 0.003, 0.003, "2nd Floor")]
        b.indoor_enrich.overture_places = fetch
        b.enrich([far, self.mall], overture=True)
        self.assertEqual([(e["tags"]["name"], e["tags"]["level"]) for e in self.read()["elements"] if e["tags"].get("level:source")],
                         [("PIQ", "2")])
        far_cell = json.loads((b.OUT / f"indoor_{frc[0]}_{frc[1]}.json").read_bytes())
        self.assertEqual(far_cell["elements"][0]["tags"]["name"], "Old")
        self.assertEqual(list(json.loads(b.OVERTURE_CACHE.read_bytes())), [b.box_key(b.venue_box(self.mall))])

    def test_a_venue_without_tonights_overture_is_not_pruned(self):
        def down(box, path=None):
            raise OSError("S3 down")
        b.indoor_enrich.overture_places = down
        shop = {"type": "node", "id": 1, "lat": 0.003, "lon": 0.003, "tags": {"shop": "gift", "name": "A", "level": "0"}}
        self.write([self.room(2, "1"), shop])
        self.assertEqual(b.enrich([self.mall], overture=True), 0, "floor 1 may be named by Overture: kept for now")
        b.indoor_enrich.overture_places = lambda box, path=None: []
        self.assertEqual(b.enrich([self.mall], overture=True), 1, "pruned once tonight's places are had")

    def test_a_failed_enrich_with_overture_prunes_nothing(self):
        b.indoor_enrich.overture_places = lambda box, path=None: []
        broken = b.indoor_enrich.enrich
        def boom(*a, **k):
            raise RuntimeError("bug")
        b.indoor_enrich.enrich = boom
        try:
            shop = {"type": "node", "id": 1, "lat": 0.003, "lon": 0.003, "tags": {"shop": "gift", "name": "A", "level": "0"}}
            self.write([self.room(2, "1"), shop])
            self.assertEqual(b.enrich([self.mall], overture=True), 0, "floor 1 may be named by Overture: kept")
        finally:
            b.indoor_enrich.enrich = broken

    def test_no_box_is_fetched_past_the_deadline(self):
        calls = []
        old = b.STARTED
        try:
            b.STARTED = time.time() - b.OVERTURE_DEADLINE - 1
            with self.assertRaises(b.OvertureMissing):
                b.cached_places({}, "r1", lambda box, path=None: calls.append(box) or [])((0.0, 0.0, 0.006, 0.006))
        finally:
            b.STARTED = old
        self.assertEqual(calls, [])

    def test_a_venue_sharing_cells_with_one_not_yet_rebuilt_is_not_pruned(self):
        # M's only cell is rebuilt; W shares it and has an old-format cell. A room in both, on a
        # floor W's old cell may name, keeps that floor.
        b.indoor_enrich.overture_places = lambda box, path=None: []
        wide = dict(self.mall, short_name="W", latitude=0.003, longitude=0.0045, radius_m=300)
        shop = {"type": "node", "id": 1, "lat": 0.003, "lon": 0.003, "tags": {"shop": "gift", "name": "A", "level": "0"}}
        self.write([self.room(8, "1"), shop])
        (b.OUT / "indoor_0_1.json").write_bytes(json.dumps({"elements": []}).encode())  # old format
        self.assertEqual(b.enrich([self.mall, wide], overture=True), 0)
        (b.OUT / "indoor_0_1.json").write_bytes(b.encode([], None))
        self.assertEqual(b.enrich([self.mall, wide], overture=True), 1, "pruned once the group's cells are all rebuilt")

    def test_a_real_cancel_is_not_taken_for_the_time_limit(self):
        def interrupted(box, path=None):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            b.cached_places({}, "r1", interrupted)((0.0, 0.0, 0.006, 0.006))

    def test_the_time_limit_is_a_missing_box(self):
        old = b.OVERTURE_FETCH_LIMIT
        b.OVERTURE_FETCH_LIMIT = 1.2
        def slow(box, path=None):
            time.sleep(5)
        try:
            with self.assertRaises(b.OvertureMissing):
                b.cached_places({}, "r1", slow)((0.0, 0.0, 0.006, 0.006))
        finally:
            b.OVERTURE_FETCH_LIMIT = old

    def test_a_venue_with_a_cell_not_yet_rebuilt_is_not_pruned(self):
        b.indoor_enrich.overture_places = lambda box, path=None: []
        wide = dict(self.mall, latitude=0.003, longitude=0.0045, radius_m=300)  # cells (0, 0) and (0, 1)
        self.assertEqual(sorted(b.cells_for(0.003, 0.0045, 300)), [(0, 0), (0, 1)])
        shop = {"type": "node", "id": 1, "lat": 0.003, "lon": 0.003, "tags": {"shop": "gift", "name": "A", "level": "0"}}
        self.write([self.room(8, "1"), shop])
        (b.OUT / "indoor_0_1.json").write_bytes(json.dumps({"elements": []}).encode())  # old format
        self.assertEqual(b.enrich([wide], overture=True), 0, "floor 1 may be named in the old cell: kept")
        (b.OUT / "indoor_0_1.json").write_bytes(b.encode([], None))
        self.assertEqual(b.enrich([wide], overture=True), 1, "pruned once every cell is rebuilt")

    def test_each_box_is_asked_once_a_run(self):
        calls = []
        def down(box, path=None):
            calls.append(box)
            raise OSError("S3 down")
        box = (0.0, 0.0, 0.006, 0.006)
        rows = [["g1", "PIQ", "gift_shop", 0.003, 0.003, None]]
        cache = {b.box_key(box): {"release": "r1", "schema": b.OVERTURE_SCHEMA, "box": list(box), "places": rows}}
        places = b.cached_places(cache, "r2", down)
        self.assertEqual(places(box), places(box))
        other = (1.0, 1.0, 1.006, 1.006)
        for _ in range(2):
            with self.assertRaises(b.OvertureMissing):
                places(other)
        self.assertEqual(len(calls), 2)

    def test_venues_sharing_a_cell_are_grouped(self):
        areas = {"A": ("mall", None, [(0, 0), (0, 1)], None), "B": ("mall", None, [(0, 1), (0, 2)], None),
                 "C": ("mall", None, [(5, 5)], None), "D": ("mall", None, [(0, 2)], None)}
        self.assertEqual(sorted(map(sorted, b.sharing_groups(areas))), [["A", "B", "D"], ["C"]])

    def test_a_failed_enrich_prunes_nothing_and_restores_the_patches(self):
        b.indoor_enrich.overture_places = lambda box, path=None: []
        saved = {k: getattr(b.indoor_enrich, k) for k in ("areas", "load", "save", "overture_places")}
        broken = b.indoor_enrich.enrich
        def boom(*a, **k):
            raise RuntimeError("bug")
        b.indoor_enrich.enrich = boom
        try:
            shop = {"type": "node", "id": 1, "lat": 0.003, "lon": 0.003, "tags": {"shop": "gift", "name": "A", "level": "0"}}
            self.write([self.room(2, "1"), shop])
            self.assertEqual(b.enrich([self.mall], overture=True), 0, "nothing pruned after a failed enrich")
        finally:
            b.indoor_enrich.enrich = broken
        self.assertEqual({k: getattr(b.indoor_enrich, k) for k in saved}, saved)

    def test_store_points_come_from_one_run_over_every_venue(self):
        # Two malls sharing the cell, each finding the same Overture place: one point, not two,
        # and neither run strips the other's.
        mall = {"type": "way", "id": 50, "tags": {"shop": "mall", "building:levels": "2"},
                "geometry": [{"lat": 0.0005, "lon": 0.0005}, {"lat": 0.0005, "lon": 0.0055}, {"lat": 0.0055, "lon": 0.0055},
                             {"lat": 0.0055, "lon": 0.0005}, {"lat": 0.0005, "lon": 0.0005}]}
        shops = [{"type": "node", "id": i, "lat": 0.001, "lon": 0.001 * i, "tags": {"shop": "gift", "name": f"S{i}", "level": lv}}
                 for i, lv in ((1, "1"), (2, "2"))]
        self.write([mall] + shops)
        b.indoor_enrich.overture_places = lambda box, path=None: [("g1", "PIQ", "gift_shop", 0.003, 0.003, "2nd Floor")]
        other = dict(self.mall, short_name="N", radius_m=250)
        b.enrich([self.mall, other], overture=True)
        points = [e for e in self.read()["elements"] if e["tags"].get("level:source")]
        self.assertEqual([(e["tags"]["name"], e["tags"]["level"]) for e in points], [("PIQ", "2")])
        self.assertEqual(b.enrich([self.mall, other], overture=True), 0, "a second run changes nothing")

    def test_run_records_the_floors_of_a_gates_ways(self):
        s, w, n, e = b.fv.cell_box(1, 2)
        lat, lon = (s + n) / 2, (w + e) / 2
        gate = {"type": "node", "id": 7, "lat": lat, "lon": lon, "tags": {"aeroway": "gate", "ref": "A1"}}
        hall = {"type": "way", "id": 8, "nodes": [6, 7], "tags": {"indoor": "corridor", "level": "2"},
                "geometry": [{"lat": lat, "lon": lon - 1e-4}, {"lat": lat, "lon": lon}]}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        old_index, old_fetch = b.INDEX, b.fetch
        try:
            b.INDEX = self.dir / "_data" / "i.json"
            b.fetch = lambda row, col, not_before=None: json.dumps({"osm3s": {"timestamp_osm_base": now}, "elements": [gate, hall]}).encode()
            b.run({(1, 2): ["X"]}, b.load_index(), time.time())
        finally:
            b.INDEX, b.fetch = old_index, old_fetch
        cell = json.loads((b.OUT / "indoor_1_2.json").read_bytes())
        self.assertEqual([x["tags"].get("level:ways") for x in cell["elements"] if x["type"] == "node"], ["2"])
        self.assertTrue(all("nodes" not in x for x in cell["elements"]))


if __name__ == "__main__":
    unittest.main()
