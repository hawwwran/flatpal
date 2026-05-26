"""Tests for the pure `_compose_rows` join in the Updates tab.

The page itself is GTK-bound and not unit-tested per the codebase
convention (same reason `installed_page` and `running_page` have no
tests). The runner is covered by `tests/test_update_runner.py`.
"""

import unittest

from flatpal.updates_page import _compose_rows


def _installed(app_id, name, version="1.0", installation="system",
               branch="stable"):
    return {
        "id": app_id, "name": name, "version": version,
        "installation": installation, "branch": branch,
    }


def _update(version="2.0", origin="flathub"):
    return {"version": version, "origin": origin}


class TestComposeRows(unittest.TestCase):
    def test_empty_updates_returns_empty(self):
        installed = [_installed("org.example.App", "App")]
        self.assertEqual(_compose_rows(installed, {}), [])

    def test_update_without_installed_app_is_dropped(self):
        # `flatpak remote-ls --updates` raced an `uninstall`: the entry
        # exists in updates but the installed-side row is gone.
        rows = _compose_rows([], {"org.ghost.App": _update()})
        self.assertEqual(rows, [])

    def test_installed_without_update_is_dropped(self):
        installed = [_installed("org.example.App", "App")]
        updates = {"org.other.App": _update()}
        self.assertEqual(_compose_rows(installed, updates), [])

    def test_alphabetical_sort_by_name_lowercase(self):
        installed = [
            _installed("org.b.App", "banana"),
            _installed("org.a.App", "Apple"),
            _installed("org.c.App", "Cherry"),
        ]
        updates = {
            "org.b.App": _update("2.0"),
            "org.a.App": _update("1.1"),
            "org.c.App": _update("3.0"),
        }
        names = [r["name"] for r in _compose_rows(installed, updates)]
        # Case-insensitive: "Apple" before "banana" before "Cherry".
        self.assertEqual(names, ["Apple", "banana", "Cherry"])

    def test_sort_tiebreaker_by_app_id(self):
        # Same lowercased name (a system+user install pair, or two
        # remotes carrying apps that happen to share a display name).
        # Tiebreak by app_id so the order is deterministic.
        installed = [
            _installed("org.b.App", "App"),
            _installed("org.a.App", "App"),
        ]
        updates = {
            "org.b.App": _update(),
            "org.a.App": _update(),
        }
        ids = [r["id"] for r in _compose_rows(installed, updates)]
        self.assertEqual(ids, ["org.a.App", "org.b.App"])

    def test_scope_derived_from_installation_field(self):
        installed = [
            _installed("org.u.App", "U", installation="user"),
            _installed("org.s.App", "S", installation="system"),
            _installed("org.x.App", "X", installation=""),
        ]
        updates = {
            "org.u.App": _update(),
            "org.s.App": _update(),
            "org.x.App": _update(),
        }
        scopes = {r["id"]: r["scope"] for r in _compose_rows(installed, updates)}
        self.assertEqual(scopes["org.u.App"], "user")
        self.assertEqual(scopes["org.s.App"], "system")
        # Anything that isn't literally "user" falls back to "system"
        # so callers don't have to branch on missing/empty.
        self.assertEqual(scopes["org.x.App"], "system")

    def test_row_carries_diff_fields(self):
        installed = [_installed("org.example.App", "App", version="1.4.0")]
        updates = {"org.example.App": _update(version="1.4.1", origin="flathub")}
        (row,) = _compose_rows(installed, updates)
        self.assertEqual(row["id"], "org.example.App")
        self.assertEqual(row["current"], "1.4.0")
        self.assertEqual(row["new"], "1.4.1")
        self.assertEqual(row["origin"], "flathub")
        self.assertEqual(row["branch"], "stable")
        # The full app dict is kept so the row-click handler can pass it
        # straight to `DetailPage.from_installed`.
        self.assertEqual(row["app"]["id"], "org.example.App")


if __name__ == "__main__":
    unittest.main()
