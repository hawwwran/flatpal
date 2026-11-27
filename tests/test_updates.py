"""Tests for the update-discovery parser + release-diff helper.

The subprocess shell-out itself is impure (and slow — ~2.5 s) so the
parser is tested against canned output fixtures.
"""

import threading
import time
import unittest
from unittest import mock

from flatpal.updates import (
    _fetch, _parse, _refresh, _run_refresh, releases_since, update_origins,
)


SAMPLE_TWO_APPS = (
    "com.discordapp.Discord\t0.0.103\tstable\tflathub\tabcdef1234567890\n"
    "org.mozilla.firefox\t142.0\tstable\tflathub\tfedcba0987654321\n"
)


class TestParse(unittest.TestCase):
    def test_returns_dict_keyed_by_app_id(self):
        out = _parse(SAMPLE_TWO_APPS)
        self.assertEqual(set(out.keys()), {"com.discordapp.Discord", "org.mozilla.firefox"})

    def test_carries_version_branch_origin_commit(self):
        out = _parse(SAMPLE_TWO_APPS)
        firefox = out["org.mozilla.firefox"]
        self.assertEqual(firefox["version"], "142.0")
        self.assertEqual(firefox["branch"], "stable")
        self.assertEqual(firefox["origin"], "flathub")
        self.assertEqual(firefox["commit"], "fedcba0987654321")

    def test_blank_lines_ignored(self):
        text = "\n\ncom.example.X\t1.0\tstable\tflathub\tcommit1\n\n"
        out = _parse(text)
        self.assertEqual(list(out.keys()), ["com.example.X"])

    def test_header_row_ignored(self):
        text = "Application\tVersion\tBranch\tOrigin\tCommit\n" + SAMPLE_TWO_APPS
        out = _parse(text)
        self.assertNotIn("Application", out)
        self.assertEqual(len(out), 2)

    def test_short_rows_padded_without_crash(self):
        # Defensive: a future flatpak version might drop a column.
        text = "com.example.X\t1.0\tstable\n"
        out = _parse(text)
        self.assertEqual(out["com.example.X"]["version"], "1.0")
        self.assertEqual(out["com.example.X"]["origin"], "")
        self.assertEqual(out["com.example.X"]["commit"], "")

    def test_duplicate_app_id_keeps_first(self):
        # System + user install of the same app both updateable → same
        # version/origin from the same remote; first wins.
        text = (
            "com.example.X\t1.0\tstable\tflathub\tcommit1\n"
            "com.example.X\t1.0\tstable\tflathub\tcommit1\n"
        )
        out = _parse(text)
        self.assertEqual(len(out), 1)

    def test_empty_text_returns_empty_dict(self):
        self.assertEqual(_parse(""), {})


class TestFetch(unittest.TestCase):
    def test_runner_returning_none_yields_empty_dict(self):
        # `_run` returns None on subprocess failure; `_fetch` must not crash.
        self.assertEqual(_fetch(lambda: None), {})

    def test_runner_returning_text_is_parsed(self):
        out = _fetch(lambda: SAMPLE_TWO_APPS)
        self.assertEqual(len(out), 2)


class TestReleasesSince(unittest.TestCase):
    def _rel(self, version: str, description: str = ""):
        return {"version": version, "date": "", "description_markup": description}

    def test_empty_releases_returns_empty(self):
        self.assertEqual(releases_since([], "1.0"), [])

    def test_blank_installed_version_returns_all(self):
        rels = [self._rel("2.0"), self._rel("1.5"), self._rel("1.0")]
        self.assertEqual(releases_since(rels, ""), rels)

    def test_stops_at_matching_installed_version(self):
        rels = [self._rel("2.0"), self._rel("1.5"), self._rel("1.0"), self._rel("0.9")]
        out = releases_since(rels, "1.5")
        self.assertEqual([r["version"] for r in out], ["2.0"])

    def test_returns_all_when_installed_version_not_in_releases(self):
        rels = [self._rel("2.0"), self._rel("1.0")]
        # Installed 1.5 is between, but not in releases → no stop, return all.
        out = releases_since(rels, "1.5")
        self.assertEqual([r["version"] for r in out], ["2.0", "1.0"])

    def test_v_prefix_tolerated_on_both_sides(self):
        rels = [self._rel("v2.0"), self._rel("v1.5"), self._rel("v1.0")]
        out = releases_since(rels, "1.5")
        self.assertEqual([r["version"] for r in out], ["v2.0"])


class CatalogRefreshTests(unittest.TestCase):
    """The version column comes from the local catalog copy, so the remotes with
    updates get their catalogs refreshed before the list is read again."""

    def test_origins_skip_blank_ones(self):
        found = _parse("a.App\t1\tstable\thwnhub\tc\nb.App\t2\tstable\t\tc\n")
        self.assertEqual(update_origins(found), {"hwnhub"})

    def test_refreshes_each_remote_once(self):
        ran = []
        out = _refresh(["hwnhub", "flathub", "hwnhub"], ran.append, deadline_s=5)
        self.assertEqual(sorted(ran), ["flathub", "hwnhub"])
        self.assertEqual(out, {"flathub", "hwnhub"})

    def test_none_refreshes_every_remote(self):
        ran = []
        self.assertIsNone(_refresh(None, ran.append, deadline_s=5))
        self.assertEqual(ran, [None])

    def test_a_slow_refresh_is_left_behind_at_the_deadline(self):
        gate = threading.Event()
        started = time.monotonic()
        _refresh(["slow"], lambda _remote: gate.wait(5), deadline_s=0.1)
        self.assertLess(time.monotonic() - started, 2)
        gate.set()

    def test_refresh_command_names_the_remote(self):
        with mock.patch("flatpal.updates.subprocess.run") as run, \
                mock.patch("flatpal.updates.host_cmd", side_effect=lambda cmd, **_: cmd):
            _run_refresh("hwnhub")
            _run_refresh(None)
        argv = [c.args[0] for c in run.call_args_list]
        refresh = ["flatpak", "update", "--appstream", "--noninteractive"]
        self.assertEqual(argv[0], refresh + ["hwnhub"])
        self.assertEqual(argv[1], refresh)

    def test_refresh_failures_are_swallowed(self):
        with mock.patch("flatpal.updates.subprocess.run", side_effect=PermissionError):
            _run_refresh("hwnhub")


if __name__ == "__main__":
    unittest.main()
