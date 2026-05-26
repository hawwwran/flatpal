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
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .core import fetch_remote_options, fix_remote_no_enumerate, format_date
from .downgrade_runner import run_downgrade, run_unmask
from .host import host_cmd
from .metainfo import load_metainfo, system_lang
from .permissions import parse_flatpak_metadata, summarize_permissions
from .release_notes import (
    build_recent_releases_placeholder,
    build_version_history_group,
    populate_recent_releases,
)
from .remote_log import fetch_current_commit, fetch_is_masked, fetch_log, parse_log
from .screenshots import build_screenshots_row
from .update_runner import run_update
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
    title and url are run through markup_escape_text; URLs frequently carry
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


class DetailPage(Adw.NavigationPage):
    """Per-app detail view. Construct via `from_installed()` or `from_catalog()`."""

    @classmethod
    def from_installed(
        cls,
        app: dict,
        parent_window: Gtk.Window,
        *,
        catalog_lookup: Optional[Callable[[str], Optional[dict]]] = None,
        updates_lookup: Optional[Callable[[str], Optional[dict]]] = None,
    ) -> "DetailPage":
        """Build a detail page for an app already deployed on this machine.

        `app` comes from `core.fetch_apps()` and carries version, size,
        install-date, branch, origin. Metainfo is read from the on-disk
        AppStream XML, and sandbox permissions from `flatpak info -m`.

        `catalog_lookup(app_id) -> dict | None` overrides the locally-
        installed metainfo's release list with the Flathub catalog's view
        when available; the catalog reflects the remote's current state
        so the "What's new since {installed}" body picks up releases that
        landed after the local install. Race: the catalog loads from a
        background worker (~1 s of local IO); a detail page opened within
        that window falls back to the local metainfo's list and the update
        box still surfaces the version diff, just without the body.

        `updates_lookup(app_id) -> dict | None` provides the per-app update
        record from the startup background fetch; used to populate the
        update card under the hero.
        """
        meta = load_metainfo(app["id"], lang=system_lang())

        catalog_entry = catalog_lookup(app["id"]) if catalog_lookup else None
        if catalog_entry and catalog_entry.get("releases"):
            meta = dict(meta)
            meta["releases"] = catalog_entry["releases"]

        update_info = updates_lookup(app["id"]) if updates_lookup else None
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
        synthesised `app` dict has only `id` and `name` populated; the
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
        # Populated only when this page renders an update card; the click
        # handler and the post-success state-flip read these refs.
        self._update_card: Optional[Gtk.Box] = None
        self._update_button: Optional[Gtk.Button] = None
        self._update_version_row: Optional[Adw.ActionRow] = None
        # Recent releases (OSTree commit history) downgrade wiring.
        # `_commits_group` is the section the worker populates; the
        # placeholder row is replaced once `flatpak remote-info --log`
        # returns. `_commit_buttons` maps a full commit hash to its
        # per-row Downgrade button so the click handler can disable it
        # while the worker thread runs `flatpak update --commit=…`.
        self._commits_group: Optional[Adw.PreferencesGroup] = None
        self._commits_placeholder: Optional[Adw.ActionRow] = None
        self._commit_buttons: dict = {}
        # `flatpak mask` banner: hidden until the worker confirms this
        # app is masked, and again after a successful Allow-updates click.
        self._mask_banner: Optional[Adw.Banner] = None
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

        if self.installed:
            self._mask_banner = Adw.Banner(title="Updates are blocked for this app")
            self._mask_banner.set_button_label("Allow updates")
            self._mask_banner.set_revealed(False)
            self._mask_banner.connect("button-clicked", self._on_unmask_clicked)
            body.append(self._mask_banner)

        # Update box sits directly under the hero so the "at a glance" diff
        # (current → new version + release notes since installed) is the
        # first thing the user reads on a detail page that has a pending
        # update. Skipped when there's no available update or the app is
        # being viewed from the catalog (Open in Software handles that flow).
        if self.installed and self.update_info:
            body.append(self._build_update_box(app, meta))

        if meta["screenshots"]:
            body.append(build_screenshots_row(
                app["id"], meta["screenshots"], self.parent_window,
            ))

        if self.installed:
            warning = self._build_remote_no_enumerate_warning(app)
            if warning is not None:
                body.append(warning)
            body.append(self._build_actions_row(app))

        description = meta["description_markup"]
        if description:
            body.append(self._build_description(description))

        body.append(self._build_about_group(app, meta))

        if self.installed and permissions:
            body.append(self._build_permissions_group(permissions))

        if self.installed:
            commits_group, placeholder = build_recent_releases_placeholder()
            self._commits_group = commits_group
            self._commits_placeholder = placeholder
            body.append(commits_group)
            threading.Thread(target=self._load_remote_log, daemon=True).start()

        if meta["releases"]:
            body.append(build_version_history_group(meta["releases"]))

        clamp.set_child(body)
        scrolled.set_child(clamp)
        toolbar_view.set_content(scrolled)

        self.set_child(toolbar_view)

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

    def _build_update_box(self, app: dict, meta: dict) -> Gtk.Widget:
        """Terracotta-tinted callout: current vs new version + release notes
        and a Mint "Update now" button.

        Lives at the top of the detail body (just under the hero) so the
        diff is the first thing the user sees on an updateable app. Release
        notes are inlined (not expanders) so the "what's new since
        installed" content is readable without an extra click. The button
        runs `flatpak update` for this single app via `update_runner`.
        """
        new_v = self.update_info.get("version") or "?"
        origin = self.update_info.get("origin") or "remote"
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

        # "What's new since v0.1.0": release notes slice from the metainfo
        # for everything that landed after the installed version. Empty
        # `releases_since` (no release tags, or installed == latest known)
        # falls through to the "release notes unavailable" branch below so
        # the card doesn't look truncated under the version-diff header.
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
                    body_label = Gtk.Label(label=rel["description_markup"])
                    body_label.set_wrap(True)
                    body_label.set_xalign(0.0)
                    body_label.add_css_class("body")
                    outer.append(body_label)
        else:
            if current == "?":
                msg = "Installed version unknown — open Software to view release notes."
            else:
                msg = "Release notes for the new version aren't available yet."
            note = Gtk.Label(label=msg)
            note.set_xalign(0.0)
            note.add_css_class("dim-label")
            note.set_wrap(True)
            note.set_margin_top(8)
            outer.append(note)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        button_row.set_halign(Gtk.Align.START)
        button_row.set_margin_top(12)

        scope = "user" if app.get("installation") == "user" else "system"
        update_btn = Gtk.Button(label="Update now")
        update_btn.add_css_class("pill")
        update_btn.add_css_class("flatpal-update-button")
        update_btn.set_tooltip_text(
            f"flatpak update --{scope} {app['id']}"
        )
        update_btn.connect("clicked", self._on_update_clicked)
        button_row.append(update_btn)
        outer.append(button_row)

        self._update_card = outer
        self._update_button = update_btn
        return outer

    def _on_update_clicked(self, _button: Gtk.Button) -> None:
        """Run `flatpak update` for this app from a worker thread."""
        # `not get_sensitive()` guards against a sub-frame double-click that
        # races past GTK's own sensitivity gate before the first handler can
        # disable the button.
        if self._update_button is None or not self._update_button.get_sensitive():
            return
        self._update_button.set_sensitive(False)
        self._update_button.set_label("Updating…")

        app_id = self.app["id"]
        scope = "user" if self.app.get("installation") == "user" else "system"

        def worker() -> None:
            ok, err = run_update(app_id, scope)
            GLib.idle_add(self._finish_update, ok, err)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_update(self, ok: bool, err: Optional[str]) -> bool:
        if ok:
            new_v = (self.update_info or {}).get("version") or ""
            # `self.app` is the same dict cached by `InstalledPage`; mutating
            # `version` here is what lets the row paint the new version after
            # `clear_update` calls `installed_page.refresh()` below.
            self.app["version"] = new_v or self.app.get("version", "")
            self._show_update_success()
            self.parent_window.clear_update(self.app["id"])
        else:
            if self._update_button is not None:
                self._update_button.set_sensitive(True)
                self._update_button.set_label("Update now")
            dialog = Adw.AlertDialog(
                heading="Update failed",
                body=(
                    f"Could not update {self.app['name']}: "
                    + (err or "unknown error")
                ),
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present(self.parent_window)
        return False

    def _show_update_success(self) -> None:
        """Replace the update card contents with a success state in place.

        The card stays in the body (no widget removal) so the user's
        scroll position is preserved; only the contents swap. The About-
        group "Version (update available)" row flips back to plain
        "Version" with the new version.
        """
        new_v = (self.update_info or {}).get("version") or "?"
        origin = (self.update_info or {}).get("origin") or "remote"

        card = self._update_card
        if card is not None:
            child = card.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                card.remove(child)
                child = nxt

            header = Gtk.Label(label="✓ Updated")
            header.set_xalign(0.0)
            header.add_css_class("title-3")
            card.append(header)

            diff = Gtk.Label(label=f"Now on {new_v}   ·   from {origin}")
            diff.set_xalign(0.0)
            diff.add_css_class("heading")
            card.append(diff)

            note = Gtk.Label(
                label="The new version is ready. Restart the app to pick it up."
            )
            note.set_xalign(0.0)
            note.set_wrap(True)
            note.add_css_class("dim-label")
            note.set_margin_top(8)
            card.append(note)

        row = self._update_version_row
        if row is not None:
            row.set_title("Version")
            row.set_subtitle(GLib.markup_escape_text(new_v) if new_v else "—")

        self._update_button = None

    def _load_remote_log(self) -> None:
        """Worker: fetch the remote OSTree log, deployed commit, mask state."""
        app_id = self.app["id"]
        scope = "user" if self.app.get("installation") == "user" else "system"
        remote = self.app.get("origin", "")
        text = fetch_log(app_id, scope, remote)
        records = parse_log(text) if text else []
        current = fetch_current_commit(app_id, scope) or ""
        masked = fetch_is_masked(app_id, scope)
        GLib.idle_add(self._apply_remote_log, records, current, masked)

    def _apply_remote_log(
        self, records: list, current_commit: str, masked: bool,
    ) -> bool:
        if self._commits_group is not None:
            self._commit_buttons = populate_recent_releases(
                self._commits_group,
                self._commits_placeholder,
                records,
                on_downgrade=self._on_downgrade_clicked,
                current_commit=current_commit,
            )
            self._commits_placeholder = None
        self._set_mask_banner(masked)
        return False

    def _set_mask_banner(self, masked: bool) -> None:
        if self._mask_banner is None:
            return
        self._mask_banner.set_title("Updates are blocked for this app")
        self._mask_banner.set_button_label("Allow updates")
        self._mask_banner.set_revealed(bool(masked))

    def _on_unmask_clicked(self, _banner: Adw.Banner) -> None:
        if self._mask_banner is None:
            return
        self._mask_banner.set_title("Allowing updates…")
        self._mask_banner.set_button_label("")

        app_id = self.app["id"]
        scope = "user" if self.app.get("installation") == "user" else "system"

        def worker() -> None:
            ok, err = run_unmask(app_id, scope)
            GLib.idle_add(self._finish_unmask, ok, err)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_unmask(self, ok: bool, err: Optional[str]) -> bool:
        if ok:
            self._set_mask_banner(False)
            return False

        self._set_mask_banner(True)
        dialog = Adw.AlertDialog(
            heading="Could not allow updates",
            body=(
                f"`flatpak mask --remove` failed for {self.app['name']}: "
                + (err or "unknown error")
            ),
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self.parent_window)
        return False

    def _on_downgrade_clicked(self, record: dict) -> None:
        commit = (record.get("commit") or "").strip()
        if not commit:
            return
        button = self._commit_buttons.get(commit)

        subject = record.get("subject") or "(no subject)"
        short = commit[:12]
        date = record.get("date_short") or ""
        dialog = Adw.AlertDialog(
            heading=f"Switch {self.app['name']} to commit {short}?",
            body=(
                f"Subject: {subject}\n"
                f"Date:    {date}\n"
                f"Commit:  {short}\n\n"
                "Downgrading can fail or lose data if the older version "
                "can't read the current app data. Make sure you have backups."
            ),
        )
        check = Gtk.CheckButton(label="Block future updates for this app")
        check.set_active(True)
        dialog.set_extra_child(check)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("downgrade", "Downgrade")
        dialog.set_response_appearance("downgrade", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            self._on_downgrade_response, record, check, button,
        )
        dialog.present(self.parent_window)

    def _on_downgrade_response(
        self,
        _dialog: Adw.AlertDialog,
        response_id: str,
        record: dict,
        check: Gtk.CheckButton,
        button: Optional[Gtk.Button],
    ) -> None:
        if response_id != "downgrade":
            return
        # Read the checkbox eagerly: Adw.AlertDialog disposes the extra
        # child once the response handler returns.
        mask_after = bool(check.get_active())
        commit = (record.get("commit") or "").strip()
        if not commit:
            return
        if button is not None:
            button.set_sensitive(False)
            button.set_label("Downgrading…")

        app_id = self.app["id"]
        scope = "user" if self.app.get("installation") == "user" else "system"

        def worker() -> None:
            ok, err, masked = run_downgrade(app_id, scope, commit, mask_after)
            GLib.idle_add(
                self._finish_downgrade,
                ok, err, masked, mask_after, record, button,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_downgrade(
        self,
        ok: bool,
        err: Optional[str],
        masked: bool,
        mask_requested: bool,
        record: dict,
        button: Optional[Gtk.Button],
    ) -> bool:
        commit = (record.get("commit") or "").strip()
        short = commit[:12] if commit else "?"
        subject = record.get("subject") or "(no subject)"

        if not ok:
            if button is not None:
                button.set_sensitive(True)
                button.set_label("Downgrade")
            dialog = Adw.AlertDialog(
                heading="Downgrade failed",
                body=(
                    f"Could not downgrade {self.app['name']}: "
                    + (err or "unknown error")
                ),
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present(self.parent_window)
            return False

        if button is not None:
            button.set_label("Downgraded")
        installed_page = getattr(self.parent_window, "installed_page", None)
        if installed_page is not None:
            installed_page.reload()
        if masked:
            self._set_mask_banner(True)

        if mask_requested and not masked:
            body = (
                f"Now on commit {short} ({subject}), but blocking future "
                f"updates failed: {err or 'unknown error'}. The next "
                "`flatpak update` may roll this app forward again."
            )
            heading = "Downgraded (updates not blocked)"
        elif masked:
            body = (
                f"Now on commit {short} ({subject}). Future updates are "
                f"blocked; to allow updates again, run: flatpak mask "
                f"--remove {self.app['id']}"
            )
            heading = "Downgraded"
        else:
            body = (
                f"Now on commit {short} ({subject}). The next "
                "`flatpak update` may roll this app forward again unless "
                "you mask it."
            )
            heading = "Downgraded"

        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self.parent_window)
        return False

    def _build_remote_no_enumerate_warning(self, app: dict) -> Optional[Gtk.Widget]:
        """Card explaining the bundle-install no-enumerate quirk, with a fix button.

        Returns None when the app's origin remote is fine. Otherwise builds a
        warning surface above the actions row. Clicking Fix runs
        `flatpak remote-modify --enumerate <remote>` and then refreshes
        the AppStream catalog so GNOME Software immediately picks up the app.
        """
        remote = app.get("origin", "")
        scope = "user" if app.get("installation") == "user" else "system"
        if not remote:
            return None
        opts = fetch_remote_options().get((remote, scope), set())
        if "no-enumerate" not in opts:
            return None

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.add_css_class("card")
        card.set_margin_top(0)

        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.set_pixel_size(28)
        icon.set_margin_start(16)
        icon.set_margin_top(14)
        icon.set_margin_bottom(14)
        icon.set_valign(Gtk.Align.START)
        card.append(icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text.set_hexpand(True)
        text.set_margin_top(12)
        text.set_margin_bottom(12)

        title = Gtk.Label(label="Hidden from GNOME Software")
        title.set_halign(Gtk.Align.START)
        title.add_css_class("heading")
        text.append(title)

        body_label = Gtk.Label(
            label=(
                f"The remote <tt>{GLib.markup_escape_text(remote)}</tt> "
                f"({scope}) was added with <tt>no-enumerate</tt>, which keeps "
                "GNOME Software from indexing this app. “Open in Software” "
                "may fail until the flag is cleared."
            )
        )
        body_label.set_use_markup(True)
        body_label.set_halign(Gtk.Align.START)
        body_label.set_wrap(True)
        body_label.set_xalign(0.0)
        body_label.add_css_class("dim-label")
        text.append(body_label)

        card.append(text)

        fix_btn = Gtk.Button(label="Fix")
        fix_btn.add_css_class("pill")
        fix_btn.set_valign(Gtk.Align.CENTER)
        fix_btn.set_margin_end(16)
        fix_btn.set_tooltip_text(
            f"flatpak remote-modify --{scope} --enumerate {remote}"
        )
        fix_btn.connect(
            "clicked",
            lambda *_: self._fix_remote_no_enumerate(fix_btn, remote, scope, card),
        )
        card.append(fix_btn)

        return card

    def _fix_remote_no_enumerate(
        self,
        button: Gtk.Button,
        remote: str,
        scope: str,
        card: Gtk.Widget,
    ) -> None:
        """Clear no-enumerate on `remote` from a worker thread; update UI on idle."""
        button.set_sensitive(False)
        button.set_label("Fixing…")

        def worker() -> None:
            ok, err = fix_remote_no_enumerate(remote, scope)

            def finish() -> bool:
                parent = card.get_parent()
                if parent is None:
                    return False
                if ok and isinstance(parent, Gtk.Box):
                    parent.remove(card)
                else:
                    button.set_label("Failed")
                    button.set_tooltip_text(err or "flatpak remote-modify failed")
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

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
            # canonical About location; useful even for users who scroll
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
            # Stashed for the post-success row flip back to the plain
            # "Version" form once `flatpak update` lands.
            self._update_version_row = ver_row
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

