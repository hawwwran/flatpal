"""Explore tab — search the Flathub appstream catalog for not-yet-installed apps."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional, Set

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .catalog import load_catalog
from .constants import INITIAL_LIMIT, LOAD_MORE_INCREMENT, MAX_LIMIT
from .metainfo import system_lang
from .popularity import format_install_count, load_popular, popularity_index
from .search import popular_shelf, search_catalog
from .widgets import make_installed_pill, make_sort_pill


# Lock search bar, status row and listboxes to the same width so the input
# visually lines up with the list rows below it.
LIST_MAX_WIDTH = 900


class ExploreRow(Adw.ActionRow):
    def __init__(self, entry: dict):
        super().__init__()
        self.entry = entry
        self.set_title(GLib.markup_escape_text(entry["name"] or entry["id"]))
        bits = [entry["id"]]
        if entry.get("developer_name"):
            bits.append(entry["developer_name"])
        self.set_subtitle(GLib.markup_escape_text(" • ".join(bits)))
        # Single-line + end-ellipsis: a freak-long app name / id / developer
        # would otherwise make the row taller and inflate the listbox's
        # natural width, which the outer clamp now no longer leaks to layout
        # but the row would still wrap visually.
        self.set_title_lines(1)
        self.set_subtitle_lines(1)
        self.set_activatable(True)

        icon = self._build_icon(entry)
        self.add_prefix(icon)

        # Suffix order matters: add_suffix appends left-to-right, so the
        # Installed pill goes on first (sits to the left) and the install-
        # count chip goes on last so it always anchors the right edge of
        # the row.
        if entry.get("installed"):
            self.add_suffix(make_installed_pill())

        pop = entry.get("popularity")
        if pop and pop.get("installs_last_month"):
            installs = pop["installs_last_month"]
            label = Gtk.Label(label=f"{format_install_count(installs)} ⇩/mo")
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            label.add_css_class("numeric")
            label.set_valign(Gtk.Align.CENTER)
            label.set_tooltip_text(f"{installs:,} installs in the past month")
            self.add_suffix(label)

    def _build_icon(self, entry: dict) -> Gtk.Image:
        cached: Optional[Path] = entry.get("cached_icon")
        if cached and cached.is_file():
            icon = Gtk.Image.new_from_file(str(cached))
        else:
            icon = Gtk.Image.new_from_icon_name(entry["id"])
            if not Gtk.IconTheme.get_for_display(self.get_display()).has_icon(entry["id"]):
                icon.set_from_icon_name("application-x-executable")
        icon.set_pixel_size(48)
        return icon


class ExplorePage(Gtk.Box):
    def __init__(
        self,
        on_row_activated: Callable[[dict], None],
        installed_ids_getter: Callable[[], Set[str]],
        on_render: Optional[Callable[[], None]] = None,
        on_show_popular_changed: Optional[Callable[[bool], None]] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_row_activated = on_row_activated
        self._installed_ids_getter = installed_ids_getter
        self._on_render = on_render
        self._on_show_popular_changed = on_show_popular_changed
        # When True (default): fetch Flathub popularity and show the popular
        # shelf in the empty-search state. When False: those network calls are
        # skipped. Local AppStream catalog search keeps working either way.
        self._show_popular = True
        self._catalog: dict = {}
        self._catalog_loaded = False
        self._catalog_loading = False
        self._popularity_hits: list = []
        self._popularity_index: dict = {}
        self._popularity_loaded = False
        self._popularity_loading = False
        # Per-page progress so we can surface partial fetches in the UI.
        self._popularity_pages_done = 0
        self._popularity_pages_total = 0
        self._sort_by = "popularity"
        self._last_query = ""
        self._popular_limit = INITIAL_LIMIT
        self._search_limit = INITIAL_LIMIT
        # Cached full result lists so "Show more" doesn't re-search.
        self._all_search_results: list = []
        self._popular_results: list = []

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(
            "Search all Flathub apps by name, ID, developer or summary"
        )
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_search_changed)

        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_child(self.search_entry)
        self.search_bar.set_search_mode(True)
        self.search_bar.set_show_close_button(False)
        self.search_bar.connect_entry(self.search_entry)

        # Status row: descriptive text on the left, "Show popular" switch on the right.
        # CenterBox (not Gtk.Box+hexpand) keeps the inner widgets from
        # propagating hexpand up to Adw.Clamp, which would otherwise let the
        # row stretch wider than the cards below. See running_page.py for the
        # same trick.
        status_row = Gtk.CenterBox()
        status_row.set_margin_top(2)
        status_row.set_margin_bottom(2)
        status_row.set_margin_start(12)
        status_row.set_margin_end(12)

        self.status_label = Gtk.Label()
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_xalign(0.0)

        # Brand-purple sort pill shared with Running/Installed tabs.
        self.sort_pill = make_sort_pill()
        self.sort_pill.set_visible(False)  # shown once a result list is populated

        status_start = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_start.append(self.status_label)
        status_start.append(self.sort_pill)
        status_row.set_start_widget(status_start)

        show_popular_caption = Gtk.Label(label="Show popular")
        show_popular_caption.add_css_class("dim-label")
        show_popular_caption.add_css_class("caption")

        self._show_popular_switch = Gtk.Switch()
        self._show_popular_switch.set_active(self._show_popular)
        self._show_popular_switch.set_valign(Gtk.Align.CENTER)
        self._show_popular_switch.set_tooltip_text(
            "Show the 'Popular this month' shelf when the search is empty. "
            "Turning it off only hides the shelf — the popularity sort and "
            "the per-row install counts keep working."
        )
        self._show_popular_switch.connect("notify::active", self._on_show_popular_toggled)

        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        end_box.append(show_popular_caption)
        end_box.append(self._show_popular_switch)
        status_row.set_end_widget(end_box)

        # Stack swaps between popular shelf, placeholder, loading spinner, results, empty.
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)

        self.placeholder = Adw.StatusPage(
            icon_name="system-search-symbolic",
            title="Type to search",
            description=(
                "Search apps available on Flathub by name, ID, developer or summary."
            ),
        )
        self.placeholder.set_vexpand(True)
        self.stack.add_named(self.placeholder, "placeholder")

        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        loading.set_valign(Gtk.Align.CENTER)
        loading.set_halign(Gtk.Align.CENTER)
        self._loading_spinner = Gtk.Spinner()
        self._loading_spinner.set_size_request(48, 48)
        self._loading_spinner.start()
        self._loading_label = Gtk.Label(label="Loading Flathub catalog…")
        loading.append(self._loading_spinner)
        loading.append(self._loading_label)
        self.stack.add_named(loading, "loading")

        # Popular shelf (default empty state once popularity loaded).
        self.popular_listbox, popular_scroll, self.popular_more_btn = (
            self._build_list_with_more(self._on_show_more_popular)
        )
        self.stack.add_named(popular_scroll, "popular")

        # Search-results list.
        self.listbox, results_scroll, self.search_more_btn = (
            self._build_list_with_more(self._on_show_more_search)
        )
        self.stack.add_named(results_scroll, "results")

        empty = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="No matches",
            description="Try a different search term.",
        )
        self.stack.add_named(empty, "empty")

        # Network-error state: Flathub popularity fetch returned empty (DNS
        # blip, sandbox network issue, Flathub down). Without this the empty
        # search state silently fell back to the "Type to search" placeholder
        # and the user couldn't tell anything had failed or retry without
        # restarting the app.
        popularity_error = Adw.StatusPage(
            icon_name="network-error-symbolic",
            title="Couldn't load popular apps",
            description=(
                "We couldn't reach Flathub to load the popular apps list. "
                "Check your network connection and try again. Search still works."
            ),
        )
        retry_btn = Gtk.Button(label="Retry")
        retry_btn.add_css_class("suggested-action")
        retry_btn.add_css_class("pill")
        retry_btn.set_halign(Gtk.Align.CENTER)
        retry_btn.connect("clicked", lambda *_: self._retry_popularity())
        popularity_error.set_child(retry_btn)
        self.stack.add_named(popularity_error, "popularity_error")

        self.stack.set_visible_child_name("placeholder")
        # Hook spinner lifecycle to stack child so it doesn't sit "started"
        # while the loading widget is off-screen.
        self.stack.connect("notify::visible-child-name", self._on_stack_changed)

        # Single outer Adw.Clamp wrapping search_bar + status_row + stack so
        # they all share the exact same 900px width allocation and the row
        # cards line up pixel-perfect with the search bar and status text.
        #
        # Two settings keep the width purely a function of *window* size,
        # never of inner content:
        #   • hexpand=True propagates up so the ViewStack → ToolbarView →
        #     window chain allocates the clamp the full window width.
        #   • tightening_threshold == maximum_size flattens AdwClamp's default
        #     cubic-ease window (400..~1150 px) into a hard `min(for_size,
        #     max)`. Without this, content-driven jitter in the clamp's
        #     allocation rides the easing curve and the search bar visibly
        #     shifts a few pixels when the stack or status text changes.
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer_box.append(self.search_bar)
        outer_box.append(status_row)
        outer_box.append(self.stack)
        outer_clamp = Adw.Clamp()
        outer_clamp.set_maximum_size(LIST_MAX_WIDTH)
        outer_clamp.set_tightening_threshold(LIST_MAX_WIDTH)
        outer_clamp.set_child(outer_box)
        outer_clamp.set_vexpand(True)
        outer_clamp.set_hexpand(True)
        self.append(outer_clamp)

    def _on_stack_changed(self, *_):
        if self.stack.get_visible_child_name() == "loading":
            self._loading_spinner.start()
        else:
            self._loading_spinner.stop()

    # ----- helpers ---------------------------------------------------------

    def _build_list_with_more(self, on_more):
        """Make a ScrolledWindow → Box(listbox + show-more button).

        No inner Adw.Clamp here — the outer clamp at the page level handles
        width constraint for the whole tab, which guarantees the row cards
        align with the search bar and status text above.
        """
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        listbox.connect("row-activated", self._on_listbox_row_activated)

        more_btn = Gtk.Button(label="Show more")
        more_btn.add_css_class("pill")
        more_btn.set_halign(Gtk.Align.CENTER)
        more_btn.set_margin_top(12)
        more_btn.set_visible(False)
        more_btn.connect("clicked", lambda *_: on_more())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_top(8)
        box.set_margin_bottom(16)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(listbox)
        box.append(more_btn)

        scroll.set_child(box)
        return listbox, scroll, more_btn

    # ----- public API ------------------------------------------------------

    def ensure_data_loaded(self) -> None:
        """Kick off catalog + popularity fetch on first activation.

        Popularity is fetched unconditionally — the toggle below only hides
        the empty-state shelf, it doesn't disable the underlying data. Once
        fetched it's cached for 24 h, so the popularity sort and the install-
        count chips keep working even when the shelf is hidden.
        """
        self._ensure_catalog()
        self._ensure_popularity()
        # Re-render right away so the empty-search state flips from the
        # "Type to search" placeholder to the loading spinner; without this
        # the spinner only appears once one of the worker threads finishes
        # and calls refresh() itself.
        self.refresh()

    def set_show_popular(self, value: bool) -> None:
        """Toggle the empty-state popular shelf.

        Only affects what appears when the search box is empty: ON shows the
        "Popular this month" shelf, OFF shows the "Type to search"
        placeholder. Popularity data itself is always fetched, so the
        popularity sort and the per-row install-count chips keep working
        regardless of this setting.
        """
        if value == self._show_popular:
            return
        self._show_popular = value
        if self._show_popular_switch.get_active() != value:
            self._show_popular_switch.set_active(value)
        self.refresh()

    def _on_show_popular_toggled(self, *_):
        new_value = self._show_popular_switch.get_active()
        if new_value == self._show_popular:
            return
        self.set_show_popular(new_value)
        if self._on_show_popular_changed:
            self._on_show_popular_changed(new_value)

    def set_sort(self, key: str) -> None:
        if key == self._sort_by:
            return
        self._sort_by = key
        # Changing sort is a "new view" — collapse any prior "Show more"
        # expansion back to the initial 50 so the user isn't surprised by
        # an unexpectedly long list in a different order.
        self._search_limit = INITIAL_LIMIT
        self._popular_limit = INITIAL_LIMIT
        self.refresh()

    def catalog_app(self, app_id: str) -> Optional[dict]:
        return self._catalog.get(app_id) if self._catalog_loaded else None

    # ----- internals -------------------------------------------------------

    def _ensure_catalog(self):
        if self._catalog_loaded or self._catalog_loading:
            return
        self._catalog_loading = True
        if self._last_query.strip():
            self.stack.set_visible_child_name("loading")

        lang = system_lang()

        def worker():
            # Catch *anything* so the finish() callback always fires and the
            # _catalog_loading flag clears. Without this, an exception inside
            # load_catalog (or any helper it calls) leaves the Explore tab
            # stuck on the loading spinner forever.
            try:
                data = load_catalog(lang=lang)
            except Exception:
                data = {}

            def finish():
                self._catalog = data
                self._catalog_loaded = True
                self._catalog_loading = False
                self.refresh()
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _ensure_popularity(self):
        if self._popularity_loaded or self._popularity_loading:
            return
        self._popularity_loading = True
        # Reset page counters so any prior fetch's numbers don't leak into
        # the status line if we're re-fetching after a manual refresh.
        self._popularity_pages_done = 0
        self._popularity_pages_total = 0

        def on_progress(done, total, _hits):
            # Fired from the fetcher's worker thread; hop to GTK's main thread.
            def update():
                self._popularity_pages_done = done
                self._popularity_pages_total = total
                return False
            GLib.idle_add(update)

        def worker():
            # Catch everything so the finish() callback always fires; without
            # this an exception from load_popular or popularity_index leaves
            # the spinner stuck and ensure_data_loaded() permanently no-ops.
            try:
                hits = load_popular(on_progress=on_progress)
            except Exception:
                hits = []
            try:
                idx = popularity_index(hits)
            except Exception:
                idx = {}

            def finish():
                self._popularity_hits = hits
                self._popularity_index = idx
                self._popularity_loaded = True
                self._popularity_loading = False
                self.refresh()
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _on_search_changed(self, entry):
        self._last_query = entry.get_text()
        self._search_limit = INITIAL_LIMIT  # new query → reset pagination
        if self._last_query.strip() and not self._catalog_loaded:
            self._ensure_catalog()
            return
        self.refresh()

    def _on_show_more_search(self):
        self._search_limit = min(self._search_limit + LOAD_MORE_INCREMENT, MAX_LIMIT)
        self.refresh()

    def _on_show_more_popular(self):
        self._popular_limit = min(self._popular_limit + LOAD_MORE_INCREMENT, MAX_LIMIT)
        self.refresh()

    def _on_listbox_row_activated(self, _listbox, row):
        if hasattr(row, "entry"):
            self._on_row_activated(row.entry)

    def _clear(self, listbox):
        child = listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            listbox.remove(child)
            child = nxt

    def refresh(self):
        """Re-render against current state (catalog/popularity/installed IDs).

        Public entry point — invoked from app.py:_refresh_active_tab when the
        user presses Reload on the Explore tab, and internally on every state
        transition (search-changed, sort-changed, fetch-finished, …).
        """
        query = self._last_query
        installed_ids = self._installed_ids_getter()

        if not query.strip():
            self._render_empty_state(installed_ids)
            if self._on_render:
                self._on_render()
            return

        if not self._catalog_loaded:
            self.stack.set_visible_child_name("loading")
            self.status_label.set_label("")
            self.sort_pill.set_visible(False)
            if self._on_render:
                self._on_render()
            return

        # The "Show popular" toggle only hides the empty-state shelf; we
        # still want install-count chips on each row and a working
        # popularity sort here. So thread the loaded index through search
        # results regardless of the toggle.
        idx = self._popularity_index if self._popularity_loaded else None
        all_results = search_catalog(
            self._catalog, installed_ids, query,
            limit=MAX_LIMIT,
            sort_by=self._sort_by,
            popularity_idx=idx,
        )
        self._all_search_results = all_results
        visible = all_results[: self._search_limit]

        self._clear(self.listbox)
        for entry in visible:
            self.listbox.append(ExploreRow(entry))

        if all_results:
            self.stack.set_visible_child_name("results")
            sort_label = "popularity" if self._sort_by == "popularity" else "alphabetical"
            if len(all_results) > len(visible):
                base = (
                    f"Showing {len(visible)} of {len(all_results)} match"
                    f"{'es' if len(all_results) != 1 else ''}"
                )
            else:
                base = (
                    f"{len(all_results)} match"
                    f"{'es' if len(all_results) != 1 else ''}"
                )
            self.status_label.set_label(
                f"{base} from {len(self._catalog)} Flathub apps"
            )
            self.sort_pill.set_label(f"sorted by {sort_label}")
            self.sort_pill.set_visible(True)
            self._update_more_button(
                self.search_more_btn, len(visible), len(all_results)
            )
        else:
            self.stack.set_visible_child_name("empty")
            self.status_label.set_label(
                f"No matches in {len(self._catalog)} Flathub apps"
            )
            self.sort_pill.set_visible(False)
            self.search_more_btn.set_visible(False)

        if self._on_render:
            self._on_render()

    def _render_empty_state(self, installed_ids: Set[str]):
        # If "Show popular" is off, skip straight to the placeholder regardless
        # of whether popularity loaded earlier.
        if not self._show_popular:
            self.stack.set_visible_child_name("placeholder")
            self.status_label.set_label("")
            self.sort_pill.set_visible(False)
            self.search_more_btn.set_visible(False)
            self.popular_more_btn.set_visible(False)
            return

        # If both catalog and popularity are loaded → show popular shelf.
        if self._catalog_loaded and self._popularity_loaded and self._popularity_hits:
            all_rows = popular_shelf(
                self._popularity_hits, self._catalog, installed_ids,
                limit=MAX_LIMIT,
            )
            self._popular_results = all_rows
            visible = all_rows[: self._popular_limit]

            self._clear(self.popular_listbox)
            for entry in visible:
                self.popular_listbox.append(ExploreRow(entry))

            self.stack.set_visible_child_name("popular")
            base = (
                f"Popular on Flathub · showing {len(visible)} of "
                f"{len(all_rows)} by installs in the past month"
                if len(all_rows) > len(visible)
                else
                f"Popular on Flathub · top {len(visible)} by installs "
                "in the past month"
            )
            # If the popularity fetch was only partially successful, say so.
            done = self._popularity_pages_done
            total = self._popularity_pages_total
            if 0 < done < total:
                base += f" · loaded {done} of {total} pages"
            self.status_label.set_label(base)
            self.sort_pill.set_label("sorted by popularity")
            self.sort_pill.set_visible(True)
            self._update_more_button(
                self.popular_more_btn, len(visible), len(all_rows)
            )
            return

        # Either dataset still loading → spinner. Previously only the catalog
        # phase had a spinner and the popularity phase silently fell back to
        # the placeholder, so when popularity took a beat to arrive the user
        # saw "Type to search" and assumed the popular shelf was missing.
        catalog_busy = not self._catalog_loaded and self._catalog_loading
        popularity_busy = not self._popularity_loaded and self._popularity_loading
        if catalog_busy or popularity_busy:
            self._loading_label.set_label(
                "Loading Flathub catalog…" if catalog_busy
                else "Loading popular apps from Flathub…"
            )
            self.stack.set_visible_child_name("loading")
            self.status_label.set_label("")
            self.sort_pill.set_visible(False)
            self.search_more_btn.set_visible(False)
            self.popular_more_btn.set_visible(False)
            return

        # Popularity fetch finished with no hits (network failure, all four
        # pages failed). Show a retry surface — without one the only way to
        # recover was to quit and relaunch.
        if self._catalog_loaded and self._popularity_loaded and not self._popularity_hits:
            self.stack.set_visible_child_name("popularity_error")
            self.status_label.set_label("")
            self.sort_pill.set_visible(False)
            self.search_more_btn.set_visible(False)
            self.popular_more_btn.set_visible(False)
            return

        # Fallback: neither fetch started yet (e.g. tab opened with Show
        # popular turned on but ensure_data_loaded hasn't fired). Land on
        # the same placeholder we use when Show popular is off.
        self.stack.set_visible_child_name("placeholder")
        self.status_label.set_label("")
        self.sort_pill.set_visible(False)
        self.search_more_btn.set_visible(False)
        self.popular_more_btn.set_visible(False)

    def _retry_popularity(self):
        """Re-arm the popularity fetch after a network failure.

        `_ensure_popularity` short-circuits when `_popularity_loaded` is true,
        so we drop the loaded flag (and the empty hits/index that landed on
        the previous attempt) before re-firing the worker. Also clear the
        loading flag defensively in case a future state-machine path lets
        us reach Retry with a stale loading=True (today the error stack
        child only renders once loading has already flipped to False).
        """
        self._popularity_loaded = False
        self._popularity_loading = False
        self._popularity_hits = []
        self._popularity_index = {}
        self._popularity_pages_done = 0
        self._popularity_pages_total = 0
        self._ensure_popularity()
        self.refresh()

    def _update_more_button(self, btn: Gtk.Button, visible_count: int, total: int):
        remaining = total - visible_count
        if remaining <= 0:
            btn.set_visible(False)
            return
        next_step = min(LOAD_MORE_INCREMENT, remaining)
        btn.set_label(f"Show {next_step} more · {remaining} hidden")
        btn.set_visible(True)
