"""Updates tab: every installed app with a pending Flathub update.

Data flows from the window-level updates dict (populated by the
background `flatpak remote-ls --updates` worker) joined against the
cached installed-app list. Per-row Update buttons reuse the shared
runner in `update_runner`; a successful update removes the row and
drops the window's dict entry so badges across the other tabs fall in
lockstep.
"""

from __future__ import annotations

import threading
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .update_runner import run_update
from .widgets import install_pill_css, make_list_clamp, make_status_label


def _compose_rows(installed_apps: list, updates: dict) -> List[dict]:
    """Join installed apps against the updates dict and sort by name.

    Pure function — extracted so the merge/sort logic is testable without
    GTK. Each output dict carries every field `UpdateRow` needs:
    `id`, `name`, `current`, `new`, `origin`, `scope`, `branch`,
    plus the full `app` dict for the row-click handler.

    Drops entries on either side that don't have a counterpart: an update
    record without an installed app (uninstall-since-fetch race) and an
    installed app without an update record (most of them).
    """
    by_id = {a["id"]: a for a in installed_apps}
    out: List[dict] = []
    for app_id, info in updates.items():
        app = by_id.get(app_id)
        if app is None:
            continue
        scope = "user" if app.get("installation") == "user" else "system"
        out.append({
            "id": app_id,
            "name": app.get("name") or app_id,
            "current": app.get("version") or "?",
            "new": info.get("version") or "?",
            "origin": info.get("origin") or "remote",
            "scope": scope,
            "branch": app.get("branch") or "",
            "app": app,
        })
    out.sort(key=lambda r: (r["name"].lower(), r["id"]))
    return out


class UpdateRow(Adw.ActionRow):
    def __init__(
        self,
        spec: dict,
        listbox: Gtk.ListBox,
        parent_window: Gtk.Window,
        on_row_activated: Callable[[dict], None],
    ):
        super().__init__()
        self._spec = spec
        self._listbox = listbox
        self._parent_window = parent_window
        self._on_row_activated = on_row_activated

        app_id = spec["id"]
        self.set_title(GLib.markup_escape_text(spec["name"]))
        subtitle_bits = [app_id]
        if spec["branch"] and spec["branch"] != "stable":
            subtitle_bits.append(spec["branch"])
        self.set_subtitle(GLib.markup_escape_text(" • ".join(subtitle_bits)))
        self.set_title_lines(1)
        self.set_subtitle_lines(1)
        self.set_activatable(True)

        icon = Gtk.Image.new_from_icon_name(app_id)
        icon.set_pixel_size(48)
        if not Gtk.IconTheme.get_for_display(self.get_display()).has_icon(app_id):
            icon.set_from_icon_name("application-x-executable")
        self.add_prefix(icon)

        diff = Gtk.Label(label=f"{spec['current']} → {spec['new']}")
        diff.add_css_class("numeric")
        diff.add_css_class("heading")
        diff.set_valign(Gtk.Align.CENTER)
        diff.set_tooltip_text(f"on {spec['origin']}")
        self.add_suffix(diff)

        self._button = Gtk.Button(label="Update")
        self._button.add_css_class("pill")
        self._button.add_css_class("flatpal-update-button")
        self._button.set_valign(Gtk.Align.CENTER)
        self._button.set_tooltip_text(
            f"flatpak update --{spec['scope']} {app_id}"
        )
        self._button.connect("clicked", self._on_update_clicked)
        self.add_suffix(self._button)

    def open_detail(self) -> None:
        self._on_row_activated(self._spec["app"])

    def _on_update_clicked(self, _btn: Gtk.Button) -> None:
        # Sub-frame double-click guard matches `DetailPage._on_update_clicked`.
        if not self._button.get_sensitive():
            return
        self._button.set_sensitive(False)
        self._button.set_label("Updating…")

        app_id = self._spec["id"]
        scope = self._spec["scope"]

        def worker() -> None:
            ok, err = run_update(app_id, scope)
            GLib.idle_add(self._finish_update, ok, err)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_update(self, ok: bool, err: Optional[str]) -> bool:
        if ok:
            # Clear the window-level entry first; the repaint it triggers
            # will re-render this page, which removes the row anyway. We
            # also remove explicitly so the row goes away even if some
            # future change makes the repaint skip this widget.
            self._parent_window.clear_update(self._spec["id"])
            parent = self.get_parent()
            if parent is self._listbox:
                self._listbox.remove(self)
        else:
            # If a concurrent refresh re-rendered the list while the worker
            # was in flight, this row is now unparented; restoring its
            # button is pointless (a fresh row already shows the default
            # "Update" state). The dialog still surfaces because the user
            # who clicked needs to see the failure.
            if self.get_parent() is not None:
                self._button.set_sensitive(True)
                self._button.set_label("Update")
            dialog = Adw.AlertDialog(
                heading="Update failed",
                body=(
                    f"Could not update {self._spec['name']}: "
                    + (err or "unknown error")
                ),
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present(self._parent_window)
        return False


class UpdatesPage(Gtk.Box):
    """Right-most tab listing every installed app with a pending update.

    The data source is the window-level `_updates` dict. The page does
    no subprocess work of its own; `notify_refreshing(True)` is called
    by the window while a fresh `flatpak remote-ls --updates` is in
    flight so the page can flip to the loading state.
    """

    def __init__(
        self,
        on_row_activated: Callable[[dict], None],
        get_installed_apps: Callable[[], list],
        updates_lookup: Callable[[str], Optional[dict]],
        parent_window: Gtk.Window,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        install_pill_css()
        self._on_row_activated = on_row_activated
        self._get_installed_apps = get_installed_apps
        self._updates_lookup = updates_lookup
        self._parent_window = parent_window
        # Initial state: pretend we're refreshing until the window tells us
        # the first fetch has landed (via `refresh()`). Prevents a flash of
        # the "Up to date" empty state during the ~2 s startup fetch.
        self._refreshing = True
        self._loaded = False

        self.status_label = make_status_label()

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_box.set_margin_top(4)
        status_box.set_margin_bottom(4)
        status_box.set_margin_start(12)
        status_box.set_margin_end(12)
        status_box.append(self.status_label)
        self.append(make_list_clamp(status_box))

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)

        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        loading_box.set_valign(Gtk.Align.CENTER)
        loading_box.set_halign(Gtk.Align.CENTER)
        loading_box.set_vexpand(True)
        spinner = Gtk.Spinner()
        spinner.set_size_request(36, 36)
        spinner.start()
        loading_label = Gtk.Label(label="Checking for updates…")
        loading_label.add_css_class("dim-label")
        loading_box.append(spinner)
        loading_box.append(loading_label)
        self.stack.add_named(loading_box, "loading")

        empty = Adw.StatusPage(
            icon_name="emblem-default-symbolic",
            title="Up to date",
            description=(
                "All installed apps are at the latest version available "
                "from their remotes."
            ),
        )
        empty.set_vexpand(True)
        self.stack.add_named(empty, "empty")

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
        self.listbox.set_hexpand(True)
        self.listbox.connect("row-activated", self._on_listbox_row_activated)
        scrolled.set_child(make_list_clamp(self.listbox))
        self.stack.add_named(scrolled, "list")

        self.stack.set_visible_child_name("loading")
        self.append(self.stack)

    def refresh(self) -> None:
        """Re-render rows from current data. State-only — no subprocess work.

        Marks the page as loaded and clears the refreshing flag, so a
        first call from "loading" lands on either the list or the empty
        state and a subsequent call after an in-flight refresh resolves
        cleanly.
        """
        self._loaded = True
        self._refreshing = False
        self._render()

    def notify_refreshing(self, active: bool) -> None:
        """Flip into the "Checking for updates…" loading state.

        Active while the window's background fetch is in flight. The
        landing call to `refresh()` clears the flag and re-renders.
        """
        self._refreshing = active
        self._render()

    def _on_listbox_row_activated(self, _listbox, row) -> None:
        if isinstance(row, UpdateRow):
            row.open_detail()

    def _render(self) -> None:
        if self._refreshing or not self._loaded:
            self.stack.set_visible_child_name("loading")
            self.status_label.set_label("Checking for updates…")
            return

        updates = self._current_updates()
        installed_apps = self._get_installed_apps()
        rows = _compose_rows(installed_apps, updates)

        # Rebuild listbox children. Cheap; N is typically <20.
        child = self.listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt

        if not rows:
            self.stack.set_visible_child_name("empty")
            self.status_label.set_label("")
            return

        for spec in rows:
            self.listbox.append(UpdateRow(
                spec=spec,
                listbox=self.listbox,
                parent_window=self._parent_window,
                on_row_activated=self._on_row_activated,
            ))

        n = len(rows)
        self.status_label.set_label(
            f"{n} app{'s' if n != 1 else ''} with pending updates"
        )
        self.stack.set_visible_child_name("list")

    def _current_updates(self) -> dict:
        """Reconstruct the full updates dict from the lookup.

        Iterates installed apps; the lookup yields the per-app record
        for the ones with pending updates. Avoids reaching into a
        private window attribute and keeps the page's view consistent
        with what the badges across the other tabs see.
        """
        out: dict = {}
        for app in self._get_installed_apps():
            info = self._updates_lookup(app["id"])
            if info is not None:
                out[app["id"]] = info
        return out
