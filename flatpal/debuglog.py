"""Tiny file-based debug log, opt-in via `FLATPAL_DEBUG=1`.

Plain stdout doesn't help once Flatpal is launched from GNOME Shell; the
journal may not capture it and there's no terminal attached. When opted in,
logging goes to `$XDG_STATE_HOME/flatpal/debug.log` which works regardless of
how the app was started and survives across launches; inside the Flatpak
sandbox that resolves to `~/.var/app/io.github.hawwwran.flatpal/.local/state/...`
which the host can read directly.

Off by default: the catalog parse path emits thousands of DEBUG records per
Explore-tab load and every record is an `fsync`'d write through
`RotatingFileHandler`, which adds noticeable latency on slow disks. Enable
with `FLATPAL_DEBUG=1` when actually debugging, e.g.

    FLATPAL_DEBUG=1 flatpak run io.github.hawwwran.flatpal
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


ENV_FLAG = "FLATPAL_DEBUG"


def _log_path() -> Path:
    base = (
        os.environ.get("XDG_STATE_HOME")
        or os.path.expanduser("~/.local/state")
    )
    return Path(base) / "flatpal" / "debug.log"


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


_configured = False


def setup() -> None:
    """Idempotent. Call once at app startup."""
    global _configured
    if _configured:
        return
    _configured = True
    if not _truthy(os.environ.get(ENV_FLAG, "")):
        return
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    handler = RotatingFileHandler(
        path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger = logging.getLogger("flatpal")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    logger.info("debug log opened at %s", path)


def log(msg, *args, **kwargs):
    """Forward to the flatpal logger. No-op unless FLATPAL_DEBUG is set."""
    logging.getLogger("flatpal").debug(msg, *args, **kwargs)
