"""Running-flatpaks enumeration + per-app CPU and RSS sampling.

Pure logic, no GTK. The UI calls `RunningTracker.sample()` on a timer and
renders the returned list. The tracker caches `psutil.Process` objects across
samples so `cpu_percent()` computes a delta — the first sample for a new
process returns 0 % and subsequent samples reflect real activity.
"""

from __future__ import annotations

import subprocess
from typing import Iterable, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover — psutil is in the deps
    psutil = None  # type: ignore


# psutil raises these for: process gone, denied, zombie, race-conditions during
# /proc reads. We catch them at every psutil boundary so the UI degrades to
# zero-values rather than crashing.
_PROC_ERRORS = (
    (psutil.Error if psutil else Exception),
    OSError,
)


def list_running_instances(runner=None) -> List[dict]:
    """Return one dict per running flatpak sandbox via `flatpak ps`.

    `runner(args) -> CompletedProcess` may be injected for tests.
    """
    args = [
        "flatpak", "ps",
        "--columns=instance,pid,child-pid,application,branch",
    ]
    run = runner or (lambda a: subprocess.run(
        a, capture_output=True, text=True, check=False, timeout=5,
    ))
    try:
        result = run(args)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if getattr(result, "returncode", 0) != 0:
        return []

    out: List[dict] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        cells = line.split("\t")
        while len(cells) < 5:
            cells.append("")
        instance, pid_s, child_pid_s, app_id, branch = cells[:5]
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        try:
            child_pid = int(child_pid_s) if child_pid_s.strip() else pid
        except ValueError:
            child_pid = pid
        if not app_id:
            continue
        out.append({
            "instance": instance,
            "pid": pid,
            "child_pid": child_pid,
            "id": app_id,
            "branch": branch,
        })
    return out


def aggregate_by_app(instances: Iterable[dict]) -> dict:
    """Group instances by `id`. Returns `{app_id: [instance, ...]}`."""
    out: dict = {}
    for inst in instances:
        out.setdefault(inst["id"], []).append(inst)
    return out


class RunningTracker:
    """Wraps `psutil.Process` cache + CPU/RSS sampling per running flatpak app.

    Usage:
        tracker = RunningTracker()
        rows = tracker.sample()  # first call: cpu=0 baseline
        time.sleep(2)
        rows = tracker.sample()  # subsequent: real cpu% over the interval
    """

    def __init__(self, process_factory=None, lister=None):
        # process_factory(pid) -> psutil.Process; injectable for tests.
        if process_factory is None:
            if psutil is None:
                raise RuntimeError("psutil is not installed")
            process_factory = psutil.Process
        self._process_factory = process_factory
        self._lister = lister or list_running_instances
        # pid -> psutil.Process (or compatible mock)
        self._proc_cache: dict = {}

    def sample(self) -> List[dict]:
        """Return one row per running app with current cpu/memory.

        Row keys: `id`, `instances` (count), `pids` (list of ints),
        `cpu_percent`, `memory_bytes`, `branch`.
        """
        instances = self._lister()
        by_app = aggregate_by_app(instances)

        seen_pids: set = set()
        rows: List[dict] = []

        for app_id, insts in by_app.items():
            cpu_sum = 0.0
            mem_sum = 0
            pids: List[int] = []
            branch = ""

            for inst in insts:
                root_pid = inst.get("child_pid") or inst["pid"]
                branch = branch or inst.get("branch", "")
                pids.append(root_pid)

                for pid in self._tree_pids(root_pid):
                    seen_pids.add(pid)
                    cpu, mem = self._sample_pid(pid)
                    cpu_sum += cpu
                    mem_sum += mem

            rows.append({
                "id": app_id,
                "instances": len(insts),
                "pids": pids,
                "branch": branch,
                "cpu_percent": cpu_sum,
                "memory_bytes": mem_sum,
            })

        self._prune_cache(seen_pids)
        return rows

    # ----- internals -------------------------------------------------------

    def _get_process(self, pid: int):
        cached = self._proc_cache.get(pid)
        if cached is not None:
            return cached
        try:
            proc = self._process_factory(pid)
        except _PROC_ERRORS:
            return None
        # Prime cpu_percent so the next sample yields a real delta.
        try:
            proc.cpu_percent(interval=None)
        except _PROC_ERRORS:
            pass
        self._proc_cache[pid] = proc
        return proc

    def _sample_pid(self, pid: int):
        proc = self._get_process(pid)
        if proc is None:
            return 0.0, 0
        try:
            cpu = float(proc.cpu_percent(interval=None))
            mem = int(proc.memory_info().rss)
            return cpu, mem
        except _PROC_ERRORS:
            self._proc_cache.pop(pid, None)
            return 0.0, 0

    def _tree_pids(self, root_pid: int) -> List[int]:
        """All PIDs in the process tree rooted at `root_pid`, including root."""
        proc = self._get_process(root_pid)
        if proc is None:
            return [root_pid]
        try:
            tree = [root_pid] + [c.pid for c in proc.children(recursive=True)]
        except _PROC_ERRORS:
            return [root_pid]
        return tree

    def _prune_cache(self, alive_pids: set) -> None:
        """Drop cached processes that didn't appear in the latest sample."""
        for pid in list(self._proc_cache.keys()):
            if pid not in alive_pids:
                self._proc_cache.pop(pid, None)


# ----- formatting ----------------------------------------------------------


SORT_KEYS = ("cpu", "memory", "name")


def sort_running(rows, key: str) -> list:
    """Sort running-app rows by one of SORT_KEYS.

    `cpu` and `memory` sort descending (high values on top); `name` sorts
    casefolded ascending. Display name (row['display_name']) is the
    tie-breaker for cpu/memory, so apps with equal stats land alphabetically.
    Unknown key falls back to 'cpu' descending (matches the user-visible
    default).
    """
    def name_key(row):
        return (row.get("display_name") or row.get("id") or "").casefold()

    if key == "memory":
        return sorted(rows, key=lambda r: (-int(r.get("memory_bytes") or 0), name_key(r)))
    if key == "name":
        return sorted(rows, key=name_key)
    # 'cpu' or anything else → cpu descending
    return sorted(rows, key=lambda r: (-float(r.get("cpu_percent") or 0.0), name_key(r)))


def format_memory(bytes_: int) -> str:
    if bytes_ <= 0:
        return "—"
    if bytes_ < 1024 * 1024:
        return f"{bytes_ / 1024:.0f} KB"
    if bytes_ < 1024 * 1024 * 1024:
        return f"{bytes_ / (1024 * 1024):.1f} MB"
    return f"{bytes_ / (1024 * 1024 * 1024):.2f} GB"


def format_cpu(percent: Optional[float]) -> str:
    """Format CPU usage as e.g. '12.3%'. 100 % = one fully-loaded core."""
    if percent is None:
        return "—"
    return f"{percent:.1f}%"
