"""Detail-page screenshot strip: thumbnails + lazy download + fullscreen glue."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from .cache import get_cached_or_download
from .constants import THUMB_H, THUMB_W
from .screenshot_viewer import ScreenshotViewer


class Thumbnail(Gtk.Button):
    """One screenshot thumbnail. Starts as a spinner, becomes a Picture."""

    def __init__(self, app_id: str, url: str, on_click):
        super().__init__()
        self.set_size_request(THUMB_W, THUMB_H)
        self.add_css_class("flat")
        self.add_css_class("card")
        self._app_id = app_id
        self._url = url
        self.path: Optional[Path] = None
        self._on_click = on_click

        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(32, 32)
        self._spinner.start()
        self.set_child(self._spinner)

        self.connect("clicked", self._activate)

    def set_loaded(self, path: Path) -> None:
        self.path = path
        self._spinner.stop()  # release the tick callback before unparenting
        picture = Gtk.Picture.new_for_filename(str(path))
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_can_shrink(True)
        picture.set_size_request(THUMB_W, THUMB_H)
        self.set_child(picture)

    def set_failed(self) -> None:
        self._spinner.stop()
        icon = Gtk.Image.new_from_icon_name("image-missing-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("dim-label")
        self.set_child(icon)

    def _activate(self, _btn):
        if self.path is not None and self._on_click:
            self._on_click(self)


def _start_download_thread(app_id: str, url: str, thumb: Thumbnail) -> None:
    """Download the screenshot off the main thread, then update the thumb."""

    def worker():
        path = get_cached_or_download(app_id, url)

        def finish():
            # The user may have navigated away while we were downloading; the
            # thumbnail has been unparented in that case and updating it is
            # wasted work (and could spin up Picture/Pixbuf for nothing).
            if thumb.get_parent() is None:
                return False
            if path is not None:
                thumb.set_loaded(path)
            else:
                thumb.set_failed()
            return False

        GLib.idle_add(finish)

    t = threading.Thread(target=worker, daemon=True)
    t.start()


def build_screenshots_row(
    app_id: str, screenshots: list, parent_window: Gtk.Window,
) -> Gtk.Widget:
    """Horizontal scrollable strip of thumbnails; click any → fullscreen viewer.

    The thumbnails list is captured by the open-fullscreen closure so we can
    skip placeholders / failed downloads and start the gallery on the clicked
    thumb's index.
    """
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    heading = Gtk.Label(label="Screenshots")
    heading.set_halign(Gtk.Align.START)
    heading.add_css_class("heading")
    outer.append(heading)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    scroller.set_min_content_height(THUMB_H + 8)

    strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    strip.set_margin_bottom(6)
    thumbs: list = []

    def open_fullscreen(clicked: Thumbnail) -> None:
        loaded = [t for t in thumbs if t.path is not None]
        if not loaded:
            return
        paths = [t.path for t in loaded]
        try:
            start = loaded.index(clicked)
        except ValueError:
            start = 0
        ScreenshotViewer(paths, index=start, transient_for=parent_window).present()

    for shot in screenshots:
        thumb = Thumbnail(app_id, shot["source_url"], open_fullscreen)
        if shot.get("caption"):
            thumb.set_tooltip_text(shot["caption"])
        strip.append(thumb)
        thumbs.append(thumb)
        _start_download_thread(app_id, shot["source_url"], thumb)

    scroller.set_child(strip)
    outer.append(scroller)
    return outer
