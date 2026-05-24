"""Detail page for a single installed Flatpak app.

Loaded by `app.FlatpalWindow` when a row is activated. Pulls metadata from
AppStream metainfo XML on disk (via flatpal.metainfo) and sandbox
permissions from `flatpak info -m` (via flatpal.permissions). Screenshots
are downloaded lazily into the on-disk cache and swapped in once ready.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .cache import get_cached_or_download
from .constants import THUMB_H, THUMB_W
from .core import format_date
from .host import host_cmd
from .metainfo import load_metainfo, system_lang
from .permissions import parse_flatpak_metadata, summarize_permissions
from .screenshot_viewer import ScreenshotViewer
from .updates import releases_since


def open_in_software(app_id: str) -> None:
    try:
        subprocess.Popen(
            ["gnome-software", f"--details={app_id}"],
            start_new_session=True,
        )
    except FileNotFoundError:
        subprocess.Popen(["xdg-open", f"appstream://{app_id}"], start_new_session=True)


def run_flatpak_app(app_id: str) -> None:
    """Launch the installed flatpak in its sandbox (background, detached).

    Inside our own sandbox this routes through flatpak-spawn --host so the
    target app runs in *its* sandbox on the host, not nested inside ours.
    """
    subprocess.Popen(
        host_cmd(["flatpak", "run", app_id]),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _open_url(url: str) -> None:
    subprocess.Popen(["xdg-open", url], start_new_session=True)


def _link_row(title: str, url: str) -> Adw.ActionRow:
    """Adw.ActionRow showing a URL with a trailing open-in-browser button.

    Clicking the row body or the button both open the URL via xdg-open. Both
    title and url are run through markup_escape_text — URLs frequently carry
    `&` in query strings, which would otherwise produce invalid Pango markup
    and a blank subtitle.
    """
    row = Adw.ActionRow()
    row.set_title(GLib.markup_escape_text(title))
    row.set_subtitle(GLib.markup_escape_text(url))
    row.set_subtitle_selectable(True)

    btn = Gtk.Button.new_from_icon_name("adw-external-link-symbolic")
    btn.set_tooltip_text("Open in browser")
    btn.add_css_class("flat")
    btn.set_valign(Gtk.Align.CENTER)
    btn.connect("clicked", lambda *_: _open_url(url))
    row.add_suffix(btn)
    row.set_activatable_widget(btn)
    return row


def _load_permissions(app_id: str) -> list:
    """Run `flatpak info -m` for app_id and return the permission summary."""
    try:
        result = subprocess.run(
            host_cmd(["flatpak", "info", "-m", app_id]),
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return summarize_permissions(parse_flatpak_metadata(result.stdout))


class _Thumbnail(Gtk.Button):
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


def _start_download_thread(app_id: str, url: str, thumb: _Thumbnail) -> None:
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


class DetailPage(Adw.NavigationPage):
    """Per-app detail view. Construct via `from_installed()` or `from_catalog()`."""

    @classmethod
    def from_installed(cls, app: dict, parent_window: Gtk.Window) -> "DetailPage":
        """Build a detail page for an app already deployed on this machine.

        `app` comes from `core.fetch_apps()` and carries version, size,
        install-date, branch, origin. Metainfo is read from the on-disk
        AppStream XML, and sandbox permissions from `flatpak info -m`.
        """
        meta = load_metainfo(app["id"], lang=system_lang())

        # The locally-installed metainfo only lists releases up to whatever
        # version was current at install time — for the "what's new since
        # installed" diff (and the Recent releases section) we want every
        # release the remote has, including the ones newer than what's
        # deployed locally. The Flathub aggregated catalog reflects the
        # remote's current state, so prefer its release list when available.
        #
        # Race: the catalog is loaded in a background thread from
        # FlatpalWindow startup (~1 s of local IO). A detail page opened
        # within that window misses the override and falls back to the
        # local metainfo's release list — the update box still surfaces
        # the version diff, just without the "What's new since …" body.
        explore = getattr(parent_window, "explore_page", None)
        catalog_entry = explore.catalog_app(app["id"]) if explore else None
        if catalog_entry and catalog_entry.get("releases"):
            meta = dict(meta)
            meta["releases"] = catalog_entry["releases"]

        update_info = None
        lookup = getattr(parent_window, "updates_lookup", None)
        if callable(lookup):
            update_info = lookup(app["id"])
        return cls(
            app=app, parent_window=parent_window, installed=True, meta=meta,
            update_info=update_info,
        )

    @classmethod
    def from_catalog(
        cls, entry: dict, parent_window: Gtk.Window
    ) -> "DetailPage":
        """Build a detail page for a not-installed app from a Flathub catalog entry.

        `entry` comes from `catalog.load_catalog()` and has the same shape as
        `metainfo.parse_metainfo()` output plus a `cached_icon` path. The
        synthesised `app` dict has only `id` and `name` populated — the
        installed-only fields (version, size, …) stay empty and the About
        group hides them.
        """
        app = {
            "id": entry["id"],
            "name": entry.get("name") or entry["id"],
            "version": "",
            "branch": "",
            "origin": "flathub",
            "installation": "",
            "size_str": "",
            "size_bytes": 0,
            "installed": None,
        }
        return cls(
            app=app, parent_window=parent_window, installed=False, meta=entry,
            update_info=None,
        )

    def __init__(
        self,
        app: dict,
        parent_window: Gtk.Window,
        *,
        installed: bool,
        meta: dict,
        update_info: Optional[dict] = None,
    ):
        super().__init__()
        self.app = app
        self.parent_window = parent_window
        self.installed = installed
        self.update_info = update_info
        self.set_title(app["name"])
        self.set_tag(f"detail-{app['id']}")

        permissions = _load_permissions(app["id"]) if self.installed else []

        # ToolbarView: header on top, scrollable content beneath.
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self._build_header(app))

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(880)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        body.set_margin_top(24)
        body.set_margin_bottom(32)
        body.set_margin_start(16)
        body.set_margin_end(16)

        body.append(self._build_hero(app, meta))

        # Update box sits directly under the hero so the "at a glance" diff
        # (current → new version + release notes since installed) is the
        # first thing the user reads on a detail page that has a pending
        # update. Skipped when there's no available update or the app is
        # being viewed from the catalog (Open in Software handles that flow).
        if self.installed and self.update_info:
            body.append(self._build_update_box(app, meta, self.update_info))

        if meta["screenshots"]:
            body.append(self._build_screenshots_row(app["id"], meta["screenshots"]))

        if self.installed:
            body.append(self._build_actions_row(app))

        description = meta["description_markup"]
        if description:
            body.append(self._build_description(description))

        body.append(self._build_about_group(app, meta))

        if self.installed and permissions:
            body.append(self._build_permissions_group(permissions))

        if meta["releases"]:
            body.append(self._build_releases_group(meta["releases"]))

        clamp.set_child(body)
        scrolled.set_child(clamp)
        toolbar_view.set_content(scrolled)

        self.set_child(toolbar_view)

    # ----- builders --------------------------------------------------------

    def _build_header(self, app: dict) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=app["name"], subtitle=app["id"]))

        button = Gtk.Button()
        button.add_css_class("suggested-action")
        button_content = Adw.ButtonContent(
            icon_name="system-software-install-symbolic",
            label="Open in Software",
        )
        button.set_child(button_content)
        button.set_tooltip_text(
            "Open this app's page in GNOME Software (or your default appstream:// handler)"
        )
        button.connect("clicked", lambda *_: open_in_software(app["id"]))
        header.pack_end(button)

        return header

    def _build_hero(self, app: dict, meta: dict) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)

        # Icon resolution chain: IconTheme (installed apps register theirs) →
        # cached Flathub icon (catalog entry) → generic fallback.
        icon = Gtk.Image()
        icon.set_pixel_size(128)
        icon.set_valign(Gtk.Align.START)
        if Gtk.IconTheme.get_for_display(self.get_display()).has_icon(app["id"]):
            icon.set_from_icon_name(app["id"])
        else:
            cached = meta.get("cached_icon")
            if cached and Path(cached).is_file():
                icon.set_from_file(str(cached))
            else:
                icon.set_from_icon_name("application-x-executable")
        box.append(icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)

        name = Gtk.Label(label=meta["name"] or app["name"])
        name.set_halign(Gtk.Align.START)
        name.add_css_class("title-1")
        name.set_wrap(True)
        text.append(name)

        if meta["developer_name"]:
            dev = Gtk.Label(label=meta["developer_name"])
            dev.set_halign(Gtk.Align.START)
            dev.add_css_class("dim-label")
            text.append(dev)

        if meta["summary"]:
            summary = Gtk.Label(label=meta["summary"])
            summary.set_halign(Gtk.Align.START)
            summary.set_wrap(True)
            summary.add_css_class("title-4")
            text.append(summary)

        box.append(text)
        return box

    def _build_screenshots_row(self, app_id: str, screenshots: list) -> Gtk.Widget:
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
        self._thumbnails: list = []
        for shot in screenshots:
            thumb = _Thumbnail(app_id, shot["source_url"], self._open_fullscreen)
            if shot.get("caption"):
                thumb.set_tooltip_text(shot["caption"])
            strip.append(thumb)
            self._thumbnails.append(thumb)
            _start_download_thread(app_id, shot["source_url"], thumb)

        scroller.set_child(strip)
        outer.append(scroller)
        return outer

    def _build_actions_row(self, app: dict) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.START)

        open_btn = Gtk.Button()
        open_btn.add_css_class("suggested-action")
        open_btn.add_css_class("pill")
        open_btn.set_child(Adw.ButtonContent(
            icon_name="media-playback-start-symbolic",
            label="Open app",
        ))
        open_btn.set_tooltip_text(f"Launch {app['name']} (flatpak run {app['id']})")
        open_btn.connect("clicked", lambda *_: run_flatpak_app(app["id"]))
        row.append(open_btn)
        return row

    def _build_update_box(
        self, app: dict, meta: dict, update_info: dict,
    ) -> Gtk.Widget:
        """Terracotta-tinted callout: current vs new version + release notes.

        Lives at the top of the detail body (just under the hero) so the
        diff is the first thing the user sees on an updateable app. Release
        notes are inlined (not expanders) so the "what's new since
        installed" content is readable without an extra click.
        """
        new_v = update_info.get("version") or "?"
        origin = update_info.get("origin") or "remote"
        current = app.get("version") or "?"

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.add_css_class("flatpal-update-card")

        header = Gtk.Label(label="Update available")
        header.set_xalign(0.0)
        header.add_css_class("title-3")
        outer.append(header)

        diff = Gtk.Label(label=f"{current} → {new_v}   ·   on {origin}")
        diff.set_xalign(0.0)
        diff.add_css_class("heading")
        outer.append(diff)

        # "What's new since v0.1.0" — release notes slice from the metainfo
        # for everything that landed after the installed version. Empty
        # `releases_since` (no release tags, or installed == latest) leaves
        # only the version-diff header above.
        new_releases = releases_since(meta.get("releases") or [], current)
        if new_releases:
            since = Gtk.Label(label=f"What's new since {current}")
            since.set_xalign(0.0)
            since.add_css_class("dim-label")
            since.add_css_class("caption-heading")
            since.set_margin_top(8)
            outer.append(since)

            for rel in new_releases:
                ver_label = Gtk.Label()
                ver_label.set_markup(
                    f"<b>{GLib.markup_escape_text(rel['version'] or '—')}</b>"
                    + (
                        f"  <span alpha='65%'>· {GLib.markup_escape_text(rel['date'])}</span>"
                        if rel.get("date") else ""
                    )
                )
                ver_label.set_xalign(0.0)
                ver_label.set_margin_top(6)
                outer.append(ver_label)

                if rel.get("description_markup"):
                    body = Gtk.Label(label=rel["description_markup"])
                    body.set_wrap(True)
                    body.set_xalign(0.0)
                    body.add_css_class("body")
                    outer.append(body)
        elif current == "?":
            # Installed version unknown (rare) → no diff possible. Tell the
            # user explicitly so the empty box isn't mysterious.
            note = Gtk.Label(label="Installed version unknown — open Software to view release notes.")
            note.set_xalign(0.0)
            note.add_css_class("dim-label")
            note.set_wrap(True)
            outer.append(note)

        return outer

    def _build_description(self, markup: str) -> Gtk.Widget:
        # Wrap the description in a .card surface so it reads as a distinct
        # content block between the actions row and the metadata groups.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        card.set_margin_top(0)

        label = Gtk.Label(label=markup)
        label.set_halign(Gtk.Align.START)
        label.set_wrap(True)
        label.set_xalign(0.0)
        label.set_selectable(True)
        label.set_margin_top(16)
        label.set_margin_bottom(16)
        label.set_margin_start(18)
        label.set_margin_end(18)
        card.append(label)
        return card

    def _build_about_group(self, app: dict, meta: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("About")

        def row(title: str, value: str, copyable: bool = False):
            r = Adw.ActionRow()
            r.set_title(title)
            r.set_subtitle(GLib.markup_escape_text(value) if value else "—")
            r.set_subtitle_selectable(copyable)
            return r

        if self.installed:
            # Verbose version line surfaces both sides of an update at the
            # canonical About location — useful even for users who scroll
            # past the top-of-page update box. The row's title flips from
            # the bare "Version" to "Version (update available)" so it's
            # scannable in a list of plain rows.
            if self.update_info:
                new_v = self.update_info.get("version") or "?"
                origin = self.update_info.get("origin") or "remote"
                current = app.get("version") or "?"
                ver_row = row(
                    "Version (update available)",
                    f"{current} → {new_v}  (on {origin})",
                )
            else:
                ver_row = row("Version", app.get("version", ""))
            group.add(ver_row)
            group.add(row("Size", app.get("size_str", "")))
            group.add(row("Installed", format_date(app.get("installed"))))
        else:
            status = row("Status", "Not installed")
            status.add_css_class("dim-label")
            group.add(status)

        group.add(row("App ID", app["id"], copyable=True))

        if self.installed:
            branch_origin = " / ".join(
                v for v in (app.get("branch"), app.get("origin")) if v
            )
            if branch_origin:
                group.add(row("Branch / Origin", branch_origin))

        if meta.get("project_license"):
            group.add(row("License", meta["project_license"]))

        if meta.get("categories"):
            group.add(row("Categories", ", ".join(meta["categories"])))

        urls = meta.get("urls", {})
        if urls.get("homepage"):
            group.add(_link_row("Homepage", urls["homepage"]))
        if urls.get("help"):
            group.add(_link_row("Help", urls["help"]))
        if urls.get("bugtracker"):
            group.add(_link_row("Report an issue", urls["bugtracker"]))
        if urls.get("donation"):
            group.add(_link_row("Donate", urls["donation"]))

        return group

    def _build_permissions_group(self, rows: list) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Sandbox permissions")
        group.set_description(
            "What this app can reach outside its sandbox."
        )

        for r in rows:
            row = Adw.ActionRow()
            row.set_title(r["label"])
            row.set_subtitle(r["value"])
            icon = Gtk.Image.new_from_icon_name(r["icon"])
            icon.set_pixel_size(20)
            if not r["granted"]:
                row.add_css_class("dim-label")
                icon.add_css_class("dim-label")
            row.add_prefix(icon)
            group.add(row)

        return group

    def _build_releases_group(self, releases: list) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Recent releases")

        for rel in releases:
            label = rel["version"] or "—"
            subtitle = rel["date"] or ""
            if rel.get("description_markup"):
                row = Adw.ExpanderRow()
                row.set_title(label)
                row.set_subtitle(subtitle)
                inner = Gtk.Label(label=rel["description_markup"])
                inner.set_wrap(True)
                inner.set_xalign(0.0)
                inner.set_margin_top(6)
                inner.set_margin_bottom(6)
                inner.set_margin_start(12)
                inner.set_margin_end(12)
                row.add_row(inner)
            else:
                row = Adw.ActionRow()
                row.set_title(label)
                row.set_subtitle(subtitle)
            group.add(row)

        return group

    # ----- behaviour -------------------------------------------------------

    def _open_fullscreen(self, clicked_thumb: "_Thumbnail") -> None:
        """Open the viewer over every thumbnail that has finished downloading.

        Skips placeholders / failed downloads so the gallery only navigates
        between images that actually exist on disk.
        """
        loaded = [t for t in getattr(self, "_thumbnails", []) if t.path is not None]
        if not loaded:
            return
        paths = [t.path for t in loaded]
        try:
            start = loaded.index(clicked_thumb)
        except ValueError:
            start = 0
        viewer = ScreenshotViewer(paths, index=start, transient_for=self.parent_window)
        viewer.present()
