import time
import unittest

from bridge import top


class TopTests(unittest.TestCase):
    def test_parse_stat_line_with_spaces_and_parens_in_comm(self):
        line = "1234 (Web Content (x)) S 1 1234 1234 0 -1 4194304 95 0 0 0 250 50 0 0 20 0 7 0 18197168 6152192 1015 1 2 3"
        p = top.parse_stat_line(line)
        self.assertEqual(p["comm"], "Web Content (x)")
        self.assertEqual((p["state"], p["ppid"], p["utime"], p["stime"], p["threads"], p["starttime"], p["rss_pages"]), ("S", 1, 250, 50, 7, 18197168, 1015))
        self.assertIsNone(top.parse_stat_line("garbage"))

    def test_parse_stat_cpus(self):
        txt = "cpu  10 0 5 100 3 0 0 0 0 0\ncpu0 1 0 1 10 0 0 0 0 0 0\ncpu1 2 0 2 20 0 0 0 0 0 0\nintr 5\n"
        cores = top.parse_stat_cpus(txt)
        self.assertEqual(len(cores), 2)
        self.assertEqual(cores[1], {"idle": 20, "total": 24})

    def test_sample_live(self):
        a = top.sample()
        self.assertGreater(a["tasks"]["total"], 1)
        self.assertEqual(len(a["cpu"]["cores"]), a["cpu"]["nproc"])
        self.assertTrue(all(p["cpu"] == 0.0 for p in a["processes"]))     # first sample: no delta yet
        self.assertGreater(a["uptime_s"], 0)
        self.assertIn("pid", a["processes"][0])
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.3:                                   # burn a little CPU so someone is non-zero
            pass
        time.sleep(0.2)
        b = top.sample()
        self.assertGreater(b["interval_s"], 0.4)
        me = [p for p in b["processes"] if p["pid"] == __import__("os").getpid()]
        self.assertTrue(me and me[0]["cpu"] > 5, me)
        self.assertEqual(b["processes"], sorted(b["processes"], key=lambda p: (-p["cpu"], -p["rss"])))
        self.assertLessEqual(len(b["processes"]), top.TOP_N)
        self.assertTrue(all(0 <= p["mem"] <= 100 for p in b["processes"]))


if __name__ == "__main__":
    unittest.main()
