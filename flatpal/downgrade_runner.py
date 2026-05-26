"""Run `flatpak update --commit=<hash>` (and optionally `flatpak mask`)
for a single app from a worker thread.

System installs are wrapped in `pkexec`: flatpak's polkit "update"
privilege doesn't cover rewinding to an arbitrary commit, so a plain
`flatpak update --commit=<hash> --system` fails with "Can't update to a
specific commit without root permissions". Same constraint applies to
`flatpak mask --system`. User installs operate on the user's own
ostree repo under `~/.local/share/flatpak` and need no elevation.

Two-step semantics: the mask is best-effort and only attempted on a
successful downgrade. A failed mask after a successful downgrade is
surfaced as a partial-success state so the UI can say "downgraded, but
the next update may roll it forward again".
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional, Tuple

from .host import host_cmd


_TIMEOUT_SECONDS = 600


def run_downgrade(
    app_id: str, scope: str, commit: str, mask_after: bool,
) -> Tuple[bool, Optional[str], bool]:
    """Run `flatpak update --commit=<commit>` for `app_id` in `scope`.

    If `mask_after` is true and the downgrade succeeds, also run
    `flatpak mask`. Returns `(ok, err, masked)`:
      (True,  None,     False) - downgrade ok, mask not requested
      (True,  None,     True)  - downgrade ok, mask ok
      (True,  mask_err, False) - downgrade ok, mask requested but failed
      (False, err,      False) - downgrade failed (mask never runs)
    """
    return _run(_default_runner, app_id, scope, commit, mask_after)


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
    commit: str,
    mask_after: bool,
) -> Tuple[bool, Optional[str], bool]:
    """Indirection so tests can supply canned subprocess results."""
    is_user = scope == "user"
    scope_flag = "--user" if is_user else "--system"
    elevate = [] if is_user else ["pkexec"]

    downgrade_argv = host_cmd([
        *elevate, "flatpak", "update", "-y", "--noninteractive",
        scope_flag, f"--commit={commit}", app_id,
    ])
    try:
        res = runner(downgrade_argv)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc), False
    if res.returncode != 0:
        tail = _last_nonempty_line(res.stderr) or _last_nonempty_line(res.stdout)
        return False, tail or f"flatpak update exited {res.returncode}", False

    if not mask_after:
        return True, None, False

    mask_argv = host_cmd([
        *elevate, "flatpak", "mask", scope_flag, app_id,
    ])
    try:
        res = runner(mask_argv)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return True, str(exc), False
    if res.returncode != 0:
        tail = _last_nonempty_line(res.stderr) or _last_nonempty_line(res.stdout)
        return True, tail or f"flatpak mask exited {res.returncode}", False
    return True, None, True


def run_unmask(app_id: str, scope: str) -> Tuple[bool, Optional[str]]:
    """Run `flatpak mask --remove <app_id>` for `scope`.

    System scope goes through `pkexec` for the same reason `flatpak mask`
    does on the way in. Returns `(True, None)` on success; `(False, err)`
    on failure.
    """
    return _run_unmask(_default_runner, app_id, scope)


def _run_unmask(
    runner: Callable[[list], subprocess.CompletedProcess],
    app_id: str,
    scope: str,
) -> Tuple[bool, Optional[str]]:
    is_user = scope == "user"
    scope_flag = "--user" if is_user else "--system"
    elevate = [] if is_user else ["pkexec"]
    argv = host_cmd([
        *elevate, "flatpak", "mask", "--remove", scope_flag, app_id,
    ])
    try:
        res = runner(argv)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if res.returncode != 0:
        tail = _last_nonempty_line(res.stderr) or _last_nonempty_line(res.stdout)
        return False, tail or f"flatpak mask --remove exited {res.returncode}"
    return True, None


def _last_nonempty_line(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s:
            return s
    return None
