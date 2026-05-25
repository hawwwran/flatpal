"""Data layer for the Explore tab — catalog + popularity loading."""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

from .catalog import load_catalog
from .metainfo import system_lang
from .popularity import load_popular, popularity_index


class CatalogManager:
    """Owns the Flathub catalog + popularity caches and their worker threads.

    Both fetches run in daemon threads and hop back to the GTK main loop via
    GLib.idle_add. `on_loaded` is called from the main loop whenever a fetch
    finishes (or progress updates) so the UI can re-render.
    """

    def __init__(self, on_loaded: Callable[[], None]):
        self._on_loaded = on_loaded
        self.catalog: dict = {}
        self.catalog_loaded = False
        self.catalog_loading = False
        self.popularity_hits: list = []
        self.popularity_idx: dict = {}
        self.popularity_loaded = False
        self.popularity_loading = False
        self.popularity_pages_done = 0
        self.popularity_pages_total = 0

    def ensure_catalog(self) -> None:
        if self.catalog_loaded or self.catalog_loading:
            return
        self.catalog_loading = True
        lang = system_lang()

        def worker():
            # Catch *anything* so the finish() callback always fires and the
            # catalog_loading flag clears. Without this, an exception inside
            # load_catalog (or any helper it calls) leaves the Explore tab
            # stuck on the loading spinner forever.
            try:
                data = load_catalog(lang=lang)
            except Exception:
                data = {}

            def finish():
                self.catalog = data
                self.catalog_loaded = True
                self.catalog_loading = False
                self._on_loaded()
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def ensure_popularity(self) -> None:
        if self.popularity_loaded or self.popularity_loading:
            return
        self.popularity_loading = True
        self.popularity_pages_done = 0
        self.popularity_pages_total = 0

        def on_progress(done, total, _hits):
            def update():
                self.popularity_pages_done = done
                self.popularity_pages_total = total
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
                self.popularity_hits = hits
                self.popularity_idx = idx
                self.popularity_loaded = True
                self.popularity_loading = False
                self._on_loaded()
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def retry_popularity(self) -> None:
        """Re-arm the popularity fetch after a network failure.

        `ensure_popularity` short-circuits when popularity_loaded is true,
        so drop the loaded flag (and the empty hits/index that landed on the
        previous attempt) before re-firing the worker.
        """
        self.popularity_loaded = False
        self.popularity_loading = False
        self.popularity_hits = []
        self.popularity_idx = {}
        self.popularity_pages_done = 0
        self.popularity_pages_total = 0
        self.ensure_popularity()
