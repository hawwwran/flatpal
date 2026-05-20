"""Installed-apps tab. Owns the list, sort and search state for installed apps."""

from __future__ import annotations

import locale
from typing import Callable, Iterable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .core import fetch_apps, format_date, sort_apps
from .metainfo import load_metainfo
from .search import filter_installed


# The listboxes below are wrapped in Adw.Clamp(900) for a tidy max-width
# layout. Everything above the list — search bar, status — should share the
# same constraint so the input box visually lines up with the list rows
# instead of stretching to the full window width.
LIST_MAX_WIDTH = 900


def _clamp_child(widget: Gtk.Widget) -> Adw.Clamp:
    """Wrap `widget` in an Adw.Clamp(max=LIST_MAX_WIDTH) so it lines up with
    the boxed-list rows below it."""
    clamp = Adw.Clamp()
    clamp.set_maximum_size(LIST_MAX_WIDTH)
    clamp.set_child(widget)
    return clamp


def _current_lang() -> Optional[str]:
    code, _ = locale.getlocale(locale.LC_MESSAGES)
    return code or None


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
    def __init__(self, app: dict):
        super().__init__()
        self.app = app
        self.set_title(GLib.markup_escape_text(app["name"]))
        subtitle_bits = [app["id"]]
        if app["branch"] and app["branch"] != "stable":
            subtitle_bits.append(app["branch"])
        self.set_subtitle(GLib.markup_escape_text(" • ".join(subtitle_bits)))
        self.set_activatable(True)

        icon = Gtk.Image.new_from_icon_name(app["id"])
        icon.set_pixel_size(48)
        if not Gtk.IconTheme.get_for_display(self.get_display()).has_icon(app["id"]):
            icon.set_from_icon_name("application-x-executable")
        self.add_prefix(icon)

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
    def __init__(self, on_row_activated, on_render=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.apps: list = []
        self.sort_key = "date"
        self.reverse = True
        self.query = ""
        self._on_row_activated = on_row_activated
        self._on_render = on_render

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
        self.append(_clamp_child(self.search_bar))

        self.status_label = Gtk.Label()
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_margin_top(4)
        self.status_label.set_margin_bottom(4)
        # Same horizontal inset as the listbox below so the status text lines
        # up with the row cards instead of floating 6px to their right.
        self.status_label.set_margin_start(12)
        self.status_label.set_margin_end(12)
        self.append(_clamp_child(self.status_label))

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
        # the search bar and status label above (which are clamped at 900
        # via _clamp_child).
        self.listbox.set_hexpand(True)
        self.listbox.connect("row-activated", self._on_listbox_row_activated)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(LIST_MAX_WIDTH)
        clamp.set_child(self.listbox)
        scrolled.set_child(clamp)
        self.append(scrolled)

    # ----- public API ------------------------------------------------------

    def set_sort(self, key: str, reverse: bool) -> None:
        self.sort_key = key
        self.reverse = reverse
        self._render()

    def reload(self) -> None:
        apps = fetch_apps()
        # AppStream metainfo populates summary + developer_name so the search
        # placeholder ("name, ID, developer or summary") is honest.
        enrich_with_metainfo(apps, lang=_current_lang())
        self.apps = apps
        self._render()

    def installed_ids(self) -> set:
        return {a["id"] for a in self.apps}

    # ----- internals -------------------------------------------------------

    def _on_search_changed(self, entry):
        self.query = entry.get_text()
        self._render()

    def _on_listbox_row_activated(self, _listbox, row):
        if hasattr(row, "app"):
            self._on_row_activated(row.app)

    def _clear_listbox(self):
        child = self.listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt

    def _render(self):
        self._clear_listbox()

        filtered = filter_installed(self.apps, self.query)
        ordered = sort_apps(filtered, self.sort_key, self.reverse)
        for a in ordered:
            self.listbox.append(AppRow(a))

        total = len(self.apps)
        visible = len(ordered)
        sort_label = {"name": "name", "date": "install date", "size": "size"}[self.sort_key]
        arrow = "↓" if self.reverse else "↑"
        if self.query.strip():
            self.status_label.set_label(
                f"{visible} of {total} apps · sorted by {sort_label} {arrow}"
            )
        else:
            self.status_label.set_label(
                f"{total} app{'s' if total != 1 else ''} · sorted by {sort_label} {arrow}"
            )

        if self._on_render:
            self._on_render()
