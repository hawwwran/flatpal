"""Fetch + cache Flathub popularity rankings.

Top-1000 most-installed apps from Flathub's `/api/v2/collection/popular`,
fetched as four parallel 250-item pages so the slowest page caps wall-clock
time. Optional `on_progress` callback fires per page so the UI can render
progressively. Cached to disk with a 24-hour TTL; on network failure we fall
back to any stale cache.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional


from . import __version__ as _VERSION
from . import debuglog

POPULAR_URL = "https://flathub.org/api/v2/collection/popular"
DEFAULT_PER_PAGE = 250
DEFAULT_PAGES = 4


def _default_cache_path() -> Path:
    """XDG-respecting cache file for the popularity snapshot.

    Mirrors `cache._default_cache_dir`: inside the Flatpak sandbox
    `$XDG_CACHE_HOME` points at `~/.var/app/<APP_ID>/cache` (persistent);
    hardcoding `~/.cache` writes into an ephemeral overlay that never survives
    the sandbox exit, so every launch ended up re-fetching from Flathub.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "flatpal" / "flathub-popular.json"


DEFAULT_CACHE_PATH = _default_cache_path()
DEFAULT_TTL_SECONDS = 24 * 60 * 60
USER_AGENT = f"Flatpal/{_VERSION} (+local)"


def _fetch_page(page: int, per_page: int, timeout: float = 10.0) -> List[dict]:
    """Single page fetch. Raises urllib/value errors on failure."""
    qs = urllib.parse.urlencode({"page": page, "per_page": per_page})
    req = urllib.request.Request(
        f"{POPULAR_URL}?{qs}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    return list(payload.get("hits", []))


def _combine(page_hits: dict) -> List[dict]:
    """Concat pages in page-number order, then re-sort by install count desc.

    Sorting after concat handles any cross-page ties or quirks; Flathub already
    sorts per page by installs_last_month, so the merged sort is a no-op
    in the happy case.
    """
    combined: List[dict] = []
    for page in sorted(page_hits.keys()):
        combined.extend(page_hits[page])
    combined.sort(key=lambda h: -(int(h.get("installs_last_month") or 0)))
    return combined


def fetch_popular(
    per_page: int = DEFAULT_PER_PAGE,
    pages: int = DEFAULT_PAGES,
    timeout: float = 10.0,
    on_progress: Optional[Callable[[int, int, List[dict]], None]] = None,
) -> tuple:
    """Fetch the top `per_page * pages` apps in parallel.

    Returns `(hits, complete)`:
      - `hits`: combined, sorted list of all pages that succeeded.
      - `complete`: True iff *every* page succeeded. Partial successes return
        what they have with `complete=False` so the caller can avoid poisoning
        a persistent cache with an incomplete dataset.

    `on_progress(completed_pages, total_pages, partial_hits)` is invoked from
    a worker thread each time a page completes.

    Raises `ValueError` for non-positive `pages` / `per_page`, and the first
    error only if *every* page failed.
    """
    if pages <= 0 or per_page <= 0:
        raise ValueError(
            f"pages and per_page must be positive (got pages={pages}, per_page={per_page})"
        )
    page_hits: dict = {}
    errors: list = []

    def fetch_one(page: int):
        return page, _fetch_page(page, per_page, timeout)

    with ThreadPoolExecutor(max_workers=pages) as ex:
        futures = [ex.submit(fetch_one, p + 1) for p in range(pages)]
        for fut in as_completed(futures):
            try:
                page, hits = fut.result()
                page_hits[page] = hits
                if on_progress:
                    on_progress(len(page_hits), pages, _combine(page_hits))
            except Exception as exc:  # noqa: BLE001 collect all
                debuglog.log("popularity page fetch failed: %r", exc)
                errors.append(exc)

    if errors and not page_hits:
        raise errors[0]

    complete = len(page_hits) == pages and not errors
    return _combine(page_hits), complete


def _read_cache(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_cache(path: Path, hits: List[dict], fetched_at: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_text(
            json.dumps({"fetched_at": fetched_at, "hits": hits}),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass


def load_popular(
    cache_path: Path = DEFAULT_CACHE_PATH,
    max_age_seconds: int = DEFAULT_TTL_SECONDS,
    *,
    fetcher: Callable[..., object] = fetch_popular,
    now: Optional[Callable[[], float]] = None,
    on_progress: Optional[Callable[[int, int, List[dict]], None]] = None,
) -> List[dict]:
    """Return popularity hits. Cache-first, network on miss/stale.

    On any network failure return whatever is cached (even if stale); only
    when there's literally no cache do we return `[]`. The optional
    `on_progress` callback is forwarded to `fetcher` if the fetcher accepts it.

    The fetcher may return either:
      - a `(hits, complete)` tuple (current `fetch_popular`), OR
      - a plain `list` of hits (legacy / test stubs, treated as complete).

    Partial fetches (`complete=False`) are returned to the caller but NOT
    written to the cache: a half-loaded snapshot shouldn't suppress a retry
    on the next launch.
    """
    now_fn = now or time.time
    cached = _read_cache(cache_path)
    if cached:
        fetched_at = cached.get("fetched_at", 0)
        if (now_fn() - fetched_at) < max_age_seconds:
            return list(cached.get("hits", []))

    try:
        # Best-effort progress forwarding; some fetchers (e.g. in tests) won't
        # accept the kwarg, so try with-callback first and degrade gracefully.
        if on_progress is not None:
            try:
                result = fetcher(on_progress=on_progress)
            except TypeError:
                result = fetcher()
        else:
            result = fetcher()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return list(cached.get("hits", [])) if cached else []

    # Accept either tuple-return (new) or list-return (legacy).
    if isinstance(result, tuple) and len(result) == 2:
        hits, complete = result
    else:
        hits, complete = list(result), True

    if complete:
        _write_cache(cache_path, hits, fetched_at=now_fn())
    return hits


def popularity_index(hits: List[dict]) -> dict:
    """Map `app_id -> {rank, installs_last_month, trending, favorites_count}`."""
    out: dict = {}
    for i, h in enumerate(hits):
        app_id = h.get("app_id")
        if not app_id or app_id in out:
            continue
        out[app_id] = {
            "rank": i + 1,
            "installs_last_month": int(h.get("installs_last_month") or 0),
            "favorites_count": int(h.get("favorites_count") or 0),
            "trending": float(h.get("trending") or 0.0),
        }
    return out


def format_install_count(n: Optional[int]) -> str:
    """Compact install-count format: 1234 → '1.2k', 211771 → '212k', 1.2e6 → '1.2M'."""
    if not n:
        return ""
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{round(n / 1000)}k"
    return f"{n / 1_000_000:.1f}M"
