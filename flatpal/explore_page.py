"""Explore tab: search the Flathub appstream catalog for not-yet-installed apps."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, NamedTuple, Optional, Set

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .constants import INITIAL_LIMIT, LOAD_MORE_INCREMENT, MAX_LIMIT
from .explore_data import CatalogManager
from .popularity import format_install_count
from .search import popular_shelf, search_catalog
from .widgets import (
    clear_listbox,
    make_installed_pill,
    make_list_clamp,
    make_sort_pill,
    make_status_label,
    make_update_pill,
    update_tooltip,
)


class ViewState(NamedTuple):
    """Snapshot of which Explore-tab widgets should be visible and labelled how.

    Produced by the resolver methods (``_resolve_view_state`` and friends) from
    immutable inputs (query string, catalog/popularity flags, computed result
    counts); consumed by ``_apply_view_state`` which does every GTK mutation in
    one place. Convention: empty string / ``None`` means "hide". Loading text
    is consulted only when ``stack_child == "loading"``.
    """

    stack_child: str
    status_text: str = ""
    sort_pill_label: str = ""  # "" → hidden
    search_more_label: Optional[str] = None  # None → hidden
    popular_more_label: Optional[str] = None  # None → hidden
    loading_text: str = ""


class ExploreRow(Adw.ActionRow):
    def __init__(self, entry: dict, update_info: Optional[dict] = None):
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

        # Suffix order matters: add_suffix appends left-to-right.
        #   [Update]  → leftmost when present; the most actionable signal
        #   [Installed] → status marker, follows Update
        #   [123k ⇩/mo] → install count, anchors the right edge
        if update_info:
            # Catalog entries don't carry the user's installed version, so
            # the tooltip degrades to the short form ("Update available →
            # {new}"); see widgets.update_tooltip.
            self.add_suffix(make_update_pill(
                tooltip=update_tooltip(None, update_info),
            ))

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
        on_show_popular_changed: Optional[Callable[[bool], None]] = None,
        updates_lookup: Optional[Callable[[str], Optional[dict]]] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_row_activated = on_row_activated
        self._installed_ids_getter = installed_ids_getter
        self._on_show_popular_changed = on_show_popular_changed
        self._updates_lookup = updates_lookup or (lambda _id: None)
        # When True (default): fetch Flathub popularity and show the popular
        # shelf in the empty-search state. When False: those network calls are
        # skipped. Local AppStream catalog search keeps working either way.
        self._show_popular = True
        self._data = CatalogManager(on_loaded=self.refresh)
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

        self.status_label = make_status_label()

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

        # Single outer clamp wrapping search_bar + status_row + stack so
        # they all share the same width allocation and the row cards line up
        # pixel-perfect with the search bar and status text above.
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer_box.append(self.search_bar)
        outer_box.append(status_row)
        outer_box.append(self.stack)
        self.append(make_list_clamp(outer_box, vexpand=True))

    def _on_stack_changed(self, *_):
        if self.stack.get_visible_child_name() == "loading":
            self._loading_spinner.start()
        else:
            self._loading_spinner.stop()

    def _build_list_with_more(self, on_more):
        """Make a ScrolledWindow → Box(listbox + show-more button).

        No inner Adw.Clamp here; the outer clamp at the page level handles
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

    def ensure_data_loaded(self) -> None:
        """Kick off catalog + popularity fetch on first activation.

        Popularity is fetched unconditionally; the toggle below only hides
        the empty-state shelf, it doesn't disable the underlying data. Once
        fetched it's cached for 24 h, so the popularity sort and the install-
        count chips keep working even when the shelf is hidden.
        """
        self._data.ensure_catalog()
        self._data.ensure_popularity()
        # Re-render right away so the empty-search state flips from the
        # "Type to search" placeholder to the loading spinner; without this
        # the spinner only appears once one of the worker threads finishes
        # and calls refresh() itself.
        self.refresh()

    def ensure_catalog_loaded(self) -> None:
        """Kick off ONLY the catalog parse (no popularity fetch).

        Used by the window at startup so detail pages of installed apps can
        consult the catalog's release list; that list comes from the
        remote and includes versions newer than what's deployed locally,
        which is exactly the "what's new since installed" diff the update
        box renders. Catalog parse is ~1 s of local IO, cheap to do eagerly.
        Idempotent thanks to CatalogManager.ensure_catalog's short-circuits.
        """
        self._data.ensure_catalog()

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
        # Changing sort is a "new view"; collapse any prior "Show more"
        # expansion back to the initial 50 so the user isn't surprised by
        # an unexpectedly long list in a different order.
        self._search_limit = INITIAL_LIMIT
        self._popular_limit = INITIAL_LIMIT
        self.refresh()

    def catalog_app(self, app_id: str) -> Optional[dict]:
        return self._data.catalog.get(app_id) if self._data.catalog_loaded else None

    def _on_search_changed(self, entry):
        self._last_query = entry.get_text()
        self._search_limit = INITIAL_LIMIT  # new query → reset pagination
        if self._last_query.strip() and not self._data.catalog_loaded:
            # Catalog parse can also be triggered by an empty query → here
            # we want the loading spinner to appear right away.
            self.stack.set_visible_child_name("loading")
            self._data.ensure_catalog()
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

    def refresh(self):
        """Re-render against current state (catalog/popularity/installed IDs).

        Public entry point: invoked from app.py:_refresh_active_tab when the
        user presses Reload on the Explore tab, and internally on every state
        transition (search-changed, sort-changed, fetch-finished, …).
        """
        installed_ids = self._installed_ids_getter()
        state = self._resolve_view_state(self._last_query, installed_ids)
        self._apply_view_state(state)

    # ----- view-state resolvers -------------------------------------------
    #
    # Each resolver returns the ViewState that should appear on screen given
    # the current data flags. The search and popular branches additionally
    # populate their listbox as a side effect; the row counts feed the
    # status text and the "Show more" button label.

    def _resolve_view_state(
        self, query: str, installed_ids: Set[str],
    ) -> ViewState:
        if not query.strip():
            return self._resolve_empty_view_state(installed_ids)
        if not self._data.catalog_loaded:
            return ViewState(
                stack_child="loading",
                loading_text="Loading Flathub catalog…",
            )
        return self._resolve_search_view_state(query, installed_ids)

    def _resolve_search_view_state(
        self, query: str, installed_ids: Set[str],
    ) -> ViewState:
        # The "Show popular" toggle only hides the empty-state shelf; we
        # still want install-count chips on each row and a working
        # popularity sort here. So thread the loaded index through search
        # results regardless of the toggle.
        idx = self._data.popularity_idx if self._data.popularity_loaded else None
        all_results = search_catalog(
            self._data.catalog, installed_ids, query,
            limit=MAX_LIMIT,
            sort_by=self._sort_by,
            popularity_idx=idx,
        )
        self._all_search_results = all_results
        visible = all_results[: self._search_limit]
        self._populate_listbox(self.listbox, visible)

        if not all_results:
            return ViewState(
                stack_child="empty",
                status_text=f"No matches in {len(self._data.catalog)} Flathub apps",
            )

        matches_word = "match" if len(all_results) == 1 else "matches"
        if len(all_results) > len(visible):
            base = f"Showing {len(visible)} of {len(all_results)} {matches_word}"
        else:
            base = f"{len(all_results)} {matches_word}"
        sort_label = "popularity" if self._sort_by == "popularity" else "alphabetical"
        return ViewState(
            stack_child="results",
            status_text=f"{base} from {len(self._data.catalog)} Flathub apps",
            sort_pill_label=f"sorted by {sort_label}",
            search_more_label=self._more_label(len(visible), len(all_results)),
        )

    def _resolve_empty_view_state(self, installed_ids: Set[str]) -> ViewState:
        # "Show popular" off → skip straight to placeholder regardless of
        # whether popularity loaded earlier.
        if not self._show_popular:
            return ViewState(stack_child="placeholder")

        # Both datasets loaded and popularity has hits → show the shelf.
        if (
            self._data.catalog_loaded
            and self._data.popularity_loaded
            and self._data.popularity_hits
        ):
            return self._resolve_popular_view_state(installed_ids)

        # Either dataset still loading → spinner. Previously only the catalog
        # phase had a spinner and the popularity phase silently fell back to
        # the placeholder, so when popularity took a beat to arrive the user
        # saw "Type to search" and assumed the popular shelf was missing.
        catalog_busy = (
            not self._data.catalog_loaded and self._data.catalog_loading
        )
        popularity_busy = (
            not self._data.popularity_loaded and self._data.popularity_loading
        )
        if catalog_busy or popularity_busy:
            return ViewState(
                stack_child="loading",
                loading_text=(
                    "Loading Flathub catalog…" if catalog_busy
                    else "Loading popular apps from Flathub…"
                ),
            )

        # Popularity fetch finished with no hits (network failure, all four
        # pages failed). Surface a retry; without one the only way to
        # recover was to quit and relaunch.
        if (
            self._data.catalog_loaded
            and self._data.popularity_loaded
            and not self._data.popularity_hits
        ):
            return ViewState(stack_child="popularity_error")

        # Fallback: neither fetch started yet (e.g. tab opened with Show
        # popular turned on but ensure_data_loaded hasn't fired).
        return ViewState(stack_child="placeholder")

    def _resolve_popular_view_state(
        self, installed_ids: Set[str],
    ) -> ViewState:
        all_rows = popular_shelf(
            self._data.popularity_hits, self._data.catalog, installed_ids,
            limit=MAX_LIMIT,
        )
        self._popular_results = all_rows
        visible = all_rows[: self._popular_limit]
        self._populate_listbox(self.popular_listbox, visible)

        if len(all_rows) > len(visible):
            base = (
                f"Popular on Flathub · showing {len(visible)} of "
                f"{len(all_rows)} by installs in the past month"
            )
        else:
            base = (
                f"Popular on Flathub · top {len(visible)} by installs "
                "in the past month"
            )
        # If the popularity fetch was only partially successful, say so.
        done = self._data.popularity_pages_done
        total = self._data.popularity_pages_total
        if 0 < done < total:
            base += f" · loaded {done} of {total} pages"

        return ViewState(
            stack_child="popular",
            status_text=base,
            sort_pill_label="sorted by popularity",
            popular_more_label=self._more_label(len(visible), len(all_rows)),
        )

    def _populate_listbox(self, listbox: Gtk.ListBox, entries: list) -> None:
        clear_listbox(listbox)
        for entry in entries:
            listbox.append(ExploreRow(
                entry, update_info=self._updates_lookup(entry["id"]),
            ))

    def _more_label(self, visible_count: int, total: int) -> Optional[str]:
        """Compose the "Show N more · M hidden" label, or None when nothing's hidden."""
        remaining = total - visible_count
        if remaining <= 0:
            return None
        next_step = min(LOAD_MORE_INCREMENT, remaining)
        return f"Show {next_step} more · {remaining} hidden"

    # ----- view-state applier ---------------------------------------------

    def _apply_view_state(self, state: ViewState) -> None:
        """Single place that mutates every state-driven widget on the page.

        Co-locating the GTK calls keeps the resolvers honest: forgetting to
        hide a more-button or the sort pill in one branch is no longer a
        silent bug, because `_apply_view_state` always touches both.
        """
        self.stack.set_visible_child_name(state.stack_child)
        self.status_label.set_label(state.status_text)

        if state.sort_pill_label:
            self.sort_pill.set_label(state.sort_pill_label)
            self.sort_pill.set_visible(True)
        else:
            self.sort_pill.set_visible(False)

        self._apply_more_button(self.search_more_btn, state.search_more_label)
        self._apply_more_button(self.popular_more_btn, state.popular_more_label)

        if state.stack_child == "loading" and state.loading_text:
            self._loading_label.set_label(state.loading_text)

    def _apply_more_button(
        self, btn: Gtk.Button, label: Optional[str],
    ) -> None:
        if label is None:
            btn.set_visible(False)
            return
        btn.set_label(label)
        btn.set_visible(True)

    def _retry_popularity(self):
        self._data.retry_popularity()
        self.refresh()
