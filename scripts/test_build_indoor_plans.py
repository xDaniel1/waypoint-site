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
        path = self.cell({"elements": [rel, {"type": "way"}]})
        self.assertTrue(b.is_fresh(path, self.now - DAY, self.now))

    def test_empty_cell_is_fresh(self):
        self.assertTrue(b.is_fresh(self.cell({"elements": []}), self.now - DAY, self.now))

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

    def test_cell_carries_its_snapshot(self):
        self.assertEqual(json.loads(b.encode([], "2026-11-22T00:00:00Z")), {"elements": [], "osm_base": "2026-11-22T00:00:00Z"})
        self.assertIsNone(b.snapshot_of(b'{"elements":[]}'))

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


if __name__ == "__main__":
    unittest.main()
