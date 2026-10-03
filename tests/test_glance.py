import gzip
import importlib.util
import os
import pathlib
import struct
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("glance", pathlib.Path(__file__).parents[1] / "glance.py")
glance = importlib.util.module_from_spec(spec)
sys.modules["glance"] = glance
spec.loader.exec_module(glance)


def healthy_data(**overrides):
    data = glance.demo_snapshot(t=0).data
    data["services"] = {"watched": [], "failed": [], "ssh": True}
    data["disks"] = [{"mount": "/", "pct": 40, "avail_gb": 100}]
    data.update(overrides)
    return data


class EvaluateTests(unittest.TestCase):
    def test_healthy_machine_has_no_issues(self):
        self.assertEqual(glance.evaluate(healthy_data()), [])

    def test_unplugged_charger_is_reported(self):
        power = {"battery": {"capacity": 50, "status": "Discharging", "eta": 3600, "health": 90, "watts": 9}, "ac": False}
        issues = glance.evaluate(healthy_data(power=power))
        self.assertTrue(any("battery power" in i.text for i in issues))

    def test_low_battery_is_critical(self):
        power = {"battery": {"capacity": 9, "status": "Discharging", "eta": 600, "health": 90, "watts": 9}, "ac": False}
        self.assertIn("crit", {i.level for i in glance.evaluate(healthy_data(power=power))})

    def test_hot_cpu(self):
        self.assertEqual(glance.evaluate(healthy_data(temps=[("CPU", 93.0)]))[0].level, "crit")

    def test_disabled_service_is_not_an_issue(self):
        services = {"watched": [("old-app", "user", "disabled")], "failed": [], "ssh": True}
        self.assertEqual(glance.evaluate(healthy_data(services=services)), [])

    def test_runners_down_are_grouped_into_one_warning(self):
        runners = [{"unit": f"u{i}", "repo": "r", "name": f"n{i}", "state": "down", "since": None, "reason": "oom-kill"}
                   for i in range(3)]
        issues = glance.evaluate(healthy_data(runners=runners))
        self.assertEqual([i.text for i in issues], ["3 GitHub runners down (oom-kill)"])

    def test_slow_ethernet_without_ip(self):
        lan = healthy_data()["lan"]
        lan["nics"][0].update(speed=10, ip=None)
        texts = " ".join(i.text for i in glance.evaluate(healthy_data(lan=lan)))
        self.assertIn("10 Mb/s", texts)
        self.assertIn("no IP address", texts)

    def test_unreachable_gateway_is_critical(self):
        lan = dict(healthy_data()["lan"], gw_ms=None)
        self.assertEqual(glance.evaluate(healthy_data(lan=lan))[0].level, "crit")

    def test_criticals_sort_first(self):
        data = healthy_data(temps=[("CPU", 82.0)], internet=dict(healthy_data()["internet"], state="offline"))
        self.assertEqual([i.level for i in glance.evaluate(data)], ["crit", "warn"])


class DisplayWatcherTests(unittest.TestCase):
    def test_lid_open_and_new_monitor_trigger(self):
        states = iter([
            (False, frozenset({"card0-eDP-1"})),
            (True, frozenset({"card0-eDP-1"})),                    # lid opened
            (True, frozenset({"card0-eDP-1"})),
            (True, frozenset({"card0-eDP-1", "card0-HDMI-A-1"})),  # monitor plugged in
            (True, frozenset({"card0-eDP-1"})),                    # unplugged
        ])
        original = glance.DisplayWatcher._state
        glance.DisplayWatcher._state = staticmethod(lambda: next(states))
        try:
            watcher = glance.DisplayWatcher()
            self.assertEqual([watcher.triggered() for _ in range(4)], [True, False, True, False])
        finally:
            glance.DisplayWatcher._state = original


class RenderTests(unittest.TestCase):
    def check_frame(self, glyphs, colors, width, height):
        glance.T = glance.Term(glyphs, colors)
        lines = glance.render_lines(glance.demo_snapshot(t=0), width, height, vt=8)
        self.assertEqual(len(lines), height)
        for line in lines:
            self.assertEqual(glance.vlen(line), width, repr(line))

    def test_every_mode_fills_the_screen_exactly(self):
        for glyphs in ("console", "unicode", "basic"):
            for colors in ("palette", "truecolor", "ansi"):
                for width, height in ((137, 38), (100, 30), (80, 24)):
                    with self.subTest(glyphs=glyphs, colors=colors, size=(width, height)):
                        self.check_frame(glyphs, colors, width, height)

    def test_tiny_terminal_does_not_crash(self):
        glance.T = glance.Term()
        glance.render_lines(glance.demo_snapshot(t=0), 40, 10)

    def test_fit_handles_wide_characters(self):
        self.assertEqual(glance.vlen(glance.fit("🔋🔋🔋", 5)), 5)


class FontTests(unittest.TestCase):
    def make_font(self, path):
        """A tiny 8x16 PSF2 font: ASCII plus Latin-1 letters we can repurpose."""
        chars = [chr(c) for c in range(32, 127)] + [chr(c) for c in range(0xC0, 0x100)]
        n, h, w = len(chars), 16, 8
        header = struct.pack("<8I", 0x864AB572, 0, 32, 1, n, h, h, w)
        table = b"".join(c.encode() + b"\xff" for c in chars)
        with gzip.open(path, "wb") as f:
            f.write(header + bytes(n * h) + table)

    def test_build_font_adds_blocks_and_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "in.psf.gz"), os.path.join(tmp, "out.psf")
            self.make_font(src)
            added = glance.build_font(src, dst)
            self.assertEqual(added, len(glance.CUSTOM_CHARS))
            raw = pathlib.Path(dst).read_bytes()
            self.assertEqual(raw[:4], b"\x72\xb5\x4a\x86")
            _, _, hs, flags, n, cs, h, w = struct.unpack("<8I", raw[:32])
            table = "".join(e.decode("utf-8") for e in raw[hs + n * cs:].split(b"\xff"))
            for ch in glance.CUSTOM_CHARS:
                self.assertIn(ch, table)
            self.assertIn("A", table)  # ASCII is never touched


if __name__ == "__main__":
    unittest.main()
