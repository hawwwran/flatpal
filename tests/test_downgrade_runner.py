"""Tests for the per-app downgrade runner.

The downgrade is `flatpak update --commit=<hash>`; the optional second
step is `flatpak mask`. Tests cover the four-state return shape and the
argv passed to each invocation.
"""

import subprocess
import unittest
from types import SimpleNamespace

from flatpal.downgrade_runner import _run, _run_unmask


def _ok(stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def _fail(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestRun(unittest.TestCase):
    def test_downgrade_ok_no_mask(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return _ok()

        ok, err, masked = _run(runner, "org.example.App", "system", "abc123", False)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertFalse(masked)
        self.assertEqual(len(calls), 1)
        self.assertIn("--commit=abc123", calls[0])
        self.assertNotIn("mask", calls[0])

    def test_downgrade_ok_mask_ok(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return _ok()

        ok, err, masked = _run(runner, "org.example.App", "user", "abc123", True)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertTrue(masked)
        self.assertEqual(len(calls), 2)
        self.assertIn("--user", calls[0])
        self.assertNotIn("pkexec", calls[0])
        self.assertIn("mask", calls[1])
        self.assertIn("--user", calls[1])
        self.assertIn("org.example.App", calls[1])
        self.assertNotIn("pkexec", calls[1])

    def test_system_scope_uses_pkexec_on_both_commands(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return _ok()

        ok, err, masked = _run(runner, "org.example.App", "system", "abc123", True)
        self.assertTrue(ok)
        self.assertTrue(masked)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "pkexec")
        self.assertIn("--commit=abc123", calls[0])
        self.assertEqual(calls[1][0], "pkexec")
        self.assertIn("mask", calls[1])

    def test_downgrade_ok_mask_fails(self):
        seq = [_ok(), _fail(1, stderr="error: permission denied")]
        calls = []

        def runner(argv):
            calls.append(argv)
            return seq.pop(0)

        ok, err, masked = _run(runner, "org.example.App", "system", "abc123", True)
        self.assertTrue(ok)
        self.assertFalse(masked)
        self.assertEqual(err, "error: permission denied")
        self.assertEqual(len(calls), 2)

    def test_mask_skipped_when_downgrade_fails(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return _fail(1, stderr="Authorization failed")

        ok, err, masked = _run(runner, "org.example.App", "system", "abc123", True)
        self.assertFalse(ok)
        self.assertFalse(masked)
        self.assertEqual(err, "Authorization failed")
        self.assertEqual(len(calls), 1)

    def test_downgrade_fail_falls_back_to_stdout_then_exit_text(self):
        ok, err, masked = _run(
            lambda argv: _fail(1, stdout="some progress line\n"),
            "org.example.App", "system", "abc123", False,
        )
        self.assertFalse(ok)
        self.assertEqual(err, "some progress line")
        self.assertFalse(masked)

        ok, err, _ = _run(
            lambda argv: _fail(7), "org.example.App", "system", "abc123", False,
        )
        self.assertFalse(ok)
        self.assertEqual(err, "flatpak update exited 7")

    def test_argv_user_scope(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        _run(runner, "org.example.App", "user", "abc123", False)
        self.assertIn("--user", seen["argv"])
        self.assertNotIn("--system", seen["argv"])
        self.assertIn("--commit=abc123", seen["argv"])
        self.assertIn("--noninteractive", seen["argv"])
        self.assertIn("-y", seen["argv"])
        self.assertIn("org.example.App", seen["argv"])

    def test_argv_system_scope(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        _run(runner, "org.example.App", "system", "abc123", False)
        self.assertIn("--system", seen["argv"])
        self.assertNotIn("--user", seen["argv"])

    def test_unknown_scope_defaults_to_system(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        _run(runner, "org.example.App", "", "abc123", False)
        self.assertIn("--system", seen["argv"])

    def test_file_not_found_on_downgrade(self):
        def runner(argv):
            raise FileNotFoundError("flatpak-spawn not found")

        ok, err, masked = _run(runner, "org.example.App", "system", "abc123", True)
        self.assertFalse(ok)
        self.assertIn("flatpak-spawn not found", err)
        self.assertFalse(masked)

    def test_timeout_on_downgrade(self):
        def runner(argv):
            raise subprocess.TimeoutExpired(cmd="flatpak", timeout=600)

        ok, err, masked = _run(runner, "org.example.App", "system", "abc123", False)
        self.assertFalse(ok)
        self.assertIn("600", err)
        self.assertFalse(masked)

    def test_timeout_on_mask_returns_partial_success(self):
        seq = [_ok()]

        def runner(argv):
            if not seq:
                raise subprocess.TimeoutExpired(cmd="flatpak", timeout=600)
            return seq.pop(0)

        ok, err, masked = _run(runner, "org.example.App", "system", "abc123", True)
        self.assertTrue(ok)
        self.assertFalse(masked)
        self.assertIn("600", err)


class TestUnmask(unittest.TestCase):
    def test_user_scope_no_pkexec(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        ok, err = _run_unmask(runner, "org.example.App", "user")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(seen["argv"][0], "flatpak")
        self.assertIn("--user", seen["argv"])
        self.assertIn("--remove", seen["argv"])
        self.assertIn("org.example.App", seen["argv"])
        self.assertNotIn("pkexec", seen["argv"])

    def test_system_scope_uses_pkexec(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        ok, err = _run_unmask(runner, "org.example.App", "system")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(seen["argv"][0], "pkexec")
        self.assertIn("flatpak", seen["argv"])
        self.assertIn("--system", seen["argv"])
        self.assertIn("--remove", seen["argv"])

    def test_nonzero_returns_failure(self):
        ok, err = _run_unmask(
            lambda argv: _fail(1, stderr="error: permission denied"),
            "org.example.App", "system",
        )
        self.assertFalse(ok)
        self.assertEqual(err, "error: permission denied")

    def test_file_not_found(self):
        def runner(argv):
            raise FileNotFoundError("flatpak-spawn not found")

        ok, err = _run_unmask(runner, "org.example.App", "user")
        self.assertFalse(ok)
        self.assertIn("flatpak-spawn", err)

    def test_timeout(self):
        def runner(argv):
            raise subprocess.TimeoutExpired(cmd="flatpak", timeout=600)

        ok, err = _run_unmask(runner, "org.example.App", "user")
        self.assertFalse(ok)
        self.assertIn("600", err)


if __name__ == "__main__":
    unittest.main()
