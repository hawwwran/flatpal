"""Installed-apps tab. Owns the list, sort and search state for installed apps."""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .core import fetch_apps, format_date, sort_apps
from .metainfo import load_metainfo, system_lang
from .search import filter_installed
from .widgets import (
    clear_listbox,
    make_list_clamp,
    make_sort_pill,
    make_status_label,
    make_update_pill,
    update_tooltip,
)


_SORT_LABELS = {"name": "name", "date": "install date", "size": "size"}


def enrich_with_metainfo(
    apps: Iterable[dict],
    lang: Optional[str] = None,
    loader: Callable[[str, Optional[str]], dict] = load_metainfo,
) -> List[dict]:
    """Add `summary` and `developer_name` to each installed-app dict from its
    on-disk AppStream metainfo. `core.fetch_apps` builds rows from
    `flatpak list` columns only, so without this enrichment the Installed
    tab's search box can't match summary/developer text. Mutates in place.

    `loader` is injectable for unit-testing without filesystem access.
    """
    out: List[dict] = []
    for app in apps:
        meta = loader(app["id"], lang)
        app["summary"] = meta.get("summary") or ""
        app["developer_name"] = meta.get("developer_name") or ""
        out.append(app)
    return out


class AppRow(Adw.ActionRow):
    def __init__(self, app: dict, update_info: Optional[dict] = None):
        super().__init__()
        self.app = app
        self.set_title(GLib.markup_escape_text(app["name"]))
        subtitle_bits = [app["id"]]
        if app["branch"] and app["branch"] != "stable":
            subtitle_bits.append(app["branch"])
        self.set_subtitle(GLib.markup_escape_text(" • ".join(subtitle_bits)))
        self.set_title_lines(1)
        self.set_subtitle_lines(1)
        self.set_activatable(True)

        icon = Gtk.Image.new_from_icon_name(app["id"])
        icon.set_pixel_size(48)
        if not Gtk.IconTheme.get_for_display(self.get_display()).has_icon(app["id"]):
            icon.set_from_icon_name("application-x-executable")
        self.add_prefix(icon)

        if update_info:
            self.add_suffix(make_update_pill(
                tooltip=update_tooltip(app.get("version"), update_info),
            ))

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_valign(Gtk.Align.CENTER)
        meta.set_halign(Gtk.Align.END)

        def small(text, *extra_css):
            lbl = Gtk.Label(label=text)
            lbl.set_halign(Gtk.Align.END)
            lbl.add_css_class("numeric")
            lbl.add_css_class("caption")
            for c in extra_css:
                lbl.add_css_class(c)
            return lbl

        meta.append(small(app["version"] or "—"))
        meta.append(small(app["size_str"] or "—", "dim-label"))
        meta.append(small(format_date(app["installed"]), "dim-label"))
        self.add_suffix(meta)


class InstalledPage(Gtk.Box):
    def __init__(
        self, on_row_activated,
        updates_lookup: Optional[Callable[[str], Optional[dict]]] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.apps: list = []
        self.sort_key = "date"
        self.reverse = True
        self.query = ""
        self._on_row_activated = on_row_activated
        self._updates_lookup = updates_lookup or (lambda _id: None)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(
            "Filter installed apps by name, ID, developer or summary"
        )
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_search_changed)

        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_child(self.search_entry)
        self.search_bar.set_search_mode(True)
        self.search_bar.set_show_close_button(False)
        self.search_bar.connect_entry(self.search_entry)
        self.append(make_list_clamp(self.search_bar))

        self.status_label = make_status_label()

        # Brand-purple sort pill shared with the other tabs.
        self.sort_pill = make_sort_pill()
        self.sort_pill.set_visible(False)  # off until the first _render run

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_box.set_margin_top(4)
        status_box.set_margin_bottom(4)
        # Same horizontal inset as the listbox below so the status text lines
        # up with the row cards instead of floating 6px to their right.
        status_box.set_margin_start(12)
        status_box.set_margin_end(12)
        status_box.append(self.status_label)
        status_box.append(self.sort_pill)
        self.append(make_list_clamp(status_box))

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_top(8)
        self.listbox.set_margin_bottom(12)
        self.listbox.set_margin_start(12)
        self.listbox.set_margin_end(12)
        # Expand to fill the clamp's full width so the row cards align with
        # the search bar and status label above.
        self.listbox.set_hexpand(True)
        self.listbox.connect("row-activated", self._on_listbox_row_activated)

        scrolled.set_child(make_list_clamp(self.listbox))
        self.append(scrolled)

    def set_sort(self, key: str, reverse: bool) -> None:
        self.sort_key = key
        self.reverse = reverse
        self._render()

    def reload(self) -> None:
        apps = fetch_apps()
        # AppStream metainfo populates summary + developer_name so the search
        # placeholder ("name, ID, developer or summary") is honest.
        enrich_with_metainfo(apps, lang=system_lang())
        self.apps = apps
        self._render()

    def refresh(self) -> None:
        """Re-render the list against current state without re-fetching apps.

        Cheap entry point invoked when the per-app update lookup changes
        (the background `flatpak remote-ls --updates` worker lands) so the
        Update badges fade in without paying for another `flatpak list`.
        """
        self._render()

    def installed_ids(self) -> set:
        return {a["id"] for a in self.apps}

    def _on_search_changed(self, entry):
        self.query = entry.get_text()
        self._render()

    def _on_listbox_row_activated(self, _listbox, row):
        if hasattr(row, "app"):
            self._on_row_activated(row.app)

    def _render(self):
        clear_listbox(self.listbox)

        filtered = filter_installed(self.apps, self.query)
        ordered = sort_apps(filtered, self.sort_key, self.reverse)
        for a in ordered:
            self.listbox.append(AppRow(a, update_info=self._updates_lookup(a["id"])))

        total = len(self.apps)
        visible = len(ordered)
        arrow = "↓" if self.reverse else "↑"
        sort_label = _SORT_LABELS.get(self.sort_key, self.sort_key)
        if self.query.strip():
            self.status_label.set_label(f"{visible} of {total} apps")
        else:
            self.status_label.set_label(
                f"{total} app{'s' if total != 1 else ''}"
            )
        self.sort_pill.set_label(f"sorted by {sort_label} {arrow}")
        self.sort_pill.set_visible(total > 0)
