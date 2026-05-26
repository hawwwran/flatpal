"""Recent-releases group for the detail page.

The top-of-page update card lives in `detail.DetailPage._build_update_box`
so it can wire its "Update now" button straight into the page's state.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


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
