"""Pure helpers — no GTK imports, safe to load in any environment.

Everything Flatpal displays comes from one of these functions, so this is
also the testable surface.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from typing import Iterable, Optional

from .host import host_cmd


MONTHS = {
    m: i for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
    )
}

SIZE_UNITS = {
    "B": 1,
    "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
    "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
}

_SIZE_RE = re.compile(r"([\d,.]+)\s*([KMGT]?i?B)", re.IGNORECASE)


def parse_size(text: Optional[str]) -> int:
    """Parse '32,6 MB' / '512 KiB' / '?' / '' → bytes. Unknown/empty → 0."""
    text = (text or "").strip()
    if not text or text == "?":
        return 0
    m = _SIZE_RE.match(text)
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", "."))
    except ValueError:
        return 0
    return int(num * SIZE_UNITS.get(m.group(2).upper(), 1))


def parse_history_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse the locale-independent time column from `flatpak history` (LC_ALL=C).

    Two shapes occur in the wild:
      'Apr 22 17:03:17' — within ~6 months, no year (year=current; if future, -1)
      'Apr 22 2024'    — older entries, with year, no time

    Returns None for anything we don't recognise.
    """
    if not text:
        return None
    parts = text.split()
    if len(parts) != 3:
        return None
    month_s, day_s, last = parts
    month = MONTHS.get(month_s)
    if month is None:
        return None
    try:
        day = int(day_s)
        if ":" in last:
            time_parts = last.split(":")
            if len(time_parts) != 3:
                return None
            h, mm, ss = (int(x) for x in time_parts)
            ref = now or datetime.now()
            dt = datetime(ref.year, month, day, h, mm, ss)
            if dt > ref:
                dt = dt.replace(year=ref.year - 1)
            return dt
        year = int(last)
        return datetime(year, month, day)
    except (ValueError, KeyError):
        return None


def parse_history_output(text: str) -> dict:
    """Map app_id → earliest 'deploy install' datetime from `flatpak history`."""
    out: dict = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        time_str, change, app = (p.strip() for p in parts)
        if "deploy install" not in change:
            continue
        dt = parse_history_time(time_str)
        if dt is None:
            continue
        if app not in out or dt < out[app]:
            out[app] = dt
    return out


def parse_list_output(text: str, install_dates: Optional[dict] = None) -> list:
    """Parse `flatpak list --app --columns=...` output into a list of dicts."""
    install_dates = install_dates or {}
    apps = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = line.split("\t")
        while len(cells) < 7:
            cells.append("")
        app_id, name, version, branch, origin, install, size_str = cells[:7]
        apps.append({
            "id": app_id,
            "name": name or app_id,
            "version": version,
            "branch": branch,
            "origin": origin,
            "installation": install,
            "size_str": size_str,
            "size_bytes": parse_size(size_str),
            "installed": install_dates.get(app_id),
        })
    return apps


def format_date(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


SORT_KEYS = ("name", "date", "size")


def sort_apps(apps: Iterable[dict], key: str, reverse: bool = False) -> list:
    """Stable sort by name|date|size; unknown key falls back to name."""
    if key == "date":
        def k(a): return a["installed"] or datetime.min
    elif key == "size":
        def k(a): return a["size_bytes"]
    else:
        def k(a): return a["name"].casefold()
    return sorted(apps, key=k, reverse=reverse)


# --- subprocess wrappers (impure) -------------------------------------------

def _run_flatpak(args, env_overrides=None, timeout: float = 10.0):
    """Run `flatpak <args>` with a hard timeout. Returns None on timeout / missing binary."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    try:
        return subprocess.run(
            host_cmd(["flatpak", *args], env=env_overrides),
            capture_output=True, text=True, env=env, check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def fetch_install_dates() -> dict:
    res = _run_flatpak(
        ["history", "--columns=time,change,application"],
        env_overrides={"LC_ALL": "C"},
    )
    if res is None or res.returncode != 0:
        return {}
    return parse_history_output(res.stdout)


def fetch_apps() -> list:
    res = _run_flatpak([
        "list", "--app",
        "--columns=application,name,version,branch,origin,installation,size",
    ])
    if res is None or res.returncode != 0:
        return []
    return parse_list_output(res.stdout, fetch_install_dates())


def fetch_remote_options() -> dict:
    """Return {(name, scope): {options}} for every configured flatpak remote.

    `scope` is "system" or "user" (matching what `flatpak list --columns=installation`
    reports for the corresponding installed apps). `options` is the set parsed
    from the `options` column with the scope token stripped — typically
    something like {"no-enumerate"} or empty.

    Used to detect the bundle-install quirk where the auto-created remote has
    `no-enumerate=true`, which keeps GNOME Software from indexing the app.
    """
    res = _run_flatpak(["remotes", "--columns=name,options"])
    if res is None or res.returncode != 0:
        return {}
    result: dict = {}
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) < 2:
            continue
        name, options_str = cells[0].strip(), cells[1].strip()
        opts = {o.strip() for o in options_str.split(",") if o.strip()}
        scope = "user" if "user" in opts else "system"
        opts.discard("system")
        opts.discard("user")
        result[(name, scope)] = opts
    return result


def fix_remote_no_enumerate(remote: str, scope: str) -> tuple:
    """Clear the no-enumerate flag on `remote`, then refresh its AppStream.

    Returns `(ok, err)`:
      - `(True, "")` once `remote-modify` succeeds. The follow-up
        `--appstream` refresh is best-effort: it makes GNOME Software see
        the app immediately, but a slow or failing refresh doesn't roll
        back the flag (which is already cleared at that point).
      - `(False, msg)` if `remote-modify` fails — `msg` is the captured
        stderr, or the exception text when the binary is missing or times
        out.

    Single source of truth for the actual flatpak invocations: an earlier
    regression mistyped the flag as `--no-no-enumerate` and silently
    no-op'd, and we don't want two call sites that could regress
    independently.
    """
    scope_flag = f"--{scope}"
    try:
        r = subprocess.run(
            host_cmd(
                ["flatpak", "remote-modify", scope_flag, "--enumerate", remote]
            ),
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if r.returncode != 0:
        return False, r.stderr.strip() or "unknown error"
    try:
        subprocess.run(
            host_cmd(
                ["flatpak", "update", scope_flag, "--appstream", remote]
            ),
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return True, ""
