import importlib.util
import pathlib
import sys
import unittest

spec = importlib.util.spec_from_file_location("glance", pathlib.Path(__file__).parents[1] / "glance.py")
glance = importlib.util.module_from_spec(spec)
sys.modules["glance"] = glance
spec.loader.exec_module(glance)


def base_data(**overrides):
    data = glance.demo_snapshot().data
    data["services"] = {"watched": [], "failed": [], "ssh": True}
    data["system"]["disks"] = [{"mount": "/", "pct": 40, "avail_gb": 100}]
    data.update(overrides)
    return data


class EvaluateTests(unittest.TestCase):
    def test_healthy_machine_has_no_issues(self):
        self.assertEqual(glance.evaluate(base_data()), [])

    def test_unplugged_charger_is_reported(self):
        battery = dict(base_data()["battery"], status="Discharging", capacity=50)
        issues = glance.evaluate(base_data(battery=battery, ac=False))
        self.assertTrue(any("battery power" in i.text for i in issues))

    def test_low_battery_is_critical(self):
        battery = dict(base_data()["battery"], status="Discharging", capacity=9)
        levels = {i.level for i in glance.evaluate(base_data(battery=battery, ac=False))}
        self.assertIn("crit", levels)

    def test_hot_cpu(self):
        issues = glance.evaluate(base_data(temps=[("CPU", 93.0)]))
        self.assertEqual(issues[0].level, "crit")

    def test_disabled_service_is_not_an_issue(self):
        services = {"watched": [("old-app", "user", "disabled")], "failed": [], "ssh": True}
        self.assertEqual(glance.evaluate(base_data(services=services)), [])

    def test_criticals_sort_first(self):
        data = base_data(temps=[("CPU", 82.0)], network=dict(base_data()["network"], internet="offline"))
        issues = glance.evaluate(data)
        self.assertEqual([i.level for i in issues], ["crit", "warn"])


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
    def test_render_fits_terminal(self):
        frame = glance.render(glance.demo_snapshot(), 100, 30, vt=8)
        lines = frame.removeprefix("\x1b[H").removesuffix("\x1b[J").split("\n")
        self.assertEqual(len(lines), 30)
        self.assertTrue(all(glance.vlen(line) == 100 for line in lines))

    def test_small_terminal_does_not_crash(self):
        glance.render(glance.demo_snapshot(), 40, 10)


if __name__ == "__main__":
    unittest.main()
