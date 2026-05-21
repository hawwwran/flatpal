"""On-disk cache for screenshot images.

The detail page asks for the same screenshot URL repeatedly across visits; we
download once and reuse. Cache lives under $XDG_CACHE_HOME/flatpal/screenshots/
(== ~/.cache/flatpal/screenshots/ when the variable isn't set).
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


def _default_cache_dir() -> Path:
    """XDG-respecting cache directory.

    Inside the Flatpak sandbox Flatpak sets $XDG_CACHE_HOME to the per-app
    persistent location (`~/.var/app/<APP_ID>/cache` on the host); on the
    raw host (`./install.sh` dev mode) it's typically unset so we fall back
    to `~/.cache`. Hardcoding `~/.cache` worked in dev but inside the
    sandbox writes land in an ephemeral overlay over a non-existent
    `~/.cache` and never persist — so downloaded screenshots got re-fetched
    every detail-page visit and stale-cache cleanup did nothing.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "flatpal" / "screenshots"


CACHE_DIR = _default_cache_dir()

_VALID_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif"}

# Refuse downloads larger than this. AppStream screenshots are routinely
# 1–3 MB; anything above ~10 MB is either a misbehaving server or hostile.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024

# Only accept responses whose Content-Type begins with one of these. Catches
# the common failure mode of a 404/login-wall HTML page being saved as a
# bogus PNG.
_ALLOWED_CONTENT_PREFIXES = ("image/", "application/octet-stream")


def _extension_for(url: str) -> str:
    """Pick a sensible file extension from a screenshot URL."""
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in _VALID_EXTS else ".img"


def screenshot_cache_path(app_id: str, url: str) -> Path:
    """Deterministic cache path for (app_id, url). Does not create the file."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    ext = _extension_for(url)
    return CACHE_DIR / app_id / f"{digest}{ext}"


def download_screenshot(
    url: str,
    dest: Path,
    timeout: float = 10.0,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> bool:
    """Download `url` into `dest` atomically. Returns True on success.

    Bails out (returning False) if:
      - the Content-Type header isn't image/* (e.g. an HTML error page)
      - the response body exceeds `max_bytes`
      - the network or filesystem raises an error

    Uses a temp file in the same directory and `os.replace` for atomicity.
    Critically, the destination directory is created **only after** the
    response is validated — so a rejected download doesn't leave behind an
    empty `~/.cache/flatpal/screenshots/<app-id>/` shell to be cleaned up later.
    """
    from . import __version__ as _ver  # local import keeps cache.py free of circulars

    tmp = None  # set only on the success path; cleanup checks for None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"Flatpal/{_ver} (+local)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype and not any(ctype.startswith(p) for p in _ALLOWED_CONTENT_PREFIXES):
                return False
            # Read at most `max_bytes + 1` so we can detect oversize without
            # buffering the entire monstrosity.
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return False
        # All checks passed → only now create the directory and write.
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        return True
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def get_cached_or_download(app_id: str, url: str) -> Optional[Path]:
    """Return a usable local path, downloading if absent. None on failure."""
    path = screenshot_cache_path(app_id, url)
    if path.is_file() and path.stat().st_size > 0:
        return path
    if download_screenshot(url, path):
        return path
    return None


def prune_cache(max_total_bytes: int, root: Optional[Path] = None) -> int:
    """Drop oldest-by-mtime screenshot files until the cache fits the budget.

    Returns the number of bytes removed. Files are deleted oldest-first; the
    parent app-id directories are removed when they're left empty. Safe to
    call on a missing cache (does nothing). Errors are swallowed — the cache
    is best-effort and we'd rather grow than crash the app.
    """
    cache_root = root if root is not None else CACHE_DIR
    if not cache_root.exists():
        return 0

    files = []
    try:
        for app_dir in cache_root.iterdir():
            if not app_dir.is_dir():
                continue
            for f in app_dir.iterdir():
                if f.is_file():
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    files.append((st.st_mtime, st.st_size, f))
    except OSError:
        return 0

    total = sum(size for _, size, _ in files)
    if total <= max_total_bytes:
        return 0

    files.sort(key=lambda t: t[0])  # oldest first

    removed = 0
    for mtime, size, path in files:
        if total <= max_total_bytes:
            break
        try:
            path.unlink()
            total -= size
            removed += size
        except OSError:
            continue

    # Sweep empty app directories.
    try:
        for app_dir in cache_root.iterdir():
            if app_dir.is_dir():
                try:
                    next(app_dir.iterdir())
                except StopIteration:
                    try:
                        app_dir.rmdir()
                    except OSError:
                        pass
                except OSError:
                    pass
    except OSError:
        pass

    return removed
