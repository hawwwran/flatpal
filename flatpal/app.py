"""GTK4 / libadwaita UI shell. Three tabs (Running, Installed, Explore) wired via Adw.ViewSwitcher."""

from __future__ import annotations

import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import settings as user_settings
from .cache import prune_cache
from .constants import SCREENSHOT_CACHE_MAX_BYTES
from .detail import DetailPage
from .explore_page import ExplorePage
from .installed_page import InstalledPage
from .no_enumerate import NoEnumerateCheck
from .running_page import RunningPage
from .updates import fetch_updates


APP_ID = "io.github.hawwwran.flatpal"


class FlatpalWindow(Adw.ApplicationWindow):
    def __init__(self, app, open_app_id: str | None = None):
        super().__init__(application=app)
        self.set_title("Flatpal")
        self.set_default_size(900, 720)

        # Per-app-id update info from `flatpak remote-ls --updates`. Populated
        # by a single background worker shortly after startup (~2.5 s), then
        # frozen for the rest of the session. Empty dict here means "lookup
        # returns None for every app id" — pages render no update badge
        # until the worker lands.
        self._updates: dict = {}
        self._updates_loaded = False

        # GTK's default icon-theme search path inside the Flatpak sandbox
        # is /app/share/icons + the runtime — it doesn't include the host's
        # per-app exported icons (one of which is, e.g. Black Box's icon).
        # Adding both system and user export trees so `IconTheme.has_icon()`
        # / `set_from_icon_name()` resolves Discord / Black Box / etc. on
        # the Running and Installed tabs. The Explore tab is unaffected —
        # it downloads its own thumbnails from the Flathub CDN. Outside the
        # sandbox the paths still exist on the host; add_search_path is a
        # no-op for non-existent directories so this is also safe in dev mode.
        icon_theme = Gtk.IconTheme.get_for_display(self.get_display())
        icon_theme.add_search_path("/var/lib/flatpak/exports/share/icons")
        icon_theme.add_search_path(
            os.path.expanduser("~/.local/share/flatpak/exports/share/icons")
        )

        # Load persisted prefs first — they seed every GIO action's initial
        # state, the visible tab, the sampling interval and the Flathub toggle.
        self.settings = user_settings.load()

        self._build_actions()
        self._build_ui()

        # Push restored prefs into each page (sort, refresh interval,
        # hide-flathub). Done AFTER _build_ui so the pages exist.
        self._apply_restored_settings()

        # Hygiene: cap the on-disk screenshot cache to a few hundred MB so it
        # doesn't grow unbounded over months of Explore browsing. Cheap — only
        # touches files when over budget.
        prune_cache(SCREENSHOT_CACHE_MAX_BYTES)

        # Always load installed apps first so Explore can see what's installed.
        self.installed_page.reload()

        # Pre-load the Flathub catalog (~1 s of local IO) so detail pages of
        # installed apps can read release notes for versions newer than the
        # one deployed locally. The locally-installed metainfo only knows
        # about its own past releases — the catalog reflects the remote's
        # current state, which is exactly the diff we want to show in the
        # "What's new since v1.10.0" block of the update box.
        self.explore_page.ensure_catalog_loaded()

        # Background-discover available updates. Single ~2.5 s call that
        # feeds every tab's update-badge lookup; finishing late just means
        # the badges fade in once the worker lands — first paint isn't
        # blocked. See updates.py for why the cost is flat regardless of
        # how many apps are installed.
        self._start_updates_fetch()

        # Detect installed apps whose origin remote has the bundle-install
        # no-enumerate quirk and surface a one-shot dialog. The detail-page
        # card still shows per-app even after dismissal — this is just the
        # global heads-up.
        NoEnumerateCheck(
            self,
            was_dismissed=bool(
                self.settings.get("no_enumerate_warning_dismissed")
            ),
            on_dismissed_change=lambda v: self._save_setting(
                "no_enumerate_warning_dismissed", v
            ),
        ).start()

        if open_app_id:
            self.open_detail_by_id(open_app_id)

    def _start_updates_fetch(self) -> None:
        def worker():
            try:
                data = fetch_updates()
            except Exception:
                data = {}

            def finish():
                self._updates = data
                self._updates_loaded = True
                self._on_updates_loaded()
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _on_updates_loaded(self) -> None:
        # Re-render every tab so the badges that depend on the lookup pick
        # up the newly-populated dict. Each page exposes a state-only
        # rerender entry point so we don't pay for another flatpak list /
        # ps sample just to add a badge.
        if not self._updates:
            # Nothing to show — no need to flicker the Running rows or
            # rebuild any AppRows. The lookup will keep returning None on
            # every call and the pages already paint without badges.
            return
        self.installed_page.refresh()
        self.explore_page.refresh()
        self.running_page.apply_updates_change()

    def updates_lookup(self, app_id: str):
        """Return the update record for `app_id` (or None)."""
        return self._updates.get(app_id) if self._updates_loaded else None

    def _apply_restored_settings(self):
        s = self.settings
        self.installed_page.set_sort(
            s.get("installed_sort_key", "date"),
            s.get("installed_reverse", True),
        )
        self.explore_page.set_sort(s.get("explore_sort_key", "popularity"))
        self.running_page.set_sort(s.get("running_sort_key", "cpu"))
        self.running_page.set_interval(s.get("running_refresh_seconds", 2))
        self.explore_page.set_show_popular(bool(s.get("show_popular", True)))

    def _save_setting(self, key: str, value):
        """Update one preference and persist to disk."""
        self.settings[key] = value
        user_settings.save(self.settings)

    # ----- GIO actions -----------------------------------------------------

    def _build_actions(self):
        s = self.settings
        sort_action = Gio.SimpleAction.new_stateful(
            "sort",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(s.get("installed_sort_key", "date")),
        )
        sort_action.connect("change-state", self._on_sort_changed)
        self.add_action(sort_action)

        rev_action = Gio.SimpleAction.new_stateful(
            "reverse",
            None,
            GLib.Variant.new_boolean(bool(s.get("installed_reverse", True))),
        )
        rev_action.connect("change-state", self._on_reverse_changed)
        self.add_action(rev_action)

        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *_: self._refresh_active_tab())
        self.add_action(refresh)

        explore_sort = Gio.SimpleAction.new_stateful(
            "explore-sort",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(s.get("explore_sort_key", "popularity")),
        )
        explore_sort.connect("change-state", self._on_explore_sort_changed)
        self.add_action(explore_sort)

        running_sort = Gio.SimpleAction.new_stateful(
            "running-sort",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(s.get("running_sort_key", "cpu")),
        )
        running_sort.connect("change-state", self._on_running_sort_changed)
        self.add_action(running_sort)

    # ----- UI scaffold -----------------------------------------------------

    def _build_ui(self):
        self.nav_view = Adw.NavigationView()
        self.nav_view.add(self._build_main_page())
        self.set_content(self.nav_view)

    def _build_main_page(self) -> Adw.NavigationPage:
        toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar()

        # Per-tab sort menus. The shared button (top-left) swaps its model
        # on tab change so the icon is always present and shows the right
        # options for whichever tab is active.
        installed_menu = Gio.Menu()
        installed_sort_section = Gio.Menu()
        installed_sort_section.append("Name", "win.sort::name")
        installed_sort_section.append("Install date", "win.sort::date")
        installed_sort_section.append("Size", "win.sort::size")
        installed_menu.append_section("Sort by", installed_sort_section)
        order_section = Gio.Menu()
        order_section.append("Reverse order", "win.reverse")
        installed_menu.append_section(None, order_section)
        self._installed_sort_menu = installed_menu

        explore_menu = Gio.Menu()
        explore_sort_section = Gio.Menu()
        explore_sort_section.append("Most popular", "win.explore-sort::popularity")
        explore_sort_section.append("Alphabetical", "win.explore-sort::name")
        explore_menu.append_section("Sort by", explore_sort_section)
        self._explore_sort_menu = explore_menu

        running_menu = Gio.Menu()
        running_sort_section = Gio.Menu()
        running_sort_section.append("CPU usage", "win.running-sort::cpu")
        running_sort_section.append("Memory usage", "win.running-sort::memory")
        running_sort_section.append("Alphabetical", "win.running-sort::name")
        running_menu.append_section("Sort by", running_sort_section)
        self._running_sort_menu = running_menu

        self.sort_btn = Gtk.MenuButton()
        self.sort_btn.set_icon_name("view-sort-ascending-symbolic")
        self.sort_btn.set_tooltip_text("Sort")
        self.sort_btn.set_menu_model(self._installed_sort_menu)
        header.pack_start(self.sort_btn)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Reload")
        refresh_btn.set_action_name("win.refresh")
        header.pack_end(refresh_btn)

        # The three pages.
        self.installed_page = InstalledPage(
            on_row_activated=self._open_detail_for,
            updates_lookup=self.updates_lookup,
        )
        self.explore_page = ExplorePage(
            on_row_activated=self._open_detail_for_explore,
            installed_ids_getter=lambda: self.installed_page.installed_ids(),
            on_show_popular_changed=lambda v: self._save_setting(
                "show_popular", bool(v)
            ),
            updates_lookup=self.updates_lookup,
        )
        self.running_page = RunningPage(
            on_row_activated=self.open_detail_by_id,
            installed_lookup=self.installed_app_lookup,
            on_interval_changed=lambda secs: self._save_setting(
                "running_refresh_seconds", int(secs)
            ),
            updates_lookup=self.updates_lookup,
        )

        # Per-tab sort pills are static labels, but clicking one pops the
        # header's sort button — the pill is the discoverability cue, the
        # header button is the canonical control. Wiring is external so the
        # pill widget itself stays a plain Label.
        for pill in (
            self.installed_page.sort_pill,
            self.explore_page.sort_pill,
            self.running_page.sort_pill,
        ):
            self._wire_sort_pill_to_button(pill)

        self.view_stack = Adw.ViewStack()
        self.view_stack.add_titled_with_icon(
            self.running_page, "running", "Running",
            "utilities-system-monitor-symbolic",
        )
        self.view_stack.add_titled_with_icon(
            self.installed_page, "installed", "Installed", "view-list-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            self.explore_page, "explore", "Explore", "system-search-symbolic"
        )
        # Restore whichever tab the user last had visible — falls back to
        # "installed" if the stored value is missing or no longer a known tab.
        last_tab = self.settings.get("last_tab", "installed")
        known = {"running", "installed", "explore"}
        self.view_stack.set_visible_child_name(
            last_tab if last_tab in known else "installed"
        )
        self.view_stack.connect("notify::visible-child-name", self._on_tab_switched)
        # The `notify` only fires on *change*; explicitly invoke once so the
        # sort-button tooltip + key-capture wiring reflect the initial tab.
        self._on_tab_switched()

        self.view_switcher = Adw.ViewSwitcher()
        self.view_switcher.set_stack(self.view_stack)
        self.view_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.view_switcher)

        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self.view_stack)

        # Key-capture binding is handled by `_on_tab_switched`, which we call
        # once at the end of __init__ for the initial state.

        page = Adw.NavigationPage(title="Flatpal", tag="main")
        page.set_child(toolbar_view)
        return page

    # ----- behaviour -------------------------------------------------------

    def _wire_sort_pill_to_button(self, pill: Gtk.Widget) -> None:
        """Make a Gtk.Label-based sort pill pop the header sort button on click."""
        gesture = Gtk.GestureClick.new()
        gesture.set_button(1)  # primary mouse button
        gesture.connect("released", lambda *_: self.sort_btn.popup())
        pill.add_controller(gesture)
        pill.set_cursor_from_name("pointer")
        pill.set_tooltip_text("Open sort menu")

    def _refresh_active_tab(self):
        name = self.view_stack.get_visible_child_name()
        if name == "installed":
            self.installed_page.reload()
        elif name == "running":
            self.running_page.refresh_now()
        else:
            # Catalog is on-disk and already memoised; just re-render against
            # any fresh installed IDs.
            self.explore_page.refresh()

    def _on_sort_changed(self, action, value):
        action.set_state(value)
        key = value.get_string()
        self.installed_page.set_sort(key, self.installed_page.reverse)
        self._save_setting("installed_sort_key", key)

    def _on_reverse_changed(self, action, value):
        action.set_state(value)
        rev = value.get_boolean()
        self.installed_page.set_sort(self.installed_page.sort_key, rev)
        self._save_setting("installed_reverse", rev)

    def _on_explore_sort_changed(self, action, value):
        action.set_state(value)
        key = value.get_string()
        self.explore_page.set_sort(key)
        self._save_setting("explore_sort_key", key)

    def _on_running_sort_changed(self, action, value):
        action.set_state(value)
        key = value.get_string()
        self.running_page.set_sort(key)
        self._save_setting("running_sort_key", key)

    def _on_tab_switched(self, *_):
        name = self.view_stack.get_visible_child_name()
        installed_active = name == "installed"
        explore_active = name == "explore"
        running_active = name == "running"

        # Remember the tab for next launch.
        if name and self.settings.get("last_tab") != name:
            self._save_setting("last_tab", name)

        # Sort button stays put; menu swaps per tab.
        if running_active:
            self.sort_btn.set_menu_model(self._running_sort_menu)
            self.sort_btn.set_tooltip_text("Sort running apps")
        elif explore_active:
            self.sort_btn.set_menu_model(self._explore_sort_menu)
            self.sort_btn.set_tooltip_text("Sort Flathub search results")
        else:
            self.sort_btn.set_menu_model(self._installed_sort_menu)
            self.sort_btn.set_tooltip_text("Sort installed apps")
        self.sort_btn.set_visible(True)

        # Rebind key-capture so typing only routes to a tab that has a search box.
        self.installed_page.search_bar.set_key_capture_widget(
            self if installed_active else None
        )
        self.explore_page.search_bar.set_key_capture_widget(
            self if explore_active else None
        )

        if installed_active:
            self.installed_page.search_entry.grab_focus()
        elif explore_active:
            self.explore_page.ensure_data_loaded()
            self.explore_page.search_entry.grab_focus()

        # Running tab only polls while it's visible (saves CPU otherwise).
        if running_active:
            self.running_page.start_tracking()
        else:
            self.running_page.stop_tracking()

    # ----- navigation ------------------------------------------------------

    def installed_app_lookup(self, app_id: str):
        """Return the installed-app dict for `app_id`, or None.

        Used by the Running tab to enrich its rows with display names, and
        internally by `open_detail_by_id` / `_open_detail_for_explore` to
        route ID-only callers through the rich installed-detail path.
        """
        for a in self.installed_page.apps:
            if a["id"] == app_id:
                return a
        return None

    def _open_detail_for(self, app: dict) -> None:
        """Called by InstalledPage. `app` carries full installed metadata."""
        page = DetailPage.from_installed(
            app,
            parent_window=self,
            catalog_lookup=self.explore_page.catalog_app,
            updates_lookup=self.updates_lookup,
        )
        self.nav_view.push(page)

    def _open_detail_for_explore(self, entry: dict) -> None:
        """Called by ExplorePage. If the app is already installed, route via
        the rich installed-detail path; otherwise show the info-only catalog
        detail."""
        if entry.get("installed"):
            installed = self.installed_app_lookup(entry["id"])
            if installed is not None:
                self._open_detail_for(installed)
                return
        page = DetailPage.from_catalog(entry, parent_window=self)
        self.nav_view.push(page)

    def open_detail_by_id(self, app_id: str) -> None:
        """Open the detail page for an installed app by ID. No-op if not installed.

        Public because `FlatpalApp.do_activate` calls it across the
        instance boundary when a --detail launch argument is pending.
        """
        installed = self.installed_app_lookup(app_id)
        if installed is not None:
            self._open_detail_for(installed)


class FlatpalApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.add_main_option(
            "detail", ord("d"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.STRING,
            "Open the detail page for this app ID on launch",
            "APP_ID",
        )
        self._pending_detail_id: str | None = None

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        self._pending_detail_id = options.get("detail")
        self.activate()
        return 0

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = FlatpalWindow(self, open_app_id=self._pending_detail_id)
        elif self._pending_detail_id:
            win.open_detail_by_id(self._pending_detail_id)
        self._pending_detail_id = None
        win.present()


def main():
    return FlatpalApp().run(sys.argv)
