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
        path = self.cell({"elements": [{"type": "relation", "members": []}, {"type": "way"}]})
        self.assertTrue(b.is_fresh(path, self.now - DAY, self.now))

    def test_empty_cell_is_fresh(self):
        self.assertTrue(b.is_fresh(self.cell({"elements": []}), self.now - DAY, self.now))

    def test_old_or_unknown_fetch_time_is_stale(self):
        path = self.cell({"elements": []})
        self.assertFalse(b.is_fresh(path, self.now - b.MAX_AGE_DAYS * DAY, self.now))
        self.assertFalse(b.is_fresh(path, None, self.now))

    def test_cell_from_old_query_is_stale(self):
        # `out tags geom` left relations without members, so no outlines.
        path = self.cell({"elements": [{"type": "relation", "tags": {"aeroway": "terminal"}}]})
        self.assertFalse(b.is_fresh(path, self.now - DAY, self.now))

    def test_missing_or_broken_file_is_stale(self):
        self.assertFalse(b.is_fresh(self.dir / "nope.json", self.now, self.now))
        self.assertFalse(b.is_fresh(self.cell("not json"), self.now, self.now))
        self.assertFalse(b.is_fresh(self.cell([1]), self.now, self.now))


class Answers(unittest.TestCase):
    def test_whole_answer_is_accepted(self):
        self.assertEqual(b.check_answer(b'{"elements":[{"type":"way"}]}'), [{"type": "way"}])

    def test_partial_answer_is_rejected(self):
        with self.assertRaises(ValueError):
            b.check_answer(b'{"elements":[],"remark":"runtime error: Query timed out"}')

    def test_answer_without_elements_is_rejected(self):
        for body in (b'{}', b'[]', b'{"elements":null}'):
            with self.assertRaises(ValueError):
                b.check_answer(body)

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

    def test_many_failures_turn_the_run_red(self):
        self.assertEqual(b.outcome(written=500, failed=3), 0)
        self.assertEqual(b.outcome(written=500, failed=40), 0)
        self.assertEqual(b.outcome(written=500, failed=60), 1)
        self.assertEqual(b.outcome(written=1, failed=300), 1)
        self.assertEqual(b.outcome(written=0, failed=0), 0)


class Files(unittest.TestCase):
    def test_atomic_write_replaces_and_leaves_no_temp(self):
        d = pathlib.Path(tempfile.mkdtemp())
        path = d / "indoor_1_2.json"
        path.write_text("old")
        b.write_atomic(path, b'{"elements":[]}')
        self.assertEqual(path.read_text(), '{"elements":[]}')
        self.assertEqual([p.name for p in d.iterdir()], ["indoor_1_2.json"])

    def test_index_round_trips_and_ignores_junk(self):
        d = pathlib.Path(tempfile.mkdtemp())
        old = b.INDEX
        try:
            b.INDEX = d / "_data" / "indoor_fetched.json"
            self.assertEqual(b.load_index(), {"fetched": {}, "tried": {}})
            b.save_index({"fetched": {"1_2": 5}, "tried": {"1_2": 6}})
            self.assertEqual(b.load_index(), {"fetched": {"1_2": 5}, "tried": {"1_2": 6}})
            b.INDEX.write_text('{"fetched": {"a": "x", "b": 1}, "tried": []}')
            self.assertEqual(b.load_index(), {"fetched": {"b": 1}, "tried": {}})
        finally:
            b.INDEX = old


if __name__ == "__main__":
    unittest.main()
