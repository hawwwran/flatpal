"""Row widgets for the Running tab — one per running app.

`RunningRow` is a single-sandbox row; `RunningExpanderRow` is a multi-sandbox
row that exposes its per-sandbox breakdown when expanded. Both implement
`.update(row)` so the parent page can refresh stats in place without
replacing the widget — which keeps in-flight hover tooltips alive across
the 2 s refresh tick. See `flatpal.row_cache.RowCache` for the differ that
orchestrates these updates.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .running import format_cpu, format_memory, format_relative_time
from .widgets import make_update_pill, update_tooltip


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
                except Exception:  # noqa: BLE001
                    # GTK detach race: widget already gone, nothing to do.
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


def expected_row_type(row: dict):
    return RunningExpanderRow if row.get("instances", 1) > 1 else RunningRow
