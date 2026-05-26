"""Run `flatpak update` for a single app from a worker thread.

System installs trigger a polkit prompt; user installs don't. The
`flatpak-spawn --host` prefix from `host_cmd` lets the prompt surface on
the host session bus when Flatpal is sandboxed (same path the
no-enumerate fix uses).

Subprocess timeout is 10 minutes; a single large app (Inkscape, Blender)
can take several minutes on a slow link, so a short cap would falsely
fail. Worker threads in `detail.py` keep the UI responsive in the
meantime.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional, Tuple

from .host import host_cmd


_TIMEOUT_SECONDS = 600


def run_update(app_id: str, scope: str) -> Tuple[bool, Optional[str]]:
    """Run `flatpak update -y --noninteractive` for `app_id` in `scope`.

    `scope` is "system" or "user"; anything else falls back to "system"
    so the caller can pass `app.get("installation") or "system"` without
    branching. Returns `(True, None)` on success; `(False, msg)` on
    failure, where `msg` is the stderr tail or the captured exception
    text.
    """
    return _run(_default_runner, app_id, scope)


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
) -> Tuple[bool, Optional[str]]:
    """Indirection so tests can supply a canned subprocess result."""
    scope_flag = "--user" if scope == "user" else "--system"
    argv = host_cmd([
        "flatpak", "update", "-y", "--noninteractive", scope_flag, app_id,
    ])
    try:
        res = runner(argv)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if res.returncode != 0:
        tail = _last_nonempty_line(res.stderr) or _last_nonempty_line(res.stdout)
        return False, tail or f"flatpak update exited {res.returncode}"
    return True, None


def _last_nonempty_line(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s:
            return s
    return None
