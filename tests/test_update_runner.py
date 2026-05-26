"""Tests for the per-app update runner.

Pure-subprocess module: the real `flatpak update` invocation is slow,
needs network, and mutates state. Tests inject a fake runner via
`_run`'s callable parameter and assert on the argv it's handed and the
shape of the (ok, err) tuple it returns.
"""

import subprocess
import unittest
from types import SimpleNamespace

from flatpal.update_runner import _run


def _ok(stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def _fail(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestRun(unittest.TestCase):
    def test_happy_path_returns_true_none(self):
        ok, err = _run(lambda argv: _ok(), "org.example.App", "system")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_nonzero_returncode_yields_stderr_tail(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _fail(1, stderr="warning: ignoring foo\nerror: No space left on device\n")

        ok, err = _run(runner, "org.example.App", "system")
        self.assertFalse(ok)
        self.assertEqual(err, "error: No space left on device")

    def test_nonzero_returncode_falls_back_to_stdout_then_exit_text(self):
        ok, err = _run(lambda argv: _fail(1, stdout="bar\n"), "org.example.App", "system")
        self.assertFalse(ok)
        self.assertEqual(err, "bar")

        ok, err = _run(lambda argv: _fail(2), "org.example.App", "system")
        self.assertFalse(ok)
        self.assertEqual(err, "flatpak update exited 2")

    def test_file_not_found_returns_false_with_exception_text(self):
        def runner(argv):
            raise FileNotFoundError("flatpak-spawn not found")

        ok, err = _run(runner, "org.example.App", "system")
        self.assertFalse(ok)
        self.assertIn("flatpak-spawn not found", err)

    def test_timeout_returns_false_with_exception_text(self):
        def runner(argv):
            raise subprocess.TimeoutExpired(cmd="flatpak", timeout=600)

        ok, err = _run(runner, "org.example.App", "system")
        self.assertFalse(ok)
        self.assertIn("600", err)

    def test_scope_user_passes_user_flag(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        _run(runner, "org.example.App", "user")
        self.assertIn("--user", seen["argv"])
        self.assertNotIn("--system", seen["argv"])

    def test_scope_system_passes_system_flag(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        _run(runner, "org.example.App", "system")
        self.assertIn("--system", seen["argv"])
        self.assertNotIn("--user", seen["argv"])

    def test_unknown_scope_defaults_to_system(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        _run(runner, "org.example.App", "")
        self.assertIn("--system", seen["argv"])

    def test_argv_carries_app_id_and_noninteractive(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return _ok()

        _run(runner, "org.example.App", "system")
        self.assertIn("org.example.App", seen["argv"])
        self.assertIn("--noninteractive", seen["argv"])
        self.assertIn("-y", seen["argv"])


if __name__ == "__main__":
    unittest.main()
