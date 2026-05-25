"""Sandbox detection and `flatpak-spawn --host` command wrapper.

When Flatpal runs inside a Flatpak sandbox, every `flatpak …` subprocess
and every read of host /proc files needs to traverse the sandbox boundary
via `flatpak-spawn --host`. On the host (dev mode, `./install.sh`) the
prefix is unnecessary, so `host_cmd` passes the argv through unchanged.
The sandbox flag is checked once per call from /.flatpak-info, which is
created by flatpak when the sandbox starts.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence


_SANDBOX_MARKER = "/.flatpak-info"


def is_sandboxed() -> bool:
    """True iff this process is running inside a Flatpak sandbox."""
    return os.path.exists(_SANDBOX_MARKER)


def host_cmd(
    cmd: Sequence[str],
    env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Return argv to run `cmd` on the host.

    Inside the sandbox: prefixes `flatpak-spawn --host` and translates each
    item in `env` into a `--env=KEY=VAL` flag so the host command actually
    sees the variable. (Setting `env=` on the outer `subprocess.run` only
    affects the flatpak-spawn process itself, not the host command it
    ultimately spawns; that's why a Python-side `LC_ALL=C` doesn't reach
    a host `flatpak history`.)

    On the host: returns a copy of `cmd` unchanged; the caller is expected
    to forward `env` directly via `subprocess.run(env=...)`.
    """
    if is_sandboxed():
        env_args = [f"--env={k}={v}" for k, v in (env or {}).items()]
        return ["flatpak-spawn", "--host", *env_args, *cmd]
    return list(cmd)
