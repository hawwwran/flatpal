"""Fetch and parse `flatpak remote-info --log` for a single installed app.

The detail page's "Recent releases" section is built straight from this
log: each OSTree commit becomes a row with a Downgrade button, so every
row is guaranteed installable. `fetch_current_commit` returns the
locally-deployed commit hash so the matching row can be marked
"Current" instead of getting a button. Failure is silent (same stance as
`updates.py`): an empty section is better than a crashed app.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional

from .host import host_cmd


_TIMEOUT_SECONDS = 30


def fetch_log(app_id: str, scope: str, remote: str) -> Optional[str]:
    """Run `flatpak remote-info --log` for `app_id` against `remote`.

    `scope` is "system" or "user"; anything else falls back to "system"
    so callers can pass `app.get("installation") or "system"` without
    branching. Returns the raw stdout or None on any failure.
    """
    return _run(_default_runner, app_id, scope, remote)


def _default_runner(argv):
    return subprocess.run(
        argv,
        capture_output=True, text=True, check=False,
        timeout=_TIMEOUT_SECONDS,
    )


def _run(
    runner: Callable[[list], subprocess.CompletedProcess],
    app_id: str,
    scope: str,
    remote: str,
) -> Optional[str]:
    """Indirection so tests can supply a canned subprocess result."""
    if not remote or not app_id:
        return None
    scope_flag = "--user" if scope == "user" else "--system"
    argv = host_cmd([
        "flatpak", "remote-info", scope_flag, "--log", remote, app_id,
    ])
    try:
        res = runner(argv)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def parse_log(text: str) -> list[dict]:
    """Return list of commit records, newest-first.

    Each record has `commit`, `parent` (optional), `subject`, `date`,
    `date_short` (first 10 chars of `date`). Header block before the
    first `Commit:` line is skipped; the `History:` line that flatpak
    prints between the head record and older commits is tolerated.
    """
    if not text:
        return []

    records: list[dict] = []
    current: Optional[dict] = None
    seen_first = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "Commit":
            if current is not None:
                records.append(current)
            current = {"commit": value, "parent": "", "subject": "", "date": "", "date_short": ""}
            seen_first = True
            continue
        if not seen_first:
            continue
        if current is None:
            continue
        if key == "Parent":
            current["parent"] = value
        elif key == "Subject":
            current["subject"] = value
        elif key == "Date":
            current["date"] = value
            current["date_short"] = value[:10]

    if current is not None:
        records.append(current)
    return records


def fetch_current_commit(app_id: str, scope: str) -> Optional[str]:
    """Run `flatpak info --show-commit` and return the locally-deployed hash.

    Returns None on any failure. Used to mark the matching row "Current"
    in the Recent releases section.
    """
    return _run_show_commit(_default_runner, app_id, scope)


def _run_show_commit(
    runner: Callable[[list], subprocess.CompletedProcess],
    app_id: str,
    scope: str,
) -> Optional[str]:
    if not app_id:
        return None
    scope_flag = "--user" if scope == "user" else "--system"
    argv = host_cmd([
        "flatpak", "info", scope_flag, "--show-commit", app_id,
    ])
    try:
        res = runner(argv)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    out = (res.stdout or "").strip()
    return out or None


def fetch_is_masked(app_id: str, scope: str) -> bool:
    """Return True iff `app_id` appears in the `flatpak mask` list.

    Silent failure (returns False) on any subprocess error: a missing
    banner is better than a crashed page.
    """
    return _run_mask_list(_default_runner, app_id, scope)


def _run_mask_list(
    runner: Callable[[list], subprocess.CompletedProcess],
    app_id: str,
    scope: str,
) -> bool:
    if not app_id:
        return False
    scope_flag = "--user" if scope == "user" else "--system"
    argv = host_cmd(["flatpak", "mask", scope_flag])
    try:
        res = runner(argv)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if res.returncode != 0:
        return False
    needle = app_id.strip()
    for line in (res.stdout or "").splitlines():
        if needle and needle in line:
            return True
    return False
