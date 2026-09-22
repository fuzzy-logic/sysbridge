import os
import tempfile
import time
import unittest

from bridge.registry import Registry
from bridge.util import hwmon_by_name, first_amdgpu_card


class RegistryTests(unittest.TestCase):
    def test_error_isolation(self):
        reg = Registry()
        calls = {"n": 0}

        @reg.probe("good", ttl_ms=10000)
        def good():
            return {"v": 1}

        @reg.probe("bad", ttl_ms=10000)
        def bad():
            calls["n"] += 1
            raise RuntimeError("boom")

        res = reg.get_many(["good", "bad", "missing"])
        self.assertTrue(res["good"]["ok"])
        self.assertEqual(res["good"]["data"], {"v": 1})
        self.assertFalse(res["bad"]["ok"])
        self.assertEqual(res["bad"]["error"]["type"], "RuntimeError")
        self.assertEqual(res["bad"]["error"]["message"], "boom")
        self.assertIsNone(res["bad"]["data"])
        self.assertFalse(res["missing"]["ok"])
        self.assertEqual(res["missing"]["error"]["type"], "UnknownProbe")

    def test_ttl_cache_and_stale(self):
        reg = Registry()
        state = {"n": 0, "fail": False}

        @reg.probe("p", ttl_ms=50)
        def p():
            state["n"] += 1
            if state["fail"]:
                raise OSError("gone")
            return state["n"]

        self.assertEqual(reg.get("p")["data"], 1)
        self.assertEqual(reg.get("p")["data"], 1)  # cached within TTL
        self.assertEqual(state["n"], 1)
        time.sleep(0.06)
        self.assertEqual(reg.get("p")["data"], 2)  # refreshed after TTL
        state["fail"] = True
        time.sleep(0.06)
        env = reg.get("p")
        self.assertFalse(env["ok"])
        self.assertTrue(env["stale"])
        self.assertEqual(env["data"], 2)  # last good value kept
        self.assertEqual(env["error"]["type"], "OSError")

    def test_async_refresh_returns_cached_immediately(self):
        reg = Registry()
        state = {"n": 0}

        @reg.probe("slow", ttl_ms=20, async_refresh=True)
        def slow():
            state["n"] += 1
            if state["n"] > 1:
                time.sleep(0.2)
            return state["n"]

        self.assertEqual(reg.get("slow")["data"], 1)  # first call is synchronous (warm-up)
        time.sleep(0.03)
        t0 = time.monotonic()
        env = reg.get("slow")
        self.assertLess(time.monotonic() - t0, 0.1)  # did not wait on the slow refresh
        self.assertEqual(env["data"], 1)
        time.sleep(0.3)
        self.assertEqual(reg.get("slow")["data"], 2)

    def test_names_validated(self):
        reg = Registry()
        with self.assertRaises(ValueError):
            reg.probe("Bad-Name")(lambda: 1)
        with self.assertRaises(ValueError):
            reg.probe("a" * 33)(lambda: 1)
        reg.probe("ok_1")(lambda: 1)
        with self.assertRaises(ValueError):
            reg.probe("ok_1")(lambda: 2)


class UtilTests(unittest.TestCase):
    def test_hwmon_by_name_on_temp_tree(self):
        with tempfile.TemporaryDirectory() as root:
            for i, name in enumerate(["acpitz", "amdgpu", "k10temp"]):
                d = os.path.join(root, f"hwmon{i}")
                os.makedirs(d)
                with open(os.path.join(d, "name"), "w") as f:
                    f.write(name + "\n")
            self.assertEqual(hwmon_by_name("amdgpu", root), os.path.join(root, "hwmon1"))
            self.assertIsNone(hwmon_by_name("nvme", root))

    def test_first_amdgpu_card_skips_connectors_and_card0(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "card0", "device"))  # a card with no amdgpu mem_info (e.g. a display-only device)
            os.makedirs(os.path.join(root, "card1-eDP-1", "device"))
            d = os.path.join(root, "card1", "device")
            os.makedirs(d)
            with open(os.path.join(d, "mem_info_vram_total"), "w") as f:
                f.write("17179869184\n")
            self.assertEqual(first_amdgpu_card(root), d)
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(first_amdgpu_card(root))


if __name__ == "__main__":
    unittest.main()
