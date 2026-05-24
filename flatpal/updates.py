"""Discover Flatpak apps with available updates from the configured remotes.

One `flatpak remote-ls --updates --app` call lists every installed app whose
remote ref is newer than what's deployed locally. The cost is ~2.5 s and it
isn't per-app, so a single background fetch at startup is enough to feed the
"Update available" badges across all three tabs and the detail page.

Failure is silent: a missing badge is better than a crashed app, and the
flatpak command can fail for legitimate reasons (no remotes configured, no
network on first launch with empty appstream cache, …).
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional

from .host import host_cmd


# Order has to match `_parse` below — we pin the column list so a future
# libflatpak default change can't shift columns under us.
_COLUMNS = "application,version,branch,origin,commit"


def fetch_updates() -> dict:
    """Run `flatpak remote-ls --updates --app` and return a per-app-id dict.

    Returns `{app_id: {"version": str, "branch": str, "origin": str,
    "commit": str}}`. Apps without an entry have no available update.
    Returns `{}` on any failure (timeout, missing binary, non-zero exit).
    """
    return _fetch(_run)


def _run() -> Optional[str]:
    try:
        res = subprocess.run(
            host_cmd([
                "flatpak", "remote-ls", "--updates", "--app",
                f"--columns={_COLUMNS}",
            ]),
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def _fetch(runner: Callable[[], Optional[str]]) -> dict:
    """Indirection so tests can supply canned `flatpak` output."""
    text = runner()
    if text is None:
        return {}
    return _parse(text)


def _parse(text: str) -> dict:
    """Parse the tab-separated `flatpak remote-ls --updates` output.

    First non-blank non-header line forward; tolerates the header row that
    `flatpak` prints when stdout is a TTY (it isn't, when we capture, but
    pin defensively because Flatpak's CLI is allowed to change).
    """
    out: dict = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = line.split("\t")
        # Pad short rows so unpacking doesn't IndexError.
        while len(cells) < 5:
            cells.append("")
        app_id, version, branch, origin, commit = (c.strip() for c in cells[:5])
        if not app_id or app_id.lower() == "application":
            continue
        # If both system and user installs have updates, the same app_id
        # shows up twice with identical version/origin — keep the first.
        if app_id in out:
            continue
        out[app_id] = {
            "version": version,
            "branch": branch,
            "origin": origin,
            "commit": commit,
        }
    return out


def releases_since(releases: list, installed_version: str) -> list:
    """Slice `releases` (newest-first, from AppStream metainfo) to those that
    landed strictly after `installed_version`.

    String comparison rather than semver parsing — AppStream versions are
    free-form (1.2.3, 2025.05, v0.1.1, …) and the metainfo entries are
    already sorted by the upstream. We stop at the first entry that
    matches `installed_version`; everything before it (newer in the list,
    older in time) is the "what's new since install" diff.
    """
    if not installed_version:
        return list(releases)
    out: list = []
    inst = installed_version.strip().lstrip("vV")
    for rel in releases:
        rel_version = (rel.get("version") or "").strip().lstrip("vV")
        if rel_version and rel_version == inst:
            break
        out.append(rel)
    return out
