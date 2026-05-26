"""Tests for the remote-log fetch and parse helpers.

Pure-subprocess module: tests inject a fake runner via the `_run` /
`_run_show_commit` callable parameter for argv assertions, and feed
canned text into `parse_log` for the pure-logic surface.
"""

import subprocess
import unittest
from types import SimpleNamespace

from flatpal.remote_log import _run, _run_mask_list, _run_show_commit, parse_log


def _ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _fail(returncode: int = 1, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


SAMPLE_LOG = """\
        Ref: app/org.example.App/x86_64/stable
         ID: org.example.App
       Arch: x86_64
     Branch: stable

     Commit: 83ef900d1ed3f3ff5e1f6c0b4d8a2c7e9b1d5e3f4a6c8e0b2d4f6a8c0e2b4d6
     Parent: 5e09c43a2b65723f9c1d3e5b7a9c1e3f5a7b9d1e3c5f7a9b1d3e5c7f9a1b3d5
    Subject: Update org.example.App to 1.2.3
       Date: 2026-03-18 19:39:58 +0000
    History:

     Commit: 5e09c43a2b65723f9c1d3e5b7a9c1e3f5a7b9d1e3c5f7a9b1d3e5c7f9a1b3d5
     Parent: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    Subject: 1.2.2
       Date: 2026-02-10 14:15:00 +0000

     Commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    Subject: Sync from upstream
       Date: 2026-01-05 09:00:00 +0000
"""


class TestParseLog(unittest.TestCase):
    def test_basic(self):
        records = parse_log(SAMPLE_LOG)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["commit"][:8], "83ef900d")
        self.assertEqual(records[0]["subject"], "Update org.example.App to 1.2.3")
        self.assertEqual(records[0]["date_short"], "2026-03-18")
        self.assertEqual(records[1]["subject"], "1.2.2")
        self.assertEqual(records[2]["parent"], "")

    def test_skips_header(self):
        records = parse_log(SAMPLE_LOG)
        self.assertEqual(records[0]["commit"][:8], "83ef900d")
        self.assertNotIn("org.example.App/x86_64", records[0]["commit"])

    def test_empty(self):
        self.assertEqual(parse_log(""), [])
        self.assertEqual(parse_log("\n\n"), [])

    def test_no_commit_lines(self):
        text = "        Ref: foo\n         ID: bar\n"
        self.assertEqual(parse_log(text), [])

    def test_tolerates_missing_parent(self):
        text = "     Commit: abc123\n    Subject: lonely\n       Date: 2026-01-01\n"
        records = parse_log(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["parent"], "")


class TestRun(unittest.TestCase):
    def test_argv_user_scope(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok("…")

        _run(runner, "org.example.App", "user", "flathub")
        self.assertIn("--user", seen["argv"])
        self.assertNotIn("--system", seen["argv"])
        self.assertIn("--log", seen["argv"])
        self.assertIn("flathub", seen["argv"])
        self.assertIn("org.example.App", seen["argv"])

    def test_argv_system_scope(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok("…")

        _run(runner, "org.example.App", "system", "flathub")
        self.assertIn("--system", seen["argv"])
        self.assertNotIn("--user", seen["argv"])

    def test_unknown_scope_defaults_to_system(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok("…")

        _run(runner, "org.example.App", "", "flathub")
        self.assertIn("--system", seen["argv"])

    def test_happy_path_returns_stdout(self):
        out = _run(lambda argv: _ok("hello"), "org.example.App", "system", "flathub")
        self.assertEqual(out, "hello")

    def test_nonzero_returns_none(self):
        out = _run(lambda argv: _fail(1, stderr="boom"), "org.example.App", "system", "flathub")
        self.assertIsNone(out)

    def test_file_not_found_returns_none(self):
        def runner(argv):
            raise FileNotFoundError("flatpak-spawn not found")

        self.assertIsNone(_run(runner, "org.example.App", "system", "flathub"))

    def test_timeout_returns_none(self):
        def runner(argv):
            raise subprocess.TimeoutExpired(cmd="flatpak", timeout=30)

        self.assertIsNone(_run(runner, "org.example.App", "system", "flathub"))

    def test_empty_remote_returns_none(self):
        called = []

        def runner(argv):
            called.append(argv)
            return _ok("")

        self.assertIsNone(_run(runner, "org.example.App", "system", ""))
        self.assertEqual(called, [])

    def test_empty_app_id_returns_none(self):
        called = []

        def runner(argv):
            called.append(argv)
            return _ok("")

        self.assertIsNone(_run(runner, "", "system", "flathub"))
        self.assertEqual(called, [])


class TestShowCommit(unittest.TestCase):
    def test_argv_carries_show_commit_and_scope(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok("abc123\n")

        out = _run_show_commit(runner, "org.example.App", "user")
        self.assertEqual(out, "abc123")
        self.assertIn("--user", seen["argv"])
        self.assertIn("--show-commit", seen["argv"])
        self.assertIn("org.example.App", seen["argv"])

    def test_system_scope(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok("deadbeef\n")

        _run_show_commit(runner, "org.example.App", "system")
        self.assertIn("--system", seen["argv"])
        self.assertNotIn("--user", seen["argv"])

    def test_unknown_scope_defaults_to_system(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok("x\n")

        _run_show_commit(runner, "org.example.App", "")
        self.assertIn("--system", seen["argv"])

    def test_strips_whitespace(self):
        out = _run_show_commit(lambda argv: _ok("  hash123  \n"), "org.example.App", "user")
        self.assertEqual(out, "hash123")

    def test_empty_output_returns_none(self):
        self.assertIsNone(_run_show_commit(lambda argv: _ok(""), "org.example.App", "user"))
        self.assertIsNone(_run_show_commit(lambda argv: _ok("\n"), "org.example.App", "user"))

    def test_nonzero_returns_none(self):
        self.assertIsNone(_run_show_commit(lambda argv: _fail(1), "org.example.App", "user"))

    def test_file_not_found_returns_none(self):
        def runner(argv):
            raise FileNotFoundError()
        self.assertIsNone(_run_show_commit(runner, "org.example.App", "user"))

    def test_timeout_returns_none(self):
        def runner(argv):
            raise subprocess.TimeoutExpired(cmd="flatpak", timeout=5)
        self.assertIsNone(_run_show_commit(runner, "org.example.App", "user"))

    def test_empty_app_id_returns_none(self):
        called = []

        def runner(argv):
            called.append(argv)
            return _ok("x")

        self.assertIsNone(_run_show_commit(runner, "", "user"))
        self.assertEqual(called, [])


class TestMaskList(unittest.TestCase):
    def test_argv_user_scope(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok("")

        _run_mask_list(runner, "org.example.App", "user")
        self.assertIn("--user", seen["argv"])
        self.assertNotIn("--system", seen["argv"])
        self.assertIn("mask", seen["argv"])
        self.assertNotIn("--remove", seen["argv"])

    def test_argv_system_scope(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok("")

        _run_mask_list(runner, "org.example.App", "system")
        self.assertIn("--system", seen["argv"])

    def test_match_when_app_listed(self):
        out = "com.discordapp.Discord\norg.example.App\n"
        self.assertTrue(_run_mask_list(lambda argv: _ok(out), "org.example.App", "user"))

    def test_substring_match_within_ref(self):
        out = "app/org.example.App/x86_64/stable\n"
        self.assertTrue(_run_mask_list(lambda argv: _ok(out), "org.example.App", "system"))

    def test_no_match(self):
        out = "com.other.App\n"
        self.assertFalse(_run_mask_list(lambda argv: _ok(out), "org.example.App", "user"))

    def test_empty_output_false(self):
        self.assertFalse(_run_mask_list(lambda argv: _ok(""), "org.example.App", "user"))

    def test_nonzero_returns_false(self):
        self.assertFalse(_run_mask_list(lambda argv: _fail(1), "org.example.App", "user"))

    def test_file_not_found_returns_false(self):
        def runner(argv):
            raise FileNotFoundError()
        self.assertFalse(_run_mask_list(runner, "org.example.App", "user"))

    def test_timeout_returns_false(self):
        def runner(argv):
            raise subprocess.TimeoutExpired(cmd="flatpak", timeout=5)
        self.assertFalse(_run_mask_list(runner, "org.example.App", "user"))

    def test_empty_app_id_returns_false(self):
        called = []

        def runner(argv):
            called.append(argv)
            return _ok("anything")

        self.assertFalse(_run_mask_list(runner, "", "user"))
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
