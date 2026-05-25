"""Borderless, OS-level fullscreen image viewer with gallery navigation.

Opened from the detail page when the user clicks a screenshot thumbnail.

Layout: 25% left zone (previous), 50% center zone (close), 25% right zone (next).
Chevrons on the left/right zones fade in on hover. Arrow keys navigate; Escape
or `q` closes. With only one image, all zones close (no chevrons shown).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from .navigator import ImageNavigator


_CSS_LOADED = False
_VIEWER_CSS = b"""
.fullscreen-viewer { background-color: black; }
.viewer-zone {
    background: transparent;
    border: none;
    box-shadow: none;
    outline: none;
    padding: 0;
    min-width: 0;
    min-height: 0;
}
.viewer-zone:hover, .viewer-zone:focus { background: transparent; }
.viewer-chevron {
    opacity: 0;
    transition: opacity 180ms ease-out;
    color: white;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.7);
}
.viewer-chevron.visible { opacity: 0.85; }
"""


def _ensure_css(display):
    global _CSS_LOADED
    if _CSS_LOADED:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_VIEWER_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _CSS_LOADED = True


def _monitor_for(parent: Optional[Gtk.Window]) -> Optional[Gdk.Monitor]:
    """Pick the monitor where the parent window's surface lives."""
    if parent is not None:
        display = parent.get_display()
        surface = parent.get_surface()
        if surface is not None:
            mon = display.get_monitor_at_surface(surface)
            if mon is not None:
                return mon

    display = parent.get_display() if parent is not None else Gdk.Display.get_default()
    if display is None:
        return None

    monitors = display.get_monitors()
    if monitors and monitors.get_n_items() > 0:
        return monitors.get_item(0)
    return None


class ScreenshotViewer(Gtk.Window):
    """Fullscreen gallery viewer over a list of locally-cached image paths."""

    def __init__(
        self,
        images: List[Path],
        index: int = 0,
        transient_for: Optional[Gtk.Window] = None,
    ):
        super().__init__()
        self._nav: ImageNavigator[Path] = ImageNavigator(images, index)
        self._target_monitor: Optional[Gdk.Monitor] = None

        if transient_for is not None:
            self.set_transient_for(transient_for)
            self._target_monitor = _monitor_for(transient_for)

        self.set_decorated(False)
        self.add_css_class("fullscreen-viewer")
        _ensure_css(self.get_display())

        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_can_shrink(True)
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)

        overlay = Gtk.Overlay()
        overlay.set_child(self._picture)
        overlay.add_overlay(self._build_zone_grid())
        self.set_child(overlay)

        # Keyboard: Escape/q closes, arrows navigate.
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        self._refresh_picture()
        self.connect("map", self._enter_fullscreen)

    def _build_zone_grid(self) -> Gtk.Widget:
        # Equal-width 4 columns, content arranged 1 / 2 / 1 via column spans.
        grid = Gtk.Grid()
        grid.set_hexpand(True)
        grid.set_vexpand(True)
        grid.set_column_homogeneous(True)

        multi = self._nav.has_multiple

        left_chevron = self._make_chevron("go-previous-symbolic") if multi else None
        left = self._make_zone(
            child=left_chevron,
            on_click=self._on_prev if multi else self._close,
            hover_chevron=left_chevron,
        )
        grid.attach(left, 0, 0, 1, 1)

        center = self._make_zone(None, on_click=self._close, hover_chevron=None)
        grid.attach(center, 1, 0, 2, 1)

        right_chevron = self._make_chevron("go-next-symbolic") if multi else None
        right = self._make_zone(
            child=right_chevron,
            on_click=self._on_next if multi else self._close,
            hover_chevron=right_chevron,
        )
        grid.attach(right, 3, 0, 1, 1)

        return grid

    def _make_chevron(self, icon_name: str) -> Gtk.Image:
        img = Gtk.Image.new_from_icon_name(icon_name)
        img.set_pixel_size(72)
        img.add_css_class("viewer-chevron")
        img.set_valign(Gtk.Align.CENTER)
        img.set_halign(Gtk.Align.CENTER)
        return img

    def _make_zone(
        self,
        child: Optional[Gtk.Widget],
        on_click,
        hover_chevron: Optional[Gtk.Image],
    ) -> Gtk.Widget:
        button = Gtk.Button()
        button.add_css_class("viewer-zone")
        button.add_css_class("flat")
        button.set_hexpand(True)
        button.set_vexpand(True)
        if child is not None:
            button.set_child(child)
        button.connect("clicked", lambda *_: on_click())

        if hover_chevron is not None:
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", lambda *_: hover_chevron.add_css_class("visible"))
            motion.connect("leave", lambda *_: hover_chevron.remove_css_class("visible"))
            button.add_controller(motion)

        return button

    def _refresh_picture(self):
        current = self._nav.current()
        if current is not None:
            self._picture.set_filename(str(current))

    def _on_next(self):
        self._nav.go_next()
        self._refresh_picture()

    def _on_prev(self):
        self._nav.go_prev()
        self._refresh_picture()

    def _close(self):
        self.close()

    def _on_key(self, _ctrl, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_q, Gdk.KEY_Q):
            self.close()
            return True
        if self._nav.has_multiple:
            if keyval in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_space):
                self._on_next()
                return True
            if keyval in (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_BackSpace):
                self._on_prev()
                return True
        return False

    def _enter_fullscreen(self, *_args):
        if self._target_monitor is not None:
            self.fullscreen_on_monitor(self._target_monitor)
        else:
            self.fullscreen()
