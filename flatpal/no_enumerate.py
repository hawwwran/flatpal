"""Bundle-install no-enumerate detection + fix flow.

Detects installed apps whose origin remote was added with no-enumerate
(commonly via direct .flatpakref / .flatpak bundle install), which keeps
GNOME Software from indexing them. Surfaces a startup dialog with a
one-click fix that clears the flag on every affected remote.

Lifecycle is one-shot: instantiate at window startup, call ``start()``.
The dismissed flag in user settings is *auto-reset* when the issue clears
so a future bundle install that re-introduces a no-enumerate remote
triggers the dialog again. Without this auto-reset, dismissing once would
permanently silence the warning even for unrelated future occurrences.
"""

from __future__ import annotations

import threading
from typing import Callable, Set, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .core import fetch_apps, fetch_remote_options, fix_remote_no_enumerate


_Affected = Set[Tuple[str, str]]  # {(remote, scope), ...}


class NoEnumerateCheck:
    """Background scan + dialog flow for installed no-enumerate remotes."""

    def __init__(
        self,
        parent_window: Gtk.Window,
        *,
        was_dismissed: bool,
        on_dismissed_change: Callable[[bool], None],
    ):
        self._window = parent_window
        self._was_dismissed = was_dismissed
        self._on_dismissed_change = on_dismissed_change

    def start(self) -> None:
        """Kick off the background scan; surfaces a dialog if needed."""
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        apps = fetch_apps()
        opts_by_remote = fetch_remote_options()
        affected: _Affected = set()
        for app in apps:
            remote = app.get("origin", "")
            if not remote:
                continue
            scope = "user" if app.get("installation") == "user" else "system"
            if "no-enumerate" in opts_by_remote.get((remote, scope), set()):
                affected.add((remote, scope))

        if not affected:
            if self._was_dismissed:
                def reset() -> bool:
                    self._on_dismissed_change(False)
                    return False
                GLib.idle_add(reset)
            return

        if self._was_dismissed:
            # Issue still present but user chose silence; respect it.
            return

        def show() -> bool:
            self._present_dialog(affected)
            return False
        GLib.idle_add(show)

    def _present_dialog(self, affected: _Affected) -> None:
        remotes_summary = ", ".join(sorted(r for r, _ in affected))
        plural = "remote is" if len(affected) == 1 else "remotes are"

        dialog = Adw.AlertDialog(
            heading="Apps hidden from GNOME Software",
            body=(
                f"{len(affected)} {plural} configured with no-enumerate, which "
                "keeps GNOME Software from indexing the apps installed from "
                "them. \"Open in Software\" and the catalog search both miss "
                "these apps until the flag is cleared.\n\n"
                f"Affected: {remotes_summary}\n\n"
                "Fix clears the flag and refreshes the AppStream catalog. "
                "Close warning suppresses this dialog on future launches; "
                "the per-app warning in the detail view stays."
            ),
        )
        dialog.add_response("close", "Close warning")
        dialog.add_response("fix", "Fix")
        dialog.set_response_appearance("fix", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("fix")
        dialog.set_close_response("close")
        dialog.connect("response", self._on_response, affected)
        dialog.present(self._window)

    def _on_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        affected: _Affected,
    ) -> None:
        if response == "fix":
            self._fix_remotes(affected)
        else:
            # "close" (button) or default close-response (Esc); both mean
            # "the user actively chose not to fix", so persist the suppression.
            self._on_dismissed_change(True)

    def _fix_remotes(self, remotes: _Affected) -> None:
        """Clear no-enumerate on every affected remote (one polkit prompt
        per --system invocation; --user invocations skip it).

        Per-remote failures are collected and surfaced via a follow-up
        dialog so the user knows the startup warning will reappear on
        the next launch.
        """
        def worker() -> None:
            failures: list = []
            for remote, scope in remotes:
                ok, err = fix_remote_no_enumerate(remote, scope)
                if not ok:
                    failures.append((remote, scope, err))

            if failures:
                def show_failure() -> bool:
                    self._present_fix_failure_dialog(failures)
                    return False
                GLib.idle_add(show_failure)

        threading.Thread(target=worker, daemon=True).start()

    def _present_fix_failure_dialog(self, failures: list) -> None:
        lines = [
            f"• {remote} ({scope}): {err}" for remote, scope, err in failures
        ]
        dialog = Adw.AlertDialog(
            heading="Couldn't clear no-enumerate on some remotes",
            body=(
                "The startup warning will reappear on the next launch.\n\n"
                + "\n".join(lines)
            ),
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self._window)
