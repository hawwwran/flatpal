"""Persistent app settings.

A small dict round-tripped through `$XDG_CONFIG_HOME/flatpal/settings.json`
(== `~/.config/flatpal/settings.json` when the variable isn't set). Pure
helpers — no GTK, easily tested. Failures are silent: missing/corrupt files
yield defaults, write errors are swallowed (a missing setting beats a
crashed app).

Add a new key by extending `DEFAULTS`; old settings files merge with the
defaults so users don't lose state when we add fields.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def _default_path() -> Path:
    """XDG-respecting settings file location.

    Inside the Flatpak sandbox `$XDG_CONFIG_HOME` is set to the per-app
    persistent `~/.var/app/<APP_ID>/config`; the bare `~/.config` path
    lands in an ephemeral overlay so every launch reverted to defaults
    (last tab, sort order, "show popular" toggle).
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "flatpal" / "settings.json"


DEFAULT_PATH = _default_path()

DEFAULTS: Dict[str, Any] = {
    # Which tab was visible when the window was last closed. On launch we
    # restore this so the user lands where they left off.
    "last_tab": "installed",
    # Per-tab sort state, persisted across launches.
    "installed_sort_key": "date",  # name | date | size
    "installed_reverse": True,
    "explore_sort_key": "popularity",  # popularity | name
    "running_sort_key": "cpu",  # cpu | memory | name
    # How often the Running tab samples while visible.
    "running_refresh_seconds": 2,
    # When True, Explore fetches Flathub popularity and shows the
    # "Popular this month" shelf in the empty-search state. When False,
    # those network calls are skipped and the shelf is hidden; local
    # AppStream catalog search still works.
    "show_popular": True,
}


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    """Return the merged settings dict. Missing/corrupt file → defaults."""
    target = path if path is not None else DEFAULT_PATH
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return dict(DEFAULTS)
    # Merge so unknown keys persist and missing keys get defaults.
    merged: Dict[str, Any] = dict(DEFAULTS)
    if isinstance(stored, dict):
        merged.update(stored)
    return merged


def save(settings: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Persist the dict atomically. Silent on filesystem errors."""
    target = path if path is not None else DEFAULT_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_text(json.dumps(settings, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass
