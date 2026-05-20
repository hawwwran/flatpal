"""Running-flatpaks tab — live CPU/RSS per running app, refreshed every 2 s."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .constants import REFRESH_MS
from .running import RunningTracker, format_cpu, format_memory, sort_running


# Line the status row + dropdown up with the boxed-list rows below.
LIST_MAX_WIDTH = 900


# Values the user can pick in the inline interval dropdown. Order matters —
# it's the order the user sees in the menu.
INTERVAL_OPTIONS_SEC = (1, 2, 5, 10, 30)
INTERVAL_LABELS = tuple(f"{s} s" for s in INTERVAL_OPTIONS_SEC)


class RunningRow(Adw.ActionRow):
    def __init__(self, row: dict, installed_lookup: Callable[[str], Optional[dict]]):
        super().__init__()
        self.row = row
        self.app_id = row["id"]

        installed = installed_lookup(row["id"]) if installed_lookup else None
        name = (installed or {}).get("name") or row["id"]
        self.set_title(GLib.markup_escape_text(name))

        subtitle_bits = [row["id"]]
        if row["instances"] > 1:
            subtitle_bits.append(f"{row['instances']} instances")
        self.set_subtitle(GLib.markup_escape_text(" • ".join(subtitle_bits)))
        self.set_activatable(True)

        icon = Gtk.Image.new_from_icon_name(row["id"])
        icon.set_pixel_size(48)
        if not Gtk.IconTheme.get_for_display(self.get_display()).has_icon(row["id"]):
            icon.set_from_icon_name("application-x-executable")
        self.add_prefix(icon)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_valign(Gtk.Align.CENTER)
        meta.set_halign(Gtk.Align.END)

        cpu_label = Gtk.Label(label=f"CPU {format_cpu(row['cpu_percent'])}")
        cpu_label.add_css_class("caption")
        cpu_label.add_css_class("numeric")
        cpu_label.set_halign(Gtk.Align.END)
        meta.append(cpu_label)

        mem_label = Gtk.Label(label=f"RSS {format_memory(row['memory_bytes'])}")
        mem_label.add_css_class("caption")
        mem_label.add_css_class("numeric")
        mem_label.add_css_class("dim-label")
        mem_label.set_halign(Gtk.Align.END)
        meta.append(mem_label)
        self.add_suffix(meta)


class RunningPage(Gtk.Box):
    def __init__(
        self,
        on_row_activated: Callable[[dict], None],
        installed_lookup: Callable[[str], Optional[dict]],
        on_interval_changed: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_row_activated = on_row_activated
        self._installed_lookup = installed_lookup
        self._on_interval_changed = on_interval_changed
        self._tracker: Optional[RunningTracker] = None
        self._timeout_id: Optional[int] = None
        self._sort_by = "cpu"  # default — highest-CPU on top
        self._sample_in_flight = False  # one sampler at a time
        self._interval_seconds = REFRESH_MS // 1000  # mutable via set_interval()

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
        status_row.set_start_widget(self.status_label)

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
        self._interval_dropdown.set_tooltip_text("How often to re-sample CPU and RSS")
        self._interval_dropdown.connect(
            "notify::selected", self._on_interval_dropdown_changed
        )

        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        end_box.append(interval_caption)
        end_box.append(self._interval_dropdown)
        status_row.set_end_widget(end_box)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)

        empty = Adw.StatusPage(
            icon_name="utilities-system-monitor-symbolic",
            title="Nothing running",
            description="No Flatpak apps are running right now.",
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
        scrolled.set_child(self.listbox)
        self.stack.add_named(scrolled, "list")
        self.stack.set_visible_child_name("empty")

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
        outer_clamp.set_child(outer_box)
        outer_clamp.set_vexpand(True)
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

    def set_sort(self, key: str) -> None:
        if key == self._sort_by:
            return
        self._sort_by = key
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
        rows = sort_running(rows, self._sort_by)

        self._clear_listbox()
        for row in rows:
            self.listbox.append(RunningRow(row, self._installed_lookup))

        if rows:
            self.stack.set_visible_child_name("list")
            total_cpu = sum(r["cpu_percent"] for r in rows)
            total_mem = sum(r["memory_bytes"] for r in rows)
            sort_label = {
                "cpu": "CPU", "memory": "memory", "name": "name",
            }.get(self._sort_by, "CPU")
            self.status_label.set_label(
                f"{len(rows)} app{'s' if len(rows) != 1 else ''} running · "
                f"{format_cpu(total_cpu)} CPU · {format_memory(total_mem)} RSS · "
                f"sorted by {sort_label}"
            )
        else:
            self.stack.set_visible_child_name("empty")
            self.status_label.set_label("")
        return False

    def _clear_listbox(self):
        child = self.listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt

    def _on_listbox_row_activated(self, _listbox, row):
        if hasattr(row, "app_id"):
            self._on_row_activated(row.app_id)
