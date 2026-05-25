"""Running-flatpaks enumeration + per-app CPU and memory sampling.

Pure logic, no GTK. The UI calls `RunningTracker.sample()` on a timer and
renders the returned list. The tracker caches a Process-shaped object per
PID across samples so `cpu_percent()` computes a delta — the first sample
for a new process returns 0 % and subsequent samples reflect real activity.

Two backends share the same shape:

* `psutil.Process` — on the host (dev mode), when psutil is installed.
* `HostProc`       — inside the Flatpak sandbox, where psutil's /proc reads
                     would only see sandbox processes. HostProc reads
                     /proc/<pid>/{stat,status,cmdline,comm} via
                     `flatpak-spawn --host cat`, so it sees the host's
                     processes through the sandbox boundary. Falls back to
                     direct reads on the host when psutil is unavailable.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from typing import Callable, Dict, Iterable, List, Optional

from .host import host_cmd, is_sandboxed

try:
    import psutil
except ImportError:  # pragma: no cover — psutil is optional now
    psutil = None  # type: ignore


# Errors raised by both psutil and HostProc when /proc reads race against a
# process exiting, or when a stub test Process is missing methods. Catching
# at every Process boundary means the UI degrades to zero-values instead of
# crashing on a transient gone-process.
_PROC_ERRORS: tuple = (
    (psutil.Error if psutil else Exception),
    OSError,
)
_META_ERRORS: tuple = _PROC_ERRORS + (AttributeError, TypeError)


_CLOCK_TICKS = os.sysconf("SC_CLK_TCK")  # usually 100 on Linux

# /proc/stat's btime line is constant for the life of the kernel — cache it
# after the first read so HostProc.create_time() doesn't fire one shell-out
# per process per sample. Module-level so every HostProc instance shares it.
_BTIME: Optional[int] = None

_BATCH_BOUNDARY = "==FLATPAL_BOUNDARY=="


def _read_btime() -> Optional[int]:
    """Read /proc/stat once and cache btime (boot time, seconds since epoch)."""
    global _BTIME
    if _BTIME is not None:
        return _BTIME
    try:
        r = subprocess.run(
            host_cmd(["cat", "/proc/stat"]),
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("btime "):
            try:
                _BTIME = int(line.split()[1])
                return _BTIME
            except (IndexError, ValueError):
                return None
    return None


def _batch_read_files(paths: List[str], timeout: float = 10.0) -> List[Optional[str]]:
    """Read many host files in ONE flatpak-spawn invocation.

    Returns a list parallel to `paths`: per file, either its contents
    or None on missing/permission-denied. The boundary is unlikely to
    appear in /proc text files so naive split is safe in practice.
    """
    if not paths:
        return []
    quoted = " ".join(shlex.quote(p) for p in paths)
    script = (
        f'for f in {quoted}; do '
        f'  cat "$f" 2>/dev/null; '
        f'  printf "%s\\n" "{_BATCH_BOUNDARY}"; '
        f'done'
    )
    try:
        r = subprocess.run(
            host_cmd(["sh", "-c", script]),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [None] * len(paths)
    if r.returncode != 0:
        return [None] * len(paths)
    chunks = r.stdout.split(_BATCH_BOUNDARY + "\n")
    out: List[Optional[str]] = []
    for i in range(len(paths)):
        if i < len(chunks):
            chunk = chunks[i]
            out.append(chunk if chunk else None)
        else:
            out.append(None)
    return out


def _fetch_parents_map(timeout: float = 5.0) -> Dict[int, int]:
    """One `ps -eo pid,ppid` via host_cmd. Empty dict on failure."""
    try:
        r = subprocess.run(
            host_cmd(["ps", "-eo", "pid,ppid", "--no-headers"]),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    out: Dict[int, int] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            out[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return out


def _walk_descendants(root_pid: int, parents: Dict[int, int]) -> List[int]:
    """BFS through `parents` from root_pid; returns [root_pid, …descendants]."""
    seen = {root_pid}
    frontier = [root_pid]
    while frontier:
        cur = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == cur and pid not in seen:
                seen.add(pid)
                frontier.append(pid)
    return list(seen)


class _MemInfo:
    """psutil.Process.memory_info() shape — only .rss matters here."""
    __slots__ = ("rss",)
    def __init__(self, rss: int) -> None:
        self.rss = rss


class _ChildRef:
    """Shape matching psutil.Process.children() entries — only .pid is used."""
    __slots__ = ("pid",)
    def __init__(self, pid: int) -> None:
        self.pid = pid


class HostProc:
    """psutil.Process-shaped reader backed by /proc reads through host_cmd.

    Caches cmdline / comm / create_time on first access (none change for a
    live process). cpu_percent() follows psutil's `interval=None` contract:
    first call seeds the baseline and returns 0.0; later calls return the %
    over the delta since the previous call. memory_info().rss is read fresh
    each call from /proc/PID/status's VmRSS line.
    """

    __slots__ = ("pid", "_cmdline", "_name", "_create_time", "_last_cpu",
                 "_sample_stat", "_sample_status")

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._cmdline: Optional[List[str]] = None
        self._name: Optional[str] = None
        self._create_time: Optional[float] = None
        self._last_cpu: Optional[tuple] = None  # (utime+stime ticks, monotonic seconds)
        # Per-sample shared-fetch cache. Set by HostProc.batch_refresh at the
        # start of each RunningTracker.sample(), cleared at the end. When set,
        # cpu_percent/memory_info skip the one-shot `flatpak-spawn cat` and
        # read from this buffer instead — that's what brings a 11-app sample
        # from ~120 shell-outs down to 2.
        self._sample_stat: Optional[str] = None
        self._sample_status: Optional[str] = None

    def _read(self, path: str, timeout: float = 2.0) -> str:
        try:
            r = subprocess.run(
                host_cmd(["cat", path]),
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise OSError(str(exc)) from exc
        if r.returncode != 0:
            raise OSError(f"cat {path} rc={r.returncode}")
        return r.stdout

    @classmethod
    def batch_refresh(cls, procs: Dict[int, "HostProc"]) -> None:
        """Pre-fetch /proc/<pid>/{stat,status} for every proc in one shell-out.

        Also opportunistically fetches /proc/<pid>/{cmdline,comm} for procs
        that haven't cached those yet, so the first sample over a fresh set
        of PIDs only fires one shell-out instead of (4 × N + 1).
        """
        if not procs:
            return
        pids = list(procs.keys())
        needs_cmdline = [p for p in pids if procs[p]._cmdline is None]
        needs_comm = [p for p in pids if procs[p]._name is None]

        stat_paths = [f"/proc/{p}/stat" for p in pids]
        status_paths = [f"/proc/{p}/status" for p in pids]
        cmdline_paths = [f"/proc/{p}/cmdline" for p in needs_cmdline]
        comm_paths = [f"/proc/{p}/comm" for p in needs_comm]

        all_paths = stat_paths + status_paths + cmdline_paths + comm_paths
        contents = _batch_read_files(all_paths)
        n = len(pids)

        for i, pid in enumerate(pids):
            procs[pid]._sample_stat = contents[i] if i < len(contents) else None
            procs[pid]._sample_status = (
                contents[n + i] if n + i < len(contents) else None
            )

        offset = 2 * n
        for i, pid in enumerate(needs_cmdline):
            idx = offset + i
            text = contents[idx] if idx < len(contents) else None
            procs[pid]._cmdline = [a for a in (text or "").split("\0") if a]

        offset = 2 * n + len(needs_cmdline)
        for i, pid in enumerate(needs_comm):
            idx = offset + i
            text = contents[idx] if idx < len(contents) else None
            procs[pid]._name = (text or "").strip()

    @classmethod
    def clear_sample_cache(cls, procs: Dict[int, "HostProc"]) -> None:
        for p in procs.values():
            p._sample_stat = None
            p._sample_status = None

    def _stat_text(self) -> str:
        """Return /proc/PID/stat content from the per-sample cache or a fresh read."""
        if self._sample_stat is not None:
            return self._sample_stat
        return self._read(f"/proc/{self.pid}/stat")

    def _stat_fields_after_comm(self) -> List[str]:
        """Return /proc/PID/stat fields starting at field 3 (state).

        comm (field 2) is in parentheses and may contain spaces, so we slice
        from the LAST ')' rather than naive split.
        """
        text = self._stat_text()
        rparen = text.rfind(")")
        if rparen == -1:
            raise OSError(f"malformed stat for pid {self.pid}")
        return text[rparen + 2:].split()

    def cmdline(self) -> List[str]:
        if self._cmdline is None:
            try:
                text = self._read(f"/proc/{self.pid}/cmdline")
                self._cmdline = [a for a in text.split("\0") if a]
            except OSError:
                self._cmdline = []
        return self._cmdline

    def name(self) -> str:
        if self._name is None:
            try:
                self._name = self._read(f"/proc/{self.pid}/comm").strip()
            except OSError:
                self._name = ""
        return self._name

    def create_time(self) -> float:
        if self._create_time is not None:
            return self._create_time
        fields = self._stat_fields_after_comm()
        starttime_ticks = int(fields[19])  # stat field 22 = starttime
        btime = _read_btime() or 0  # cached module-level; one shell-out per session
        self._create_time = btime + starttime_ticks / _CLOCK_TICKS
        return self._create_time

    def cpu_percent(self, interval: Optional[float] = None) -> float:
        """psutil-compatible: interval=None returns delta since last call."""
        if interval is not None and interval > 0:
            time.sleep(interval)
        try:
            fields = self._stat_fields_after_comm()
            total_ticks = int(fields[11]) + int(fields[12])  # utime + stime
        except (OSError, ValueError, IndexError):
            self._last_cpu = None
            return 0.0
        now = time.monotonic()
        if self._last_cpu is None:
            self._last_cpu = (total_ticks, now)
            return 0.0
        prev_ticks, prev_now = self._last_cpu
        self._last_cpu = (total_ticks, now)
        delta = now - prev_now
        if delta <= 0:
            return 0.0
        return (total_ticks - prev_ticks) / _CLOCK_TICKS / delta * 100.0

    def memory_info(self) -> _MemInfo:
        if self._sample_status is not None:
            text = self._sample_status
        else:
            try:
                text = self._read(f"/proc/{self.pid}/status")
            except OSError:
                return _MemInfo(rss=0)
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return _MemInfo(rss=int(parts[1]) * 1024)
                    except ValueError:
                        return _MemInfo(rss=0)
                break
        return _MemInfo(rss=0)

    def children(self, recursive: bool = True) -> List[_ChildRef]:
        """Walk the host process tree rooted at self.pid via `ps -eo pid,ppid`.

        One shell-out per call; the in-memory BFS that follows is cheap.
        """
        try:
            r = subprocess.run(
                host_cmd(["ps", "-eo", "pid,ppid", "--no-headers"]),
                capture_output=True, text=True, timeout=3, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if r.returncode != 0:
            return []
        parents: dict = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                parents[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
        # BFS down from self.pid through `parents`.
        descendants: set = set()
        frontier = [self.pid]
        while frontier:
            cur = frontier.pop()
            for pid, ppid in parents.items():
                if ppid == cur and pid not in descendants and pid != self.pid:
                    descendants.add(pid)
                    if recursive:
                        frontier.append(pid)
        return [_ChildRef(p) for p in descendants]


def _default_process_factory():
    """Pick HostProc inside the sandbox (or when psutil is missing); else psutil.Process."""
    if is_sandboxed() or psutil is None:
        return HostProc
    return psutil.Process


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

    Inside our own sandbox the call is routed through `flatpak-spawn --host`
    so we see the host's running flatpaks, not the (empty) set inside our
    own jail. `runner(args) -> CompletedProcess` is injectable for tests.
    """
    args = host_cmd([
        "flatpak", "ps",
        "--columns=instance,pid,child-pid,application,branch",
    ])
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
        # process_factory(pid) -> psutil.Process-shaped object. The default
        # picks HostProc inside the sandbox (psutil's /proc reads can't see
        # the host) and psutil.Process on the host. Tests inject a stub.
        if process_factory is None:
            process_factory = _default_process_factory()
        self._process_factory = process_factory
        self._lister = lister or list_running_instances
        # pid -> Process-shaped object (psutil.Process / HostProc / mock).
        self._proc_cache: dict = {}

    def sample(self) -> List[dict]:
        """Return one row per running app with current cpu/memory.

        Row keys: `id`, `instances` (count), `pids` (list of ints),
        `cpu_percent`, `memory_bytes`, `branch`, `sub_instances`. The
        `sub_instances` list holds one dict per running sandbox of the app
        with its own `instance`, `pid`, `branch`, `cpu_percent`,
        `memory_bytes` — sorted by pid so the UI ordering doesn't flicker
        between samples.

        Sandbox fast path: when the process factory is HostProc, we
        pre-fetch /proc/<pid>/{stat,status} (and any uncached
        cmdline/comm) for every PID we'll touch in ONE flatpak-spawn
        invocation, plus one more for `ps -eo pid,ppid` to walk the
        tree. Without this batching, a single 11-app sample fires ~120
        flatpak-spawn cats at ~50–100 ms each — visibly slow (~15 s
        observed). With it, two shell-outs and ~200 ms total.
        """
        instances = self._lister()
        by_app = aggregate_by_app(instances)

        use_batch = self._process_factory is HostProc and bool(instances)
        parents_map: Optional[Dict[int, int]] = (
            _fetch_parents_map() if use_batch else None
        )

        # Walk trees up front so we know every PID before the batch fetch.
        tree_by_root: Dict[int, List[int]] = {}
        all_pids: set = set()
        for inst in instances:
            root_pid = inst.get("child_pid") or inst["pid"]
            if parents_map is not None:
                tree = _walk_descendants(root_pid, parents_map)
            else:
                tree = self._tree_pids(root_pid)
            tree_by_root[root_pid] = tree
            all_pids.update(tree)

        # Ensure a Process-shaped object exists for every PID (seeds the
        # cpu_percent delta baseline on the very first sample of a new PID).
        for pid in all_pids:
            self._get_process(pid)

        host_procs: Dict[int, HostProc] = {}
        if use_batch:
            host_procs = {
                pid: p for pid, p in self._proc_cache.items()
                if pid in all_pids and isinstance(p, HostProc)
            }
            HostProc.batch_refresh(host_procs)

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
                for pid in tree_by_root.get(root_pid, [root_pid]):
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

        if use_batch:
            HostProc.clear_sample_cache(host_procs)
        self._prune_cache(seen_pids)
        return rows

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
