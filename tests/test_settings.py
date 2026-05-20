"""Tests for the small JSON-backed settings store."""

import json
import tempfile
import unittest
from pathlib import Path

from flatpal import settings


class TestSettings(unittest.TestCase):
    def _tmp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name) / "settings.json"

    def test_missing_file_returns_defaults(self):
        out = settings.load(path=Path("/nonexistent/never.json"))
        self.assertEqual(out, settings.DEFAULTS)
        # Defaults should be a copy, not the module-level dict.
        out["last_tab"] = "running"
        self.assertEqual(settings.DEFAULTS["last_tab"], "installed")

    def test_round_trip(self):
        p = self._tmp()
        settings.save({"last_tab": "running",
                       "installed_sort_key": "name",
                       "installed_reverse": False}, path=p)
        loaded = settings.load(path=p)
        self.assertEqual(loaded["last_tab"], "running")
        self.assertEqual(loaded["installed_sort_key"], "name")
        self.assertFalse(loaded["installed_reverse"])
        # Missing keys still get defaults.
        self.assertEqual(loaded["running_refresh_seconds"], 2)

    def test_corrupt_file_falls_back_to_defaults(self):
        p = self._tmp()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not-json")
        self.assertEqual(settings.load(path=p), settings.DEFAULTS)

    def test_non_dict_payload_ignored(self):
        p = self._tmp()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([1, 2, 3]))
        self.assertEqual(settings.load(path=p), settings.DEFAULTS)

    def test_save_creates_parent_dir(self):
        p = self._tmp().with_name("nested") / "deep" / "settings.json"
        settings.save({"last_tab": "explore"}, path=p)
        self.assertTrue(p.is_file())
        self.assertEqual(json.loads(p.read_text())["last_tab"], "explore")

    def test_write_failure_does_not_raise(self):
        # Unwritable path under /proc → save() swallows the OSError silently.
        unwritable = Path("/proc/1/no-write-here/settings.json")
        settings.save({"a": 1}, path=unwritable)

    def test_unknown_keys_preserved(self):
        p = self._tmp()
        # Older / future settings keys should be preserved across save/load.
        settings.save({"experimental_thing": True, "last_tab": "running"}, path=p)
        loaded = settings.load(path=p)
        self.assertTrue(loaded["experimental_thing"])
        self.assertEqual(loaded["last_tab"], "running")


if __name__ == "__main__":
    unittest.main()
