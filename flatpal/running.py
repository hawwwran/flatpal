"""Running-flatpaks enumeration + per-app CPU and memory sampling.

Pure logic, no GTK. The UI calls `RunningTracker.sample()` on a timer and
renders the returned list. The tracker caches `psutil.Process` objects across
samples so `cpu_percent()` computes a delta — the first sample for a new
process returns 0 % and subsequent samples reflect real activity. Memory is
read from psutil's RSS (resident set size) — the physical RAM the process
currently holds.
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable, Iterable, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover — psutil is in the deps
    psutil = None  # type: ignore


# psutil raises these for: process gone, denied, zombie, race-conditions during
# /proc reads. We catch them at every psutil boundary so the UI degrades to
# zero-values rather than crashing.
_PROC_ERRORS: tuple = (
    (psutil.Error if psutil else Exception),
    OSError,
)

# Same as _PROC_ERRORS plus AttributeError/TypeError — the latter two cover
# stub Process objects in tests (which don't implement cmdline/name/create_time)
# and lets `_process_meta` fall through to its defaults instead of crashing.
_META_ERRORS: tuple = _PROC_ERRORS + (AttributeError, TypeError)


def _process_meta(proc) -> dict:
    """Best-effort extraction of cmdline / comm / create_time off a psutil.Process.

    Each field returns its empty/None default on any psutil or attribute error,
    so the UI degrades gracefully when /proc reads race or a test fixture
    skips one of the methods.
    """
    meta: dict = {"cmdline": [], "comm": "", "started_at": None}
    if proc is None:
        return meta
    try:
        c = proc.cmdline()
        if isinstance(c, list):
            meta["cmdline"] = [str(x) for x in c]
    except _META_ERRORS:
        pass
    try:
        n = proc.name()
        if n:
            meta["comm"] = str(n)
    except _META_ERRORS:
        pass
    try:
        meta["started_at"] = float(proc.create_time())
    except _META_ERRORS:
        pass
    return meta


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
    """Wraps `psutil.Process` cache + CPU/memory sampling per running flatpak app.

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
        `cpu_percent`, `memory_bytes`, `branch`, `sub_instances`. The
        `sub_instances` list holds one dict per running sandbox of the app
        with its own `instance`, `pid`, `branch`, `cpu_percent`,
        `memory_bytes` — sorted by pid so the UI ordering doesn't flicker
        between samples.
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
            sub_instances: List[dict] = []

            for inst in insts:
                root_pid = inst.get("child_pid") or inst["pid"]
                branch = branch or inst.get("branch", "")
                pids.append(root_pid)

                inst_cpu = 0.0
                inst_mem = 0
                for pid in self._tree_pids(root_pid):
                    seen_pids.add(pid)
                    cpu, mem = self._sample_pid(pid)
                    inst_cpu += cpu
                    inst_mem += mem

                # Read cmdline / comm / create_time off the ROOT pid only —
                # children inherit and would just duplicate noise. Best-effort:
                # /proc reads race, so any field may end up empty/None.
                root_proc = self._get_process(root_pid)
                meta = _process_meta(root_proc)

                cpu_sum += inst_cpu
                mem_sum += inst_mem
                sub_instances.append({
                    "instance": inst.get("instance", ""),
                    "pid": root_pid,
                    "branch": inst.get("branch", ""),
                    "cpu_percent": inst_cpu,
                    "memory_bytes": inst_mem,
                    "cmdline": meta["cmdline"],
                    "comm": meta["comm"],
                    "started_at": meta["started_at"],
                })

            # Oldest sandbox first — usually the "main" interactive instance.
            # PID tiebreaks when create_time is missing or equal (it can match
            # to the second for processes started back-to-back).
            sub_instances.sort(
                key=lambda s: (
                    s.get("started_at") if s.get("started_at") is not None else float("inf"),
                    s.get("pid", 0),
                )
            )

            rows.append({
                "id": app_id,
                "instances": len(insts),
                "pids": pids,
                "branch": branch,
                "cpu_percent": cpu_sum,
                "memory_bytes": mem_sum,
                "sub_instances": sub_instances,
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


def order_with_freeze(
    rows: List[dict],
    frozen_order: List[str],
    natural_sort: Callable[[List[dict]], List[dict]],
) -> List[dict]:
    """Apply a "freeze the row order" filter to a fresh sample.

    Rules:
      * Apps that were in `frozen_order` AND are still present in `rows`
        keep their relative position from `frozen_order`.
      * Newly-arrived apps (in `rows` but not in `frozen_order`) get
        appended at the end, in the order `natural_sort` produces — so
        their relative ordering is still sensible even when frozen.
      * Apps that vanished from the bus drop out.

    Pure helper: the UI page passes its own `natural_sort` closure so the
    freeze logic doesn't need to know about sort keys.
    """
    by_id = {r["id"]: r for r in rows}
    ordered: List[dict] = []
    for app_id in frozen_order:
        r = by_id.pop(app_id, None)
        if r is not None:
            ordered.append(r)
    if by_id:
        ordered.extend(natural_sort(list(by_id.values())))
    return ordered


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


def format_relative_time(
    epoch: Optional[float],
    now_fn: Optional[Callable[[], float]] = None,
) -> str:
    """Render a unix epoch as 'just now' / '5s ago' / '2m ago' / '3h ago' / '4d ago'.

    Returns the empty string when `epoch` is None or in the future. `now_fn`
    is injectable so unit tests don't depend on wall-clock time.
    """
    if epoch is None:
        return ""
    now = (now_fn or time.time)()
    delta = now - float(epoch)
    if delta < 0:
        return ""
    if delta < 10:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"
