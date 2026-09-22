import os
import unittest

from bridge import parsers

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class DfTests(unittest.TestCase):
    def test_btrfs_subvolumes_dedupe_to_one_row(self):
        rows = parsers.parse_df(fixture("df-btrfs.txt"))
        btrfs = [r for r in rows if r["fstype"] == "btrfs"]
        self.assertEqual(len(btrfs), 1)
        self.assertEqual(btrfs[0]["target"], "/")
        self.assertEqual(btrfs[0]["size"], 1151268290560)
        self.assertGreater(btrfs[0]["percent"], 60)

    def test_efivarfs_dropped_boot_kept(self):
        rows = parsers.parse_df(fixture("df-btrfs.txt"))
        self.assertNotIn("efivarfs", [r["fstype"] for r in rows])
        self.assertIn("/boot", [r["target"] for r in rows])

    def test_empty(self):
        self.assertEqual(parsers.parse_df(""), [])


class RocmTests(unittest.TestCase):
    def test_table_with_warning(self):
        out = parsers.parse_rocm_showpids(fixture("rocm-showpids.txt"))
        self.assertEqual(len(out["warnings"]), 1)
        self.assertTrue(out["warnings"][0].startswith("WARNING"))
        self.assertEqual(len(out["processes"]), 3)
        big = max(out["processes"], key=lambda p: p["vram_used"])
        self.assertEqual(big["pid"], 1845878)
        self.assertEqual(big["name"], "llama-server")
        self.assertEqual(big["vram_used"], 26990665728)
        self.assertIsNone(big["cu_occupancy"])

    def test_no_processes(self):
        txt = ("============================ ROCm System Management Interface ============================\n"
               "===================================== KFD Processes ======================================\n"
               "No KFD PIDs currently running\n"
               "==========================================================================================\n")
        out = parsers.parse_rocm_showpids(txt)
        self.assertEqual(out["processes"], [])
        self.assertEqual(out["warnings"], [])

    def test_process_name_with_space(self):
        txt = ("PID    \tPROCESS NAME\tGPU(s)\tVRAM USED  \tSDMA USED\tCU OCCUPANCY\t\n"
               "42\tWeb Content\t0     \t1024       \t0        \tUNKNOWN     \t\n")
        out = parsers.parse_rocm_showpids(txt)
        self.assertEqual(out["processes"][0]["name"], "Web Content")
        self.assertEqual(out["processes"][0]["vram_used"], 1024)


class MeminfoTests(unittest.TestCase):
    def test_values_in_bytes(self):
        mi = parsers.parse_meminfo(fixture("meminfo.txt"))
        self.assertIn("MemTotal", mi)
        self.assertEqual(mi["MemTotal"] % 1024, 0)
        self.assertGreater(mi["MemTotal"], 100 * 1024 ** 3)
        self.assertIn("HugePages_Total", mi)  # no kB suffix, must still parse


class SsTests(unittest.TestCase):
    def test_listeners(self):
        rows = parsers.parse_ss_tlnp(fixture("ss-tlnp.txt"))
        by_port = {r["port"]: r for r in rows}
        self.assertEqual(by_port[8080]["process"], "llama-server")
        self.assertEqual(by_port[8080]["pid"], 1845787)
        self.assertEqual(by_port[8080]["addr"], "127.0.0.1")
        self.assertIsNone(by_port[631]["process"])  # not our user: no process shown
        self.assertEqual(rows, sorted(rows, key=lambda r: (r["port"], r["addr"])))


class SmallParsers(unittest.TestCase):
    def test_ps(self):
        txt = "    PID COMMAND         %CPU   RSS\n2019568 chrome          41.8 700888\n  4553 Web Content      3.2 343440\n"
        rows = parsers.parse_ps(txt)
        self.assertEqual(rows[0], {"pid": 2019568, "comm": "chrome", "pcpu": 41.8, "rss": 700888 * 1024})
        self.assertEqual(rows[1]["comm"], "Web Content")

    def test_loadavg_and_stat(self):
        la = parsers.parse_loadavg("2.10 2.65 2.49 1/4572 2073416")
        self.assertEqual(la, {"load": [2.10, 2.65, 2.49], "running": 1, "total": 4572})
        st = parsers.parse_stat_cpu("cpu  10 0 5 100 3 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n")
        self.assertEqual(st, {"idle": 103, "total": 118})

    def test_fdinfo(self):
        d = parsers.parse_fdinfo_drm("drm-driver:\tamdgpu\ndrm-memory-vram:\t71296 KiB\ndrm-memory-gtt: \t26366340 KiB\n")
        self.assertEqual(d["drm-memory-gtt"], 26366340)
        self.assertEqual(d["drm-memory-vram"], 71296)

    def test_pp_dpm(self):
        d = parsers.parse_pp_dpm("0: 600Mhz \n1: 1100Mhz \n2: 2900Mhz *\n")
        self.assertEqual(d, {"levels_mhz": [600, 1100, 2900], "active_mhz": 2900})

    def test_xrt_examine(self):
        txt = ("System Configuration\n  OS Name              : Linux\n\nXRT\n  Version              : 2.21.75\n"
               "  amdxdna Version      : 7.1.9-arch1-2\n  NPU Firmware Version : 1.1.2.65\n\n"
               "Device(s) Present\n|BDF             |Name          |\n|----------------|--------------|\n|[0000:c4:00.1]  |RyzenAI-npu5  |\n")
        d = parsers.parse_xrt_examine(txt)
        self.assertEqual(d["xrt_version"], "2.21.75")
        self.assertEqual(d["npu_firmware"], "1.1.2.65")
        self.assertEqual(d["devices"], [{"bdf": "0000:c4:00.1", "name": "RyzenAI-npu5"}])

    def test_ini_preset(self):
        d = parsers.parse_ini_preset("[x]\nctx-size = 131072\nmodel = /models/a b.gguf\nload-on-startup = false\n\n")
        self.assertEqual(d["ctx-size"], "131072")
        self.assertEqual(d["model"], "/models/a b.gguf")


if __name__ == "__main__":
    unittest.main()
