"""Tests for flatpal.permissions using the Signal `flatpak info -m` fixture."""

import unittest
from pathlib import Path

from flatpal.permissions import (
    parse_flatpak_metadata,
    summarize_permissions,
)


FIXTURES = Path(__file__).parent / "fixtures"


def load_signal():
    return (FIXTURES / "signal.info-m.txt").read_text(encoding="utf-8")


class TestParseFlatpakMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.meta = parse_flatpak_metadata(load_signal())

    def test_application_section(self):
        self.assertEqual(self.meta["Application"]["name"], "org.signal.Signal")
        self.assertEqual(self.meta["Application"]["command"], "signal-desktop")

    def test_context_section_present(self):
        self.assertEqual(self.meta["Context"]["shared"], "network;ipc;")
        self.assertEqual(self.meta["Context"]["devices"], "all;")
        self.assertIn("wayland", self.meta["Context"]["sockets"])

    def test_session_bus_keys(self):
        sess = self.meta["Session Bus Policy"]
        self.assertEqual(sess["org.freedesktop.secrets"], "talk")
        self.assertEqual(sess["org.kde.kwalletd6"], "talk")

    def test_extension_section_loaded(self):
        ext = self.meta["Extension org.signal.Signal.Debug"]
        self.assertEqual(ext["directory"], "lib/debug")
        self.assertEqual(ext["autodelete"], "true")

    def test_environment_section(self):
        env = self.meta["Environment"]
        self.assertEqual(env["SIGNAL_DISABLE_GPU"], "0")
        self.assertEqual(env["SIGNAL_PASSWORD_STORE"], "basic")


class TestParseFlatpakMetadataEdgeCases(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_flatpak_metadata(""), {})

    def test_line_without_equals_ignored(self):
        out = parse_flatpak_metadata("[A]\njunkline\nkey=value\n")
        self.assertEqual(out, {"A": {"key": "value"}})

    def test_key_before_section_ignored(self):
        out = parse_flatpak_metadata("key=value\n[A]\nx=1\n")
        self.assertEqual(out, {"A": {"x": "1"}})

    def test_duplicate_section_merges(self):
        out = parse_flatpak_metadata("[A]\nk=1\n[A]\nl=2\n")
        self.assertEqual(out, {"A": {"k": "1", "l": "2"}})

    def test_whitespace_tolerance(self):
        out = parse_flatpak_metadata("[ A ]\n  k = v  \n")
        self.assertEqual(out["A"]["k"], "v")


class TestSummarizePermissionsSignal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        meta = parse_flatpak_metadata(load_signal())
        cls.rows = summarize_permissions(meta)
        cls.by_label = {r["label"]: r for r in cls.rows}

    def test_all_labels_present(self):
        # We expect at least these categories every time.
        for label in ("Network", "Inter-process", "Display", "Audio",
                      "Devices", "Filesystem"):
            self.assertIn(label, self.by_label, f"missing {label}")

    def test_network_granted(self):
        self.assertTrue(self.by_label["Network"]["granted"])
        self.assertEqual(self.by_label["Network"]["value"], "Full access")

    def test_devices_all(self):
        self.assertEqual(self.by_label["Devices"]["value"], "All devices")
        self.assertTrue(self.by_label["Devices"]["granted"])

    def test_display_both(self):
        # Signal exposes both wayland and x11 (fallback-x11 is suppressed because x11 is set).
        self.assertIn("Wayland", self.by_label["Display"]["value"])
        self.assertIn("X11", self.by_label["Display"]["value"])

    def test_audio_pulse(self):
        self.assertIn("PulseAudio", self.by_label["Audio"]["value"])
        self.assertTrue(self.by_label["Audio"]["granted"])

    def test_filesystem_sandbox_only(self):
        # Signal's fixture has no `filesystems=` row in [Context].
        self.assertEqual(self.by_label["Filesystem"]["value"], "Sandbox only")
        self.assertFalse(self.by_label["Filesystem"]["granted"])

    def test_dbus_row_counts(self):
        # Session has 11 keys in fixture; system has 1.
        dbus = self.by_label.get("D-Bus")
        self.assertIsNotNone(dbus)
        self.assertIn("session bus", dbus["value"])
        self.assertIn("system bus", dbus["value"])


class TestSummarizePermissionsMinimal(unittest.TestCase):
    def test_no_context_section(self):
        # An empty metadata dict still yields a stable row list.
        rows = summarize_permissions({})
        labels = [r["label"] for r in rows]
        self.assertIn("Network", labels)
        self.assertIn("Filesystem", labels)
        # Nothing granted.
        for r in rows:
            self.assertFalse(r["granted"], f"{r['label']} should be denied: {r}")

    def test_filesystem_host(self):
        meta = {"Context": {"filesystems": "host;"}}
        rows = summarize_permissions(meta)
        fs = next(r for r in rows if r["label"] == "Filesystem")
        self.assertEqual(fs["value"], "All host files")
        self.assertTrue(fs["granted"])

    def test_filesystem_home(self):
        meta = {"Context": {"filesystems": "home;"}}
        rows = summarize_permissions(meta)
        fs = next(r for r in rows if r["label"] == "Filesystem")
        self.assertEqual(fs["value"], "Home directory")
        self.assertTrue(fs["granted"])


if __name__ == "__main__":
    unittest.main()
