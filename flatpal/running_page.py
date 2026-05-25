"""Running-flatpaks tab — live CPU/memory per running app, refreshed every 2 s."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .constants import REFRESH_MS
from .running import (
    RunningTracker,
    format_cpu,
    format_memory,
    order_with_freeze,
    sort_running,
)
from .row_cache import RowCache
from .running_rows import expected_row_type, make_running_row
from .widgets import (
    make_freeze_pill,
    make_list_clamp,
    make_sort_pill,
    make_status_label,
)


# Values the user can pick in the inline interval dropdown. Order matters —
# it's the order the user sees in the menu.
INTERVAL_OPTIONS_SEC = (1, 2, 5, 10, 30)
INTERVAL_LABELS = tuple(f"{s} s" for s in INTERVAL_OPTIONS_SEC)


_SORT_LABELS = {"cpu": "CPU", "memory": "memory", "name": "name"}


class RunningPage(Gtk.Box):
    def __init__(
        self,
        on_row_activated: Callable[[dict], None],
        installed_lookup: Callable[[str], Optional[dict]],
        on_interval_changed: Optional[Callable[[int], None]] = None,
        updates_lookup: Optional[Callable[[str], Optional[dict]]] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_row_activated = on_row_activated
        self._installed_lookup = installed_lookup
        self._on_interval_changed = on_interval_changed
        self._updates_lookup = updates_lookup or (lambda _id: None)
        self._tracker: Optional[RunningTracker] = None
        self._timeout_id: Optional[int] = None
        self._sort_by = "cpu"  # current sort key; persisted via set_sort
        self._sample_in_flight = False  # one sampler at a time
        self._interval_seconds = REFRESH_MS // 1000  # mutable via set_interval()
        # Last sampled+enriched rows, kept so set_sort and the freeze toggle
        # can re-render instantly without waiting for the next sample.
        self._last_rows: list[dict] = []
        # Position-freeze state. When True, _render_rows reuses the cache's
        # rendered_order to keep apps in place across refreshes; new apps are
        # appended in natural sort order, vanished apps drop out.
        self._freeze_position = False
        # RowCache (created after self.listbox below) keeps row widgets alive
        # across ticks so the tooltip-on-hover doesn't die every 2 s.
        self._rows: Optional[RowCache] = None

        # Status row: "N apps running …" on the left, refresh-interval picker on the right.
        # Use Gtk.CenterBox (not Gtk.Box+hexpand) so children DON'T need hexpand
        # to push apart — hexpand on a Gtk.Box child propagates up the tree and
        # makes Adw.Clamp ignore its max-size constraint, which would let the
        # status text drift further left than the row cards below. CenterBox
        # places its end widget at its right edge without that propagation.
        status_row = Gtk.CenterBox()
        status_row.set_margin_top(2)
        status_row.set_margin_bottom(2)
        # Match the listbox margins below for a clean vertical line-up.
        status_row.set_margin_start(12)
        status_row.set_margin_end(12)

        self.status_label = make_status_label()

        # Brand-purple sort pill. Updated by _render_rows whenever sort changes.
        # Hidden until the first sample arrives — an empty pill looks broken,
        # and the actual sort label isn't known until _render_rows runs.
        self.sort_pill = make_sort_pill()
        self.sort_pill.set_visible(False)

        # Freeze-position toggle: blue when on, gray when off. Pins the
        # current order so CPU/memory swings don't shuffle the list while the
        # user is reading it. Hidden alongside the sort pill until the first
        # sample arrives — a toggle with no rows behind it is meaningless.
        self.freeze_pill = make_freeze_pill(
            self._on_freeze_toggled,
            initial=self._freeze_position,
        )
        self.freeze_pill.set_visible(False)

        # "Collapse all": visible only while at least one ExpanderRow is open.
        self.collapse_btn = Gtk.Button(label="Collapse all")
        self.collapse_btn.add_css_class("flat")
        self.collapse_btn.add_css_class("caption")
        self.collapse_btn.set_valign(Gtk.Align.CENTER)
        self.collapse_btn.set_visible(False)
        self.collapse_btn.connect("clicked", self._on_collapse_all_clicked)

        start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        start_box.append(self.status_label)
        start_box.append(self.sort_pill)
        start_box.append(self.freeze_pill)
        start_box.append(self.collapse_btn)
        status_row.set_start_widget(start_box)

        interval_caption = Gtk.Label(label="Refresh")
        interval_caption.add_css_class("dim-label")
        interval_caption.add_css_class("caption")

        self._interval_dropdown = Gtk.DropDown.new_from_strings(list(INTERVAL_LABELS))
        try:
            self._interval_dropdown.set_selected(
                INTERVAL_OPTIONS_SEC.index(self._interval_seconds)
            )
        except ValueError:
            self._interval_dropdown.set_selected(INTERVAL_OPTIONS_SEC.index(2))
        self._interval_dropdown.set_tooltip_text("How often to re-sample CPU and memory")
        self._interval_dropdown.connect(
            "notify::selected", self._on_interval_dropdown_changed
        )

        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        end_box.append(interval_caption)
        end_box.append(self._interval_dropdown)
        status_row.set_end_widget(end_box)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)

        # Loading state. Flatpal sees itself in `flatpak ps`, so the "empty"
        # page below is effectively unreachable in practice — but on the
        # first sample, before flatpak-spawn has rounded the trip, we have
        # nothing to render. Show a spinner + status line until then.
        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        loading_box.set_valign(Gtk.Align.CENTER)
        loading_box.set_halign(Gtk.Align.CENTER)
        loading_box.set_vexpand(True)
        loading_spinner = Gtk.Spinner()
        loading_spinner.set_size_request(36, 36)
        loading_spinner.start()
        loading_label = Gtk.Label(label="Looking for running Flatpaks…")
        loading_label.add_css_class("dim-label")
        loading_box.append(loading_spinner)
        loading_box.append(loading_label)
        self.stack.add_named(loading_box, "loading")

        empty = Adw.StatusPage(
            icon_name="utilities-system-monitor-symbolic",
            title="Nothing running",
            description="No Flatpak apps are running right now.",
        )
        empty.set_vexpand(True)
        self.stack.add_named(empty, "empty")

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_top(8)
        self.listbox.set_margin_bottom(12)
        self.listbox.set_margin_start(12)
        self.listbox.set_margin_end(12)
        self.listbox.set_hexpand(True)
        self.listbox.connect("row-activated", self._on_listbox_row_activated)
        self._scrolled.set_child(self.listbox)
        self.stack.add_named(self._scrolled, "list")

        self._rows = RowCache(
            container=self.listbox,
            make_widget=lambda r: make_running_row(
                r, self._installed_lookup, self._on_row_activated,
                update_info=self._updates_lookup(r["id"]),
            ),
            expected_type=expected_row_type,
            on_new_widget=self._wire_new_row,
        )
        # Default to loading; _render_rows flips to "list" once a sample lands.
        self.stack.set_visible_child_name("loading")

        # Single outer clamp wrapping status_row + stack so both share the
        # same width allocation. Wrapping each child in its own clamp produces
        # subtly different positions because the stack adds an extra layer of
        # allocation (and the listbox inside it has hexpand that propagates
        # upward differently than the status row's centerbox).
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer_box.append(status_row)
        outer_box.append(self.stack)
        self.append(make_list_clamp(outer_box, vexpand=True))

    # ----- public API ------------------------------------------------------

    def start_tracking(self) -> None:
        """Begin polling at the current interval. Idempotent."""
        if self._tracker is None:
            self._tracker = RunningTracker()
        if self._timeout_id is None:
            # Sample once immediately so the first paint isn't 0%/0MB.
            self._refresh()
            self._timeout_id = GLib.timeout_add(
                self._interval_seconds * 1000, self._on_tick
            )

    def stop_tracking(self) -> None:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None

    def refresh_now(self) -> None:
        if self._tracker is None:
            self._tracker = RunningTracker()
        self._refresh()

    def apply_updates_change(self) -> None:
        """Rebuild visible rows so the per-app Update pill picks up the
        latest `updates_lookup` result.

        Called once after the background `flatpak remote-ls --updates`
        worker lands. Clears the row cache so the row-diff path in
        `_render_rows` falls into the cold-path branch that reconstructs
        each row via `make_running_row` (which re-reads the lookup), then
        rerenders against the cached `_last_rows` so no extra `flatpak
        ps` sampling happens just to add a badge.

        Snapshots and restores any expanded ExpanderRow disclosure state
        so a user who happened to expand a multi-sandbox row during the
        first ~2.5 s after launch doesn't see it silently collapse when
        the worker lands.
        """
        expanded_ids = {
            widget.app_id
            for widget in self._rows.iter_widgets()
            if isinstance(widget, Adw.ExpanderRow) and widget.get_expanded()
        }
        self._rows.clear()
        if self._last_rows:
            self._render_rows(self._last_rows)
        if expanded_ids:
            for widget in self._rows.iter_widgets():
                if (
                    isinstance(widget, Adw.ExpanderRow)
                    and widget.app_id in expanded_ids
                ):
                    widget.set_expanded(True)

    def set_sort(self, key: str) -> None:
        """User-driven sort change. Re-renders from the cached sample."""
        if key == self._sort_by:
            return
        self._sort_by = key
        # A new sort retires the previously-frozen order — the user explicitly
        # asked for a different layout, so honour it. The render captures a
        # new order; if freeze is still on, that new order becomes the pin.
        self._rows.reset_order()
        if self._last_rows:
            self._render_rows(self._last_rows)
        else:
            self._refresh()

    def set_interval(self, seconds: int) -> None:
        """Change the sampling interval. Restarts the timer if running."""
        if seconds == self._interval_seconds:
            return
        self._interval_seconds = seconds
        try:
            self._interval_dropdown.set_selected(
                INTERVAL_OPTIONS_SEC.index(seconds)
            )
        except ValueError:
            pass
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = GLib.timeout_add(
                self._interval_seconds * 1000, self._on_tick
            )

    def _on_interval_dropdown_changed(self, *_):
        idx = self._interval_dropdown.get_selected()
        if not (0 <= idx < len(INTERVAL_OPTIONS_SEC)):
            return
        new_seconds = INTERVAL_OPTIONS_SEC[idx]
        if new_seconds == self._interval_seconds:
            return
        self.set_interval(new_seconds)
        if self._on_interval_changed:
            self._on_interval_changed(new_seconds)

    # ----- internals -------------------------------------------------------

    def _on_tick(self):
        self._refresh()
        return True  # keep running

    def _refresh(self):
        """Kick off an off-main-thread sample. Skips if one is in flight."""
        if self._tracker is None or self._sample_in_flight:
            return
        self._sample_in_flight = True
        tracker = self._tracker

        def worker():
            try:
                rows = tracker.sample()
            except Exception:
                rows = []
            GLib.idle_add(self._on_sample_done, rows)

        threading.Thread(target=worker, daemon=True).start()

    def _on_sample_done(self, rows):
        self._sample_in_flight = False
        # Attach display name so sort_running can tie-break alphabetically.
        for row in rows:
            installed = self._installed_lookup(row["id"]) if self._installed_lookup else None
            row["display_name"] = (installed or {}).get("name") or row["id"]
        # Cache the enriched rows so set_sort / expand handlers can re-render
        # instantly without waiting for the next sample.
        self._last_rows = list(rows)
        self._render_rows(rows)
        return False

    def _render_rows(self, rows: list) -> None:
        """Diff the listbox against the new sample.

        Keeps each row widget alive across ticks (cached by app_id) so an
        in-flight hover-tooltip survives the refresh. Restructures the
        listbox only when the order or the set of running apps changes —
        which means tooltips, hover states, and expander disclosure are
        all stable during a typical refresh.

        Called from _on_sample_done after every tick, and from set_sort /
        freeze-toggle to re-render the cached sample.
        """
        natural = lambda r: sort_running(r, self._sort_by)  # noqa: E731
        if self._freeze_position and self._rows.rendered_order:
            sorted_rows = order_with_freeze(
                list(rows), self._rows.rendered_order, natural,
            )
        else:
            sorted_rows = natural(list(rows))

        self._rows.render(sorted_rows)

        if sorted_rows:
            self.stack.set_visible_child_name("list")
            total_cpu = sum(r["cpu_percent"] for r in sorted_rows)
            total_mem = sum(r["memory_bytes"] for r in sorted_rows)
            self.status_label.set_label(
                f"{len(sorted_rows)} app{'s' if len(sorted_rows) != 1 else ''} "
                f"running · {format_cpu(total_cpu)} CPU · "
                f"{format_memory(total_mem)} memory"
            )
            self.sort_pill.set_visible(True)
            self.freeze_pill.set_visible(True)
        else:
            self.stack.set_visible_child_name("empty")
            self.status_label.set_label("")
            self.sort_pill.set_visible(False)
            self.freeze_pill.set_visible(False)

        self.sort_pill.set_label(
            f"sorted by {_SORT_LABELS.get(self._sort_by, 'CPU')}"
        )
        self._update_collapse_button()

    def _wire_new_row(self, widget) -> None:
        if isinstance(widget, Adw.ExpanderRow):
            widget.connect("notify::expanded", self._on_expander_toggled)

    def _update_collapse_button(self) -> None:
        self.collapse_btn.set_visible(self._any_expanded())

    def _any_expanded(self) -> bool:
        return any(
            isinstance(w, Adw.ExpanderRow) and w.get_expanded()
            for w in self._rows.iter_widgets()
        )

    # ----- expander + freeze callbacks ----------------------------------

    def _on_expander_toggled(self, _row, _pspec) -> None:
        # Body click toggles the expander; we no longer touch sort or
        # scroll. The only side-effect is showing/hiding "Collapse all".
        self._update_collapse_button()

    def _on_freeze_toggled(self, active: bool) -> None:
        self._freeze_position = active
        # Force an immediate re-render so the toggle's effect is visible
        # without waiting for the next sample. ON: locks the current order.
        # OFF: returns to natural sort.
        if self._last_rows:
            self._render_rows(self._last_rows)

    def _on_collapse_all_clicked(self, _btn) -> None:
        for widget in self._rows.iter_widgets():
            if isinstance(widget, Adw.ExpanderRow) and widget.get_expanded():
                widget.set_expanded(False)

    def _on_listbox_row_activated(self, _listbox, row):
        # ExpanderRow consumes its own body clicks for toggling; the suffix
        # arrow button is the path to the detail page for multi-instance apps.
        if isinstance(row, Adw.ExpanderRow):
            return
        if hasattr(row, "app_id"):
            self._on_row_activated(row.app_id)
