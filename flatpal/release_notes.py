"""Two groups for the detail page:

- `build_recent_releases_placeholder` + `populate_recent_releases`:
  OSTree-commit history from `flatpak remote-info --log`, one row per
  commit with a Downgrade button. Two-step so the page can render the
  placeholder synchronously and fill in rows once the worker returns.

- `build_version_history_group`: AppStream `<releases>` metainfo with
  per-version release notes. No actions, just the upstream changelog.

The top-of-page update card lives in `detail.DetailPage._build_update_box`
so it can wire its "Update now" button straight into the page's state.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


_BUILD_HASH_SUFFIX = re.compile(r"\s*\([0-9a-fA-F]{4,}\)\s*$")


def _clean_subject(subject: str) -> str:
    """Strip the Flathub trailing `(deadbeef…)` build-id from a commit subject."""
    return _BUILD_HASH_SUFFIX.sub("", subject or "").strip() or "(no subject)"


def build_version_history_group(releases: list) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    group.set_title("Version history")
    group.set_description("Upstream release notes from AppStream metainfo.")

    for rel in releases:
        label = rel.get("version") or "—"
        subtitle = rel.get("date") or ""
        if rel.get("description_markup"):
            row = Adw.ExpanderRow()
            row.set_title(label)
            row.set_subtitle(subtitle)
            inner = Gtk.Label(label=rel["description_markup"])
            inner.set_wrap(True)
            inner.set_xalign(0.0)
            inner.set_margin_top(6)
            inner.set_margin_bottom(6)
            inner.set_margin_start(12)
            inner.set_margin_end(12)
            row.add_row(inner)
        else:
            row = Adw.ActionRow()
            row.set_title(label)
            row.set_subtitle(subtitle)
        group.add(row)

    return group


def build_recent_releases_placeholder() -> tuple[Adw.PreferencesGroup, Adw.ActionRow]:
    """Build the commits group with a single "Loading…" row.

    Pair with `populate_recent_releases` once the parsed log arrives.
    """
    group = Adw.PreferencesGroup()
    group.set_title("Recent releases")
    group.set_description("Switch to a specific commit from the remote.")

    loading = Adw.ActionRow()
    loading.set_title("Loading commit history…")
    loading.add_css_class("dim-label")
    group.add(loading)
    return group, loading


def populate_recent_releases(
    group: Adw.PreferencesGroup,
    placeholder: Optional[Adw.ActionRow],
    records: list,
    *,
    on_downgrade: Callable[[dict], None],
    current_commit: str = "",
) -> dict:
    """Replace the placeholder with one row per OSTree commit.

    Returns `{commit_hash: button}` so the caller can disable a specific
    button while a downgrade is in flight. The row whose commit matches
    `current_commit` gets a "Current" suffix instead of a button.
    """
    if placeholder is not None:
        group.remove(placeholder)

    if not records:
        empty = Adw.ActionRow()
        empty.set_title("No commit history available")
        empty.set_subtitle("The remote did not return a usable log.")
        empty.add_css_class("dim-label")
        group.add(empty)
        return {}

    buttons: dict[str, Gtk.Button] = {}
    current = (current_commit or "").strip().lower()

    for rec in records:
        commit = (rec.get("commit") or "").strip()
        if not commit:
            continue
        subject = _clean_subject(rec.get("subject") or "")
        short = commit[:12]
        date = rec.get("date_short") or ""

        row = Adw.ActionRow()
        row.set_title(subject)
        subtitle = " · ".join(s for s in (date, short) if s)
        row.set_subtitle(subtitle)

        if commit.lower() == current:
            marker = Gtk.Label(label="Current")
            marker.add_css_class("dim-label")
            marker.set_valign(Gtk.Align.CENTER)
            row.add_suffix(marker)
        else:
            button = Gtk.Button(label="Downgrade")
            button.add_css_class("flat")
            button.set_valign(Gtk.Align.CENTER)
            button.connect("clicked", lambda _b, r=rec: on_downgrade(r))
            row.add_suffix(button)
            buttons[commit] = button

        group.add(row)

    return buttons
