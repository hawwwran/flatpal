"""Flathub appstream catalog loader.

Parses the aggregated `appstream.xml.gz` shipped by Flatpak into a dict keyed
by app-id. Reuses `metainfo.parse_component` so catalog entries have the same
shape as installed-app metainfo dicts — only an extra `cached_icon` path is
added (pointing at the icon Flatpak already extracted to disk).
"""

from __future__ import annotations

import gzip
import io
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, List, Optional

from .metainfo import parse_component


# Flatpak ships the Flathub catalog at one of two locations depending on
# whether the user added the remote system-wide or `--user`. Architecture
# matches `os.uname().machine` (x86_64, aarch64, …).
_FLATPAK_BASES: tuple = (
    Path("/var/lib/flatpak"),
    Path(os.path.expanduser("~/.local/share/flatpak")),
)
ARCH = os.uname().machine
ICON_SIZES = ("128x128", "64x64")

_CACHE: dict = {}


def _catalog_candidates() -> List[Path]:
    """Possible appstream.xml.gz locations, in priority order (system first)."""
    return [
        base / "appstream" / "flathub" / ARCH / "active" / "appstream.xml.gz"
        for base in _FLATPAK_BASES
    ]


def _icon_root_candidates() -> List[Path]:
    """Possible icon-cache roots, parallel to the catalog locations."""
    return [
        base / "appstream" / "flathub" / ARCH / "active" / "icons"
        for base in _FLATPAK_BASES
    ]


# Back-compat: first system path. Kept so tests / external callers that
# imported the old constant still resolve.
CATALOG_PATH = _catalog_candidates()[0]
ICON_ROOT = _icon_root_candidates()[0]


def catalog_icon_path(
    app_id: str,
    icon_root: Optional[Path] = None,
    icon_roots: Optional[Iterable[Path]] = None,
) -> Optional[Path]:
    """Return the largest available cached icon for `app_id`, or None.

    `icon_root` is the legacy single-root override (still used by tests).
    `icon_roots` overrides the full search list.
    """
    if icon_root is not None:
        roots: Iterable[Path] = [icon_root]
    elif icon_roots is not None:
        roots = icon_roots
    else:
        roots = _icon_root_candidates()
    for root in roots:
        for size in ICON_SIZES:
            candidate = root / size / f"{app_id}.png"
            if candidate.is_file():
                return candidate
    return None


def parse_catalog(
    source,
    lang: Optional[str] = None,
    icon_root: Optional[Path] = None,
) -> dict:
    """Parse `<components>…</components>` XML into `{app_id: entry}`.

    `source` may be:
      - a `str` of XML text (tests, smaller inputs), or
      - a file-like object (production: a `gzip.open` stream of the on-disk
        catalog — ~50 MB decompressed).

    We use `ET.iterparse` with `elem.clear()` after each `<component>` so the
    peak memory stays in the low MBs even for the full Flathub catalog,
    instead of building one ~50 MB ElementTree in memory.

    Entries match `metainfo.parse_component` output plus:
      - `cached_icon`: Path to the largest cached PNG, or None.
    """
    if isinstance(source, str):
        source = io.BytesIO(source.encode("utf-8"))

    out: dict = {}
    # Canonical iterparse pattern: grab the root via the first `start` event
    # so we can both clear()-and-remove() each <component> after processing.
    # Without the remove(), empty shells stay attached to <components> and
    # iterparse's memory savings are partly undone (~400 KB on the full Flathub
    # catalog of ~4000 entries).
    it = ET.iterparse(source, events=("start", "end"))
    try:
        _, root_elem = next(it)  # opening <components>
    except StopIteration:
        return {}
    except ET.ParseError:
        return {}

    try:
        for event, elem in it:
            if event != "end" or elem.tag != "component":
                continue
            entry = parse_component(elem, lang)
            app_id = entry["id"]
            if app_id:
                entry["cached_icon"] = catalog_icon_path(app_id, icon_root=icon_root)
                out[app_id] = entry
            elem.clear()
            root_elem.remove(elem)
    except ET.ParseError:
        return {}
    return out


def load_catalog(
    lang: Optional[str] = None,
    *,
    path: Optional[Path] = None,
    force: bool = False,
) -> dict:
    """Read + parse the on-disk Flathub catalog. Memoized per-lang.

    `path` overrides the default search (system → user). When omitted we try
    each candidate in order and use the first one that opens; this covers
    machines where Flathub is added with `--user`.

    Returns an empty dict if no catalog is readable.
    """
    cache_key = (lang, str(path) if path else "auto")
    if not force and cache_key in _CACHE:
        return _CACHE[cache_key]

    candidates: List[Path] = [path] if path is not None else _catalog_candidates()
    parsed: Optional[dict] = None
    for target in candidates:
        try:
            # Stream the gzip output straight into iterparse so peak memory
            # stays bounded — the full catalog is ~50 MB decompressed.
            with gzip.open(target, "rb") as f:
                parsed = parse_catalog(f, lang=lang)
            break
        except (OSError, EOFError):
            continue

    if parsed is None:
        _CACHE[cache_key] = {}
        return _CACHE[cache_key]

    _CACHE[cache_key] = parsed
    return parsed


def clear_cache() -> None:
    """Drop the memoized catalog. Used by tests to keep instances isolated."""
    _CACHE.clear()
