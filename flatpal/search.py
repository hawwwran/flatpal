"""Search/filter helpers for both tabs. Pure logic, no GTK."""

from __future__ import annotations

from typing import Iterable, List, Optional, Set


# Order matters: tested first → fastest reject path.
INSTALLED_FIELDS = ("name", "id", "developer_name", "summary")
CATALOG_FIELDS = ("name", "id", "developer_name", "summary")


def _normalise(text: Optional[str]) -> str:
    return (text or "").casefold()


def _value_for(entry: dict, key: str) -> str:
    """Best-effort field lookup across installed/catalog dict shapes."""
    if key == "summary":
        return _normalise(entry.get("summary") or entry.get("name"))
    if key == "developer_name":
        return _normalise(entry.get("developer_name"))
    return _normalise(entry.get(key))


def matches(entry: dict, query: str, fields: Iterable[str] = INSTALLED_FIELDS) -> bool:
    """Lowercase substring match. Empty query → match everything."""
    if not query:
        return True
    q = _normalise(query)
    for field in fields:
        if q in _value_for(entry, field):
            return True
    return False


def filter_installed(apps: List[dict], query: str) -> List[dict]:
    """Filter an installed-apps list by `query`. Empty query → original list."""
    if not query.strip():
        return list(apps)
    return [a for a in apps if matches(a, query, INSTALLED_FIELDS)]


def search_catalog(
    catalog: dict,
    installed_ids: Set[str],
    query: str,
    limit: int = 50,
    sort_by: str = "name",
    popularity_idx: Optional[dict] = None,
) -> List[dict]:
    """Search the catalog for `query`, return up to `limit` rows.

    Empty query returns `[]` (the Explore tab shows a placeholder for that
    state — the catalog has thousands of entries; an unfiltered page would
    just be noise).

    Each returned dict is the catalog entry plus:
      - `installed: bool` — true when the app is already installed locally
      - `popularity` (only when `popularity_idx` is provided) — the index
        entry (`rank`, `installs_last_month`, …) or None for unranked apps.

    `sort_by="name"` sorts casefolded alphabetically. `sort_by="popularity"`
    uses the supplied index: ranked apps come first in rank order, then
    unranked apps alphabetically.
    """
    q = (query or "").strip()
    if not q:
        return []

    out: List[dict] = []
    for app_id, entry in catalog.items():
        if matches(entry, q, CATALOG_FIELDS):
            row = dict(entry)
            row["installed"] = app_id in installed_ids
            if popularity_idx is not None:
                row["popularity"] = popularity_idx.get(app_id)
            out.append(row)

    if sort_by == "popularity" and popularity_idx is not None:
        out.sort(key=_popularity_sort_key)
    else:
        out.sort(key=lambda r: _normalise(r.get("name")))

    if len(out) > limit:
        out = out[:limit]
    return out


def _popularity_sort_key(row: dict):
    pop = row.get("popularity")
    if pop is None:
        return (1, 0, _normalise(row.get("name")))  # unranked → after all ranked
    return (0, pop["rank"], _normalise(row.get("name")))


def popular_shelf(
    popularity_hits: list,
    catalog: dict,
    installed_ids: Set[str],
    limit: int = 20,
) -> List[dict]:
    """Build the top-N popular apps as catalog rows for the empty-state shelf.

    Joins Flathub popularity hits against the local catalog (so we get the
    same Adw-renderable entry shape with cached_icon etc.). Apps that are in
    popularity but missing from the catalog are skipped silently.
    """
    out: List[dict] = []
    for i, hit in enumerate(popularity_hits):
        app_id = hit.get("app_id")
        if not app_id:
            continue
        entry = catalog.get(app_id)
        if entry is None:
            continue
        row = dict(entry)
        row["installed"] = app_id in installed_ids
        row["popularity"] = {
            "rank": i + 1,
            "installs_last_month": int(hit.get("installs_last_month") or 0),
            "favorites_count": int(hit.get("favorites_count") or 0),
            "trending": float(hit.get("trending") or 0.0),
        }
        out.append(row)
        if len(out) >= limit:
            break
    return out
