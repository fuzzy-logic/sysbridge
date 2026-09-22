import os
import tempfile
import unittest

from bridge.settings import Settings, SettingsError


class SettingsTests(unittest.TestCase):
    def test_defaults_whitelist_persist(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state", "settings.json")
            s = Settings(p, defaults={"home_position": "left", "bogus": "x"})
            self.assertEqual(s.all(), {"home_position": "left"})
            self.assertEqual(s.update({"home_position": "right"}), {"home_position": "right"})
            self.assertTrue(os.path.isfile(p))
            self.assertEqual(Settings(p).get("home_position"), "right")            # file wins over defaults
            for bad in [{}, {"home_position": "middle"}, {"nope": "top"}, "top", {"home_position": 1}]:
                with self.assertRaises(SettingsError):
                    s.update(bad)
            with open(p, "w") as f:
                f.write('{"home_position": "diagonal", "x": 1}')
            self.assertEqual(Settings(p).get("home_position"), "top")              # garbage in the file is ignored
            with open(p, "w") as f:
                f.write("not json")
            self.assertEqual(Settings(p, defaults={"home_position": "bottom"}).get("home_position"), "bottom")


if __name__ == "__main__":
    unittest.main()
