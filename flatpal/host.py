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
from typing import List, Sequence


_SANDBOX_MARKER = "/.flatpak-info"


def is_sandboxed() -> bool:
    """True iff this process is running inside a Flatpak sandbox."""
    return os.path.exists(_SANDBOX_MARKER)


def host_cmd(cmd: Sequence[str]) -> List[str]:
    """Return argv to run `cmd` on the host.

    Inside the sandbox: prefixes `flatpak-spawn --host`.
    On the host: returns a copy of `cmd` unchanged.
    """
    if is_sandboxed():
        return ["flatpak-spawn", "--host", *cmd]
    return list(cmd)
