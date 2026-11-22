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


if __name__ == "__main__":
    unittest.main()
