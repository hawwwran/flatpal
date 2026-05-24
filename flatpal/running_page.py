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
    format_relative_time,
    order_with_freeze,
    sort_running,
)
from .widgets import (
    make_freeze_pill, make_sort_pill, make_update_pill, update_tooltip,
)


# Line the status row + dropdown up with the boxed-list rows below.
LIST_MAX_WIDTH = 900


# Values the user can pick in the inline interval dropdown. Order matters —
# it's the order the user sees in the menu.
INTERVAL_OPTIONS_SEC = (1, 2, 5, 10, 30)
INTERVAL_LABELS = tuple(f"{s} s" for s in INTERVAL_OPTIONS_SEC)


_SORT_LABELS = {"cpu": "CPU", "memory": "memory", "name": "name"}


def _build_app_icon(display, app_id: str) -> Gtk.Image:
    """Themed app icon, 48px, falling back to a generic when the theme has no match."""
    icon = Gtk.Image.new_from_icon_name(app_id)
    icon.set_pixel_size(48)
    if not Gtk.IconTheme.get_for_display(display).has_icon(app_id):
        icon.set_from_icon_name("application-x-executable")
    return icon


def _build_stats_box(
    cpu_percent: float, memory_bytes: int,
) -> tuple[Gtk.Box, Gtk.Label, Gtk.Label]:
    """Stacked CPU + memory labels — returns (box, cpu_label, mem_label).

    Callers hold onto the label refs so they can mutate them in place on the
    next sample, instead of throwing the whole row away. Keeping widgets alive
    is what lets the tooltip survive across refreshes.
    """
    meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    meta.set_valign(Gtk.Align.CENTER)
    meta.set_halign(Gtk.Align.END)

    cpu_label = Gtk.Label(label=f"CPU {format_cpu(cpu_percent)}")
    cpu_label.add_css_class("caption")
    cpu_label.add_css_class("numeric")
    cpu_label.set_halign(Gtk.Align.END)
    meta.append(cpu_label)

    mem_label = Gtk.Label(label=f"Memory {format_memory(memory_bytes)}")
    mem_label.add_css_class("caption")
    mem_label.add_css_class("numeric")
    mem_label.add_css_class("dim-label")
    mem_label.set_halign(Gtk.Align.END)
    meta.append(mem_label)
    return meta, cpu_label, mem_label


def _update_stats(
    cpu_label: Gtk.Label, mem_label: Gtk.Label,
    cpu_percent: float, memory_bytes: int,
) -> None:
    cpu_label.set_label(f"CPU {format_cpu(cpu_percent)}")
    mem_label.set_label(f"Memory {format_memory(memory_bytes)}")


def _app_title(row: dict, installed: Optional[dict]) -> str:
    name = (installed or {}).get("name") or row["id"]
    return GLib.markup_escape_text(name)


def _app_subtitle(row: dict) -> str:
    bits = [row["id"]]
    count = row.get("instances", 1)
    if count > 1:
        # "sandboxes" makes it explicit that this counts independent flatpak
        # sandbox processes — not windows. Single-instance GTK apps (most of
        # them) keep all their windows in one sandbox no matter how many.
        bits.append(f"{count} sandboxes")
    return GLib.markup_escape_text(" • ".join(bits))


def _sub_title_markup(cmdline, comm, pid) -> str:
    """Pango markup for the sub-row title: bold argv[0] basename + the rest.

    Adw.ActionRow titles render markup by default and ellipsize at end on a
    single line, so the full command is visible up to the row's width.
    """
    cmd = [s for s in (cmdline or []) if s]
    if cmd:
        head = cmd[0].rsplit("/", 1)[-1] or cmd[0]
        head_markup = f"<b>{GLib.markup_escape_text(head)}</b>"
        if len(cmd) > 1:
            rest = " ".join(cmd[1:])
            return f"{head_markup} {GLib.markup_escape_text(rest)}"
        return head_markup
    if comm:
        return f"<b>{GLib.markup_escape_text(str(comm))}</b>"
    return GLib.markup_escape_text(f"Process {pid if pid is not None else '?'}")


class _SubInstanceRow(Adw.ActionRow):
    """One sub-row inside RunningExpanderRow, showing a single sandbox.

    The title and tooltip are derived from cmdline (constant for the life of
    the process), so we set them once in __init__. Only the subtitle and
    stats labels change between ticks; `update()` mutates them in place so
    the row's tooltip survives mouse hover.
    """

    def __init__(self, sub: dict):
        super().__init__()
        self.pid = sub.get("pid")

        cmdline = sub.get("cmdline")
        self.set_title(_sub_title_markup(cmdline, sub.get("comm"), self.pid))
        # Adw.ActionRow's default lets long titles wrap to multiple lines and
        # push the row taller. Force a single line so end-ellipsis kicks in.
        self.set_title_lines(1)

        full_cmd = " ".join(s for s in (cmdline or []) if s)
        if full_cmd:
            self.set_tooltip_text(full_cmd)

        stats_box, self._cpu_label, self._mem_label = _build_stats_box(
            sub.get("cpu_percent", 0.0), sub.get("memory_bytes", 0),
        )
        self.add_suffix(stats_box)

        self.update(sub)

    def update(self, sub: dict) -> None:
        bits = [f"PID {sub.get('pid', '?')}"]
        rel = format_relative_time(sub.get("started_at"))
        if rel:
            bits.append(f"started {rel}")
        branch = sub.get("branch")
        if branch and branch != "stable":
            bits.append(branch)
        self.set_subtitle(GLib.markup_escape_text(" · ".join(bits)))
        _update_stats(
            self._cpu_label, self._mem_label,
            sub.get("cpu_percent", 0.0), sub.get("memory_bytes", 0),
        )


class RunningRow(Adw.ActionRow):
    """Single-sandbox row. Click anywhere → opens the detail page.

    Mirrors the suffix layout of `RunningExpanderRow` so single- and
    multi-sandbox rows align visually:
      `[update] [stats] [detail arrow] [bullet]`
    where the bullet stands in for the expander chevron that ExpanderRow
    draws for multi-sandbox apps. The bullet's tooltip explains the slot's
    meaning ("single flatpak sandbox running"). Note that Adw.ActionRow's
    `add_suffix` *appends* (unlike Adw.ExpanderRow which prepends), so the
    first call here lands on the left.

    `update(row)` mutates the title/subtitle/stats in place; the icon is set
    once because it depends on app_id which is stable for the row's lifetime.
    """

    def __init__(
        self,
        row: dict,
        installed_lookup: Callable[[str], Optional[dict]],
        on_open_detail: Callable[[str], None],
        update_info: Optional[dict] = None,
    ):
        super().__init__()
        self.app_id = row["id"]
        self._installed_lookup = installed_lookup
        self.set_activatable(True)

        self.add_prefix(_build_app_icon(self.get_display(), row["id"]))

        if update_info:
            installed = installed_lookup(row["id"]) if installed_lookup else None
            self.add_suffix(make_update_pill(
                tooltip=update_tooltip(
                    (installed or {}).get("version"), update_info,
                ),
            ))

        stats_box, self._cpu_label, self._mem_label = _build_stats_box(
            row["cpu_percent"], row["memory_bytes"],
        )

        open_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
        open_btn.add_css_class("flat")
        open_btn.set_valign(Gtk.Align.CENTER)
        open_btn.set_tooltip_text("Open details page")
        open_btn.connect("clicked", lambda _b: on_open_detail(self.app_id))

        bullet = Gtk.Label(label="•")
        bullet.add_css_class("dim-label")
        bullet.set_valign(Gtk.Align.CENTER)
        # Match the libadwaita expander-arrow image's natural width (19 px on
        # GNOME 50) and centre the dot inside that slot, so single- and
        # multi-sandbox rows share the same right-edge column. Without the
        # forced width the bullet's natural ~8 px shifts everything to its
        # left by 11 px relative to ExpanderRow's chevron.
        bullet.set_size_request(19, -1)
        bullet.set_xalign(0.5)
        bullet.set_tooltip_text("Single flatpak sandbox running")

        self.add_suffix(stats_box)
        self.add_suffix(open_btn)
        self.add_suffix(bullet)

        self.update(row)

    def update(self, row: dict) -> None:
        installed = self._installed_lookup(row["id"]) if self._installed_lookup else None
        self.set_title(_app_title(row, installed))
        self.set_subtitle(_app_subtitle(row))
        _update_stats(
            self._cpu_label, self._mem_label,
            row["cpu_percent"], row["memory_bytes"],
        )


class RunningExpanderRow(Adw.ExpanderRow):
    """Multi-sandbox row. Body click toggles the expander; suffix arrow opens detail.

    Sub-rows are kept in `_sub_cache` keyed by PID so a refresh updates each
    sub-row's stats in place instead of destroying and recreating it. New
    sandboxes are appended; vanished ones removed; existing ones mutated.
    """

    def __init__(
        self,
        row: dict,
        installed_lookup: Callable[[str], Optional[dict]],
        on_open_detail: Callable[[str], None],
        update_info: Optional[dict] = None,
    ):
        super().__init__()
        self.app_id = row["id"]
        self._installed_lookup = installed_lookup
        # pid → _SubInstanceRow. Kept across ticks; hover-tooltips survive.
        self._sub_cache: dict = {}

        self.add_prefix(_build_app_icon(self.get_display(), row["id"]))

        # Suffix order: stats on the left, then the detail-open arrow last so
        # it sits immediately next to the expander chevron that
        # Adw.ExpanderRow draws on the far right. Body click toggles the
        # expander, so we need this dedicated affordance to reach the detail
        # page; keeping it next to the chevron groups the two "navigate"
        # controls together visually.
        stats_box, self._cpu_label, self._mem_label = _build_stats_box(
            row["cpu_percent"], row["memory_bytes"],
        )
        open_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
        open_btn.add_css_class("flat")
        open_btn.set_valign(Gtk.Align.CENTER)
        open_btn.set_tooltip_text("Open details page")
        open_btn.connect("clicked", lambda _b: on_open_detail(self.app_id))

        self.add_suffix(open_btn)
        self.add_suffix(stats_box)
        # Adw.ExpanderRow.add_suffix *prepends*, so this Update pill ends
        # up leftmost in the suffix area — same visual order as RunningRow:
        # [update] [stats] [arrow] [chevron].
        if update_info:
            installed = installed_lookup(row["id"]) if installed_lookup else None
            self.add_suffix(make_update_pill(
                tooltip=update_tooltip(
                    (installed or {}).get("version"), update_info,
                ),
            ))

        self.update(row)

    def update(self, row: dict) -> None:
        installed = self._installed_lookup(row["id"]) if self._installed_lookup else None
        self.set_title(_app_title(row, installed))
        self.set_subtitle(_app_subtitle(row))
        self.set_tooltip_text(
            f"{row.get('instances', 1)} separate flatpak sandboxes are running "
            "for this app. Each one is a distinct process tree — expand the "
            "row to see per-sandbox CPU and memory."
        )
        _update_stats(
            self._cpu_label, self._mem_label,
            row["cpu_percent"], row["memory_bytes"],
        )

        # Sub-row diff: update existing by PID, append new ones, remove
        # vanished ones. Sub-instances are sorted by started_at in
        # running.py, so newly-arrived sandboxes naturally end up last —
        # which is also where add_row puts them.
        new_subs = row.get("sub_instances", [])
        new_pids = {s["pid"] for s in new_subs}
        for pid in list(self._sub_cache.keys()):
            if pid not in new_pids:
                widget = self._sub_cache.pop(pid)
                try:
                    self.remove(widget)
                except Exception:
                    pass
        for sub in new_subs:
            pid = sub["pid"]
            existing = self._sub_cache.get(pid)
            if existing is not None:
                existing.update(sub)
            else:
                new_sub = _SubInstanceRow(sub)
                self._sub_cache[pid] = new_sub
                self.add_row(new_sub)


def make_running_row(
    row: dict,
    installed_lookup: Callable[[str], Optional[dict]],
    on_open_detail: Callable[[str], None],
    update_info: Optional[dict] = None,
):
    if row.get("instances", 1) > 1:
        return RunningExpanderRow(
            row, installed_lookup, on_open_detail, update_info=update_info,
        )
    return RunningRow(
        row, installed_lookup, on_open_detail, update_info=update_info,
    )


def _expected_row_type(row: dict):
    return RunningExpanderRow if row.get("instances", 1) > 1 else RunningRow


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
        # Position-freeze state. When True, _render_rows reuses _rendered_order
        # to keep apps in place across refreshes; new apps are appended in
        # natural sort order, vanished apps drop out.
        self._freeze_position = False
        self._rendered_order: list[str] = []
        # Row widgets kept across ticks. Keeping them alive prevents the
        # tooltip-on-hover from dying every 2 s when the listbox is rebuilt.
        self._row_cache: dict = {}

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

        self.status_label = Gtk.Label()
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_xalign(0.0)

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
        # Default to loading; _render_rows flips to "list" once a sample lands.
        self.stack.set_visible_child_name("loading")

        # Single outer Adw.Clamp wrapping status_row + stack so both share the
        # exact same 900px width allocation. Wrapping each child in its own
        # clamp produces subtly different positions because the stack adds an
        # extra layer of allocation (and the listbox inside it has hexpand
        # that propagates upward differently than the status row's centerbox).
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer_box.append(status_row)
        outer_box.append(self.stack)

        outer_clamp = Adw.Clamp()
        outer_clamp.set_maximum_size(LIST_MAX_WIDTH)
        # tightening_threshold = max disables AdwClamp's cubic-ease window so
        # the child width is purely min(for_size, max). Combined with hexpand,
        # the result is "fixed at max on wide windows; shrinks linearly only
        # when the window itself is narrower than max." See explore_page.py.
        outer_clamp.set_tightening_threshold(LIST_MAX_WIDTH)
        outer_clamp.set_child(outer_box)
        outer_clamp.set_vexpand(True)
        outer_clamp.set_hexpand(True)
        self.append(outer_clamp)

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
        worker lands. Clears `_row_cache` so the row-diff path in
        `_render_rows` falls into the cold-path branch that reconstructs
        each row via `make_running_row` (which re-reads the lookup), then
        rerenders against the cached `_last_rows` so no extra `flatpak
        ps` sampling happens just to add a badge.
        """
        for widget in list(self._iter_row_widgets()):
            self.listbox.remove(widget)
        self._row_cache.clear()
        if self._last_rows:
            self._render_rows(self._last_rows)

    def set_sort(self, key: str) -> None:
        """User-driven sort change. Re-renders from the cached sample."""
        if key == self._sort_by:
            return
        self._sort_by = key
        # A new sort retires the previously-frozen order — the user explicitly
        # asked for a different layout, so honour it. The render captures a
        # new order; if freeze is still on, that new order becomes the pin.
        self._rendered_order = []
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
        if self._freeze_position and self._rendered_order:
            sorted_rows = order_with_freeze(
                list(rows), self._rendered_order, natural,
            )
        else:
            sorted_rows = natural(list(rows))
        self._rendered_order = [r["id"] for r in sorted_rows]

        new_data_by_id = {r["id"]: r for r in sorted_rows}
        new_ids = set(new_data_by_id.keys())

        # 1) Apps that vanished from the bus → drop their widgets.
        for app_id in list(self._row_cache.keys()):
            if app_id not in new_ids:
                widget = self._row_cache.pop(app_id)
                try:
                    self.listbox.remove(widget)
                except Exception:
                    pass

        # 2) Apps whose sandbox count flipped between 1 and >1 need a
        #    different widget class (ActionRow ↔ ExpanderRow). Evict and
        #    let step 4 recreate them.
        for app_id in list(self._row_cache.keys()):
            if not isinstance(
                self._row_cache[app_id], _expected_row_type(new_data_by_id[app_id])
            ):
                widget = self._row_cache.pop(app_id)
                try:
                    self.listbox.remove(widget)
                except Exception:
                    pass

        # 3) Decide whether we can update in place.
        current_order = [w.app_id for w in self._iter_row_widgets()]
        if current_order == self._rendered_order and current_order:
            # Hot path: same apps, same order. Mutate each row's labels.
            # No listbox structural change — tooltips and hover states all
            # survive the refresh.
            for app_id in self._rendered_order:
                self._row_cache[app_id].update(new_data_by_id[app_id])
        else:
            # Cold path: order changed or rows added. Detach existing
            # (widgets stay alive via the cache), then re-append in the new
            # order, updating or creating each.
            for widget in list(self._iter_row_widgets()):
                self.listbox.remove(widget)
            for app_id in self._rendered_order:
                existing = self._row_cache.get(app_id)
                if existing is not None:
                    existing.update(new_data_by_id[app_id])
                else:
                    existing = make_running_row(
                        new_data_by_id[app_id],
                        self._installed_lookup,
                        self._on_row_activated,
                        update_info=self._updates_lookup(app_id),
                    )
                    if isinstance(existing, Adw.ExpanderRow):
                        existing.connect(
                            "notify::expanded", self._on_expander_toggled,
                        )
                    self._row_cache[app_id] = existing
                self.listbox.append(existing)

        # 4) Status row + pill + empty stack.
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

    def _iter_row_widgets(self):
        child = self.listbox.get_first_child()
        while child is not None:
            if hasattr(child, "app_id"):
                yield child
            child = child.get_next_sibling()

    def _update_collapse_button(self) -> None:
        self.collapse_btn.set_visible(self._any_expanded())

    def _any_expanded(self) -> bool:
        child = self.listbox.get_first_child()
        while child is not None:
            if isinstance(child, Adw.ExpanderRow) and child.get_expanded():
                return True
            child = child.get_next_sibling()
        return False

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
        child = self.listbox.get_first_child()
        while child is not None:
            if isinstance(child, Adw.ExpanderRow) and child.get_expanded():
                child.set_expanded(False)
            child = child.get_next_sibling()

    def _on_listbox_row_activated(self, _listbox, row):
        # ExpanderRow consumes its own body clicks for toggling; the suffix
        # arrow button is the path to the detail page for multi-instance apps.
        if isinstance(row, Adw.ExpanderRow):
            return
        if hasattr(row, "app_id"):
            self._on_row_activated(row.app_id)
