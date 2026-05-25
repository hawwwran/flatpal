"""Release-notes widgets: the top-of-page update card and the recent-releases group."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .updates import releases_since


def build_update_box(app: dict, meta: dict, update_info: dict) -> Gtk.Widget:
    """Terracotta-tinted callout: current vs new version + release notes.

    Lives at the top of the detail body (just under the hero) so the
    diff is the first thing the user sees on an updateable app. Release
    notes are inlined (not expanders) so the "what's new since
    installed" content is readable without an extra click.
    """
    new_v = update_info.get("version") or "?"
    origin = update_info.get("origin") or "remote"
    current = app.get("version") or "?"

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    outer.add_css_class("flatpal-update-card")

    header = Gtk.Label(label="Update available")
    header.set_xalign(0.0)
    header.add_css_class("title-3")
    outer.append(header)

    diff = Gtk.Label(label=f"{current} → {new_v}   ·   on {origin}")
    diff.set_xalign(0.0)
    diff.add_css_class("heading")
    outer.append(diff)

    # "What's new since v0.1.0" — release notes slice from the metainfo
    # for everything that landed after the installed version. Empty
    # `releases_since` (no release tags, or installed == latest known)
    # falls through to the "release notes unavailable" branch below so
    # the card doesn't look truncated under the version-diff header.
    new_releases = releases_since(meta.get("releases") or [], current)
    if new_releases:
        since = Gtk.Label(label=f"What's new since {current}")
        since.set_xalign(0.0)
        since.add_css_class("dim-label")
        since.add_css_class("caption-heading")
        since.set_margin_top(8)
        outer.append(since)

        for rel in new_releases:
            ver_label = Gtk.Label()
            ver_label.set_markup(
                f"<b>{GLib.markup_escape_text(rel['version'] or '—')}</b>"
                + (
                    f"  <span alpha='65%'>· {GLib.markup_escape_text(rel['date'])}</span>"
                    if rel.get("date") else ""
                )
            )
            ver_label.set_xalign(0.0)
            ver_label.set_margin_top(6)
            outer.append(ver_label)

            if rel.get("description_markup"):
                body = Gtk.Label(label=rel["description_markup"])
                body.set_wrap(True)
                body.set_xalign(0.0)
                body.add_css_class("body")
                outer.append(body)
    else:
        # No body to show: either current is "?" (rare — flatpak list
        # didn't report a version) or the metainfo's release list
        # doesn't extend past the installed version (catalog may lag
        # behind the remote, or upstream tags but doesn't update
        # metainfo on time). Either way, surface a one-liner so the
        # card doesn't look truncated under the bare version-diff line.
        if current == "?":
            msg = "Installed version unknown — open Software to view release notes."
        else:
            msg = "Release notes for the new version aren't available yet."
        note = Gtk.Label(label=msg)
        note.set_xalign(0.0)
        note.add_css_class("dim-label")
        note.set_wrap(True)
        note.set_margin_top(8)
        outer.append(note)

    return outer


def build_releases_group(releases: list) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    group.set_title("Recent releases")

    for rel in releases:
        label = rel["version"] or "—"
        subtitle = rel["date"] or ""
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
