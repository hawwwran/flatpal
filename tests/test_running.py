"""Tests for the running-tracker — psutil and subprocess fully mocked."""

import unittest
from unittest import mock

from flatpal.running import (
    RunningTracker,
    SORT_KEYS,
    aggregate_by_app,
    format_cpu,
    format_memory,
    list_running_instances,
    sort_running,
)


def _make_run_result(stdout: str, returncode: int = 0):
    return mock.Mock(stdout=stdout, returncode=returncode)


class TestListRunningInstances(unittest.TestCase):
    def test_parses_tab_separated_output(self):
        sample = (
            "12345\t1000\t1001\torg.signal.Signal\tstable\n"
            "67890\t2000\t2002\tcom.discordapp.Discord\tstable\n"
        )
        rows = list_running_instances(
            runner=lambda _args: _make_run_result(sample)
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "org.signal.Signal")
        self.assertEqual(rows[0]["pid"], 1000)
        self.assertEqual(rows[0]["child_pid"], 1001)
        self.assertEqual(rows[1]["child_pid"], 2002)

    def test_missing_child_pid_falls_back_to_pid(self):
        sample = "12345\t1000\t\torg.example\tstable\n"
        rows = list_running_instances(
            runner=lambda _args: _make_run_result(sample)
        )
        self.assertEqual(rows[0]["child_pid"], 1000)

    def test_returncode_nonzero_returns_empty(self):
        rows = list_running_instances(
            runner=lambda _args: _make_run_result("anything", returncode=1)
        )
        self.assertEqual(rows, [])

    def test_missing_flatpak_returns_empty(self):
        def boom(_args):
            raise FileNotFoundError("no flatpak")
        self.assertEqual(list_running_instances(runner=boom), [])

    def test_blank_lines_ignored(self):
        sample = "\n\n12345\t1\t2\torg.x\tstable\n\n"
        rows = list_running_instances(
            runner=lambda _args: _make_run_result(sample)
        )
        self.assertEqual(len(rows), 1)

    def test_skips_rows_with_unparseable_pid(self):
        sample = "abc\tnot-int\t-\torg.x\tstable\n12345\t1\t2\torg.y\tstable\n"
        rows = list_running_instances(
            runner=lambda _args: _make_run_result(sample)
        )
        self.assertEqual([r["id"] for r in rows], ["org.y"])

    def test_skips_rows_with_no_app_id(self):
        sample = "12345\t1\t2\t\tstable\n"
        rows = list_running_instances(
            runner=lambda _args: _make_run_result(sample)
        )
        self.assertEqual(rows, [])


class TestAggregateByApp(unittest.TestCase):
    def test_groups_by_app_id(self):
        inst = [
            {"id": "org.signal.Signal", "pid": 1},
            {"id": "org.signal.Signal", "pid": 2},
            {"id": "com.discordapp.Discord", "pid": 3},
        ]
        grouped = aggregate_by_app(inst)
        self.assertEqual(set(grouped.keys()),
                         {"org.signal.Signal", "com.discordapp.Discord"})
        self.assertEqual(len(grouped["org.signal.Signal"]), 2)


class _FakeMemInfo:
    def __init__(self, rss):
        self.rss = rss


class _FakeProcess:
    """Minimal psutil.Process stand-in.

    Mimics real psutil semantics: the FIRST call to cpu_percent returns 0.0
    (baseline) without consuming the sequence; subsequent calls pop the
    queued values. This matches how `_get_process` primes new entries.
    """

    def __init__(self, pid, cpu_seq=None, rss=10 * 1024 * 1024, children=None):
        self.pid = pid
        self._cpu_seq = list(cpu_seq or [])
        self._rss = rss
        self._children = children or []
        self._primed = False

    def cpu_percent(self, interval=None):
        if not self._primed:
            self._primed = True
            return 0.0
        if not self._cpu_seq:
            return 0.0
        return self._cpu_seq.pop(0)

    def memory_info(self):
        return _FakeMemInfo(self._rss)

    def children(self, recursive=False):
        return list(self._children)


class TestRunningTracker(unittest.TestCase):
    def _make_lister(self, *instances):
        return lambda: list(instances)

    def _factory_from(self, processes_by_pid):
        def factory(pid):
            if pid in processes_by_pid:
                return processes_by_pid[pid]
            raise KeyError(pid)
        return factory

    def test_single_instance_aggregates_cpu_and_memory(self):
        # An app with a root proc and one child.
        child = _FakeProcess(101, cpu_seq=[5.0, 12.0], rss=40 * 1024 * 1024)
        root = _FakeProcess(100, cpu_seq=[2.0, 8.0], rss=50 * 1024 * 1024,
                            children=[child])
        tracker = RunningTracker(
            process_factory=self._factory_from({100: root, 101: child}),
            lister=self._make_lister(
                {"instance": "x", "pid": 100, "child_pid": 100,
                 "id": "org.example", "branch": "stable"}
            ),
        )
        first = tracker.sample()
        # First sample primes cpu_percent; effective return is the priming
        # value popped (so the row sees the test's first cpu_seq item).
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["id"], "org.example")
        self.assertEqual(first[0]["memory_bytes"], 90 * 1024 * 1024)

        second = tracker.sample()
        # Sum of next cpu_seq values: 8 + 12 = 20
        self.assertAlmostEqual(second[0]["cpu_percent"], 20.0)

    def test_multiple_instances_of_same_app_sum_together(self):
        a = _FakeProcess(1, cpu_seq=[1.0, 5.0], rss=10 * 1024 * 1024)
        b = _FakeProcess(2, cpu_seq=[2.0, 7.0], rss=20 * 1024 * 1024)
        tracker = RunningTracker(
            process_factory=self._factory_from({1: a, 2: b}),
            lister=self._make_lister(
                {"instance": "i1", "pid": 1, "child_pid": 1,
                 "id": "org.example", "branch": "stable"},
                {"instance": "i2", "pid": 2, "child_pid": 2,
                 "id": "org.example", "branch": "stable"},
            ),
        )
        tracker.sample()  # prime
        second = tracker.sample()
        row = second[0]
        self.assertEqual(row["instances"], 2)
        self.assertEqual(row["memory_bytes"], 30 * 1024 * 1024)
        self.assertAlmostEqual(row["cpu_percent"], 12.0)
        self.assertEqual(set(row["pids"]), {1, 2})

    def test_stale_processes_pruned_from_cache(self):
        a = _FakeProcess(1, cpu_seq=[1.0, 2.0])
        b = _FakeProcess(2, cpu_seq=[1.0, 2.0])
        instances = [
            {"instance": "i1", "pid": 1, "child_pid": 1,
             "id": "org.a", "branch": "stable"},
            {"instance": "i2", "pid": 2, "child_pid": 2,
             "id": "org.b", "branch": "stable"},
        ]
        lister_calls = {"n": 0}
        def lister():
            lister_calls["n"] += 1
            if lister_calls["n"] == 1:
                return list(instances)
            return [instances[0]]  # pid=2 disappears

        tracker = RunningTracker(
            process_factory=self._factory_from({1: a, 2: b}),
            lister=lister,
        )
        tracker.sample()
        self.assertIn(2, tracker._proc_cache)
        tracker.sample()
        self.assertNotIn(2, tracker._proc_cache)

    def test_unknown_pid_returns_zero(self):
        # Factory raises one of the recognised psutil errors → tracker uses
        # zeros, doesn't crash.
        import psutil

        def factory(_pid):
            raise psutil.NoSuchProcess(99)
        tracker = RunningTracker(
            process_factory=factory,
            lister=self._make_lister(
                {"instance": "i", "pid": 99, "child_pid": 99,
                 "id": "org.example", "branch": "stable"}
            ),
        )
        rows = tracker.sample()
        self.assertEqual(rows[0]["cpu_percent"], 0.0)
        self.assertEqual(rows[0]["memory_bytes"], 0)

    def test_empty_list(self):
        tracker = RunningTracker(
            process_factory=lambda pid: _FakeProcess(pid),
            lister=lambda: [],
        )
        self.assertEqual(tracker.sample(), [])


class TestSortRunning(unittest.TestCase):
    ROWS = [
        {"id": "org.a", "display_name": "Alpha",
         "cpu_percent": 10.0, "memory_bytes": 300 * 1024 * 1024},
        {"id": "org.b", "display_name": "Bravo",
         "cpu_percent": 50.0, "memory_bytes": 100 * 1024 * 1024},
        {"id": "org.c", "display_name": "Charlie",
         "cpu_percent": 0.0, "memory_bytes": 500 * 1024 * 1024},
    ]

    def test_sort_keys_constant(self):
        self.assertEqual(set(SORT_KEYS), {"cpu", "memory", "name"})

    def test_sort_by_cpu_descending(self):
        out = sort_running(self.ROWS, "cpu")
        self.assertEqual([r["id"] for r in out], ["org.b", "org.a", "org.c"])

    def test_sort_by_memory_descending(self):
        out = sort_running(self.ROWS, "memory")
        self.assertEqual([r["id"] for r in out], ["org.c", "org.a", "org.b"])

    def test_sort_by_name_casefold(self):
        out = sort_running(self.ROWS, "name")
        self.assertEqual([r["id"] for r in out], ["org.a", "org.b", "org.c"])

    def test_sort_unknown_key_defaults_to_cpu(self):
        out = sort_running(self.ROWS, "bogus")
        self.assertEqual(out[0]["id"], "org.b")  # cpu highest

    def test_cpu_tie_breaks_alphabetically(self):
        rows = [
            {"id": "org.z", "display_name": "Zebra", "cpu_percent": 5.0,
             "memory_bytes": 1},
            {"id": "org.a", "display_name": "Apple", "cpu_percent": 5.0,
             "memory_bytes": 1},
        ]
        out = sort_running(rows, "cpu")
        self.assertEqual([r["id"] for r in out], ["org.a", "org.z"])

    def test_uses_id_when_display_name_missing(self):
        rows = [
            {"id": "org.b", "cpu_percent": 1, "memory_bytes": 1},
            {"id": "org.a", "cpu_percent": 1, "memory_bytes": 1},
        ]
        out = sort_running(rows, "name")
        self.assertEqual([r["id"] for r in out], ["org.a", "org.b"])

    def test_tolerates_missing_numeric_fields(self):
        out = sort_running(
            [{"id": "x", "display_name": "x"}],
            "cpu",
        )
        self.assertEqual(out[0]["id"], "x")


class TestFormatters(unittest.TestCase):
    def test_format_memory(self):
        self.assertEqual(format_memory(0), "—")
        self.assertEqual(format_memory(1024 * 250), "250 KB")
        self.assertEqual(format_memory(int(1024 * 1024 * 1.5)), "1.5 MB")
        self.assertEqual(format_memory(1024 * 1024 * 1024), "1.00 GB")
        self.assertEqual(format_memory(int(2.5 * 1024**3)), "2.50 GB")
        # Sub-KB values round through banker's rounding for ":.0f".
        self.assertIn(format_memory(512), {"0 KB", "1 KB"})

    def test_format_cpu(self):
        self.assertEqual(format_cpu(None), "—")
        self.assertEqual(format_cpu(0.0), "0.0%")
        self.assertEqual(format_cpu(12.345), "12.3%")


if __name__ == "__main__":
    unittest.main()
