"""Tests for the search helpers."""

import unittest

from flatpal.search import (
    filter_installed,
    matches,
    popular_shelf,
    search_catalog,
)


def installed_app(app_id, name, version="1.0", summary="", developer=None):
    """Shape of dict produced by core.fetch_apps + a `summary` overlay."""
    return {
        "id": app_id,
        "name": name,
        "version": version,
        "branch": "stable",
        "origin": "flathub",
        "installation": "system",
        "size_str": "10 MB",
        "size_bytes": 10_000_000,
        "installed": None,           # date
        "summary": summary,          # not in real fetch_apps but the search helper tolerates either
        "developer_name": developer, # ditto
    }


def catalog_entry(app_id, name="", summary="", developer=None):
    return {
        "id": app_id,
        "name": name or app_id,
        "summary": summary,
        "developer_name": developer,
        "description_markup": "",
        "categories": [],
        "urls": {},
        "screenshots": [],
        "releases": [],
        "project_license": None,
        "cached_icon": None,
    }


class TestMatches(unittest.TestCase):
    def test_empty_query_matches(self):
        self.assertTrue(matches(catalog_entry("x"), ""))

    def test_substring_in_name(self):
        e = catalog_entry("com.example.x", name="Discord")
        self.assertTrue(matches(e, "isc"))

    def test_case_insensitive_query(self):
        e = catalog_entry("com.example.x", name="Discord")
        self.assertTrue(matches(e, "DISCORD"))
        self.assertTrue(matches(e, "disCord"))

    def test_case_insensitive_data(self):
        e = catalog_entry("Com.Example.X", name="DISCORD")
        self.assertTrue(matches(e, "discord"))

    def test_matches_app_id(self):
        e = catalog_entry("com.example.SecretApp", name="Public Name")
        self.assertTrue(matches(e, "secret"))

    def test_matches_developer(self):
        e = catalog_entry("x", name="App", developer="Awesome Inc.")
        self.assertTrue(matches(e, "awesome"))

    def test_matches_summary(self):
        e = catalog_entry("x", name="App", summary="A cool video editor")
        self.assertTrue(matches(e, "video"))

    def test_no_match(self):
        e = catalog_entry("com.example.x", name="App", summary="Hello", developer="Dev")
        self.assertFalse(matches(e, "nonexistent"))

    def test_unicode_substring(self):
        e = catalog_entry("x", name="Žluťoučký kůň", summary="Czech apparatus")
        # Substring of the unicode name itself.
        self.assertTrue(matches(e, "kůň"))
        # Also case-insensitive (casefold handles ě/É etc.).
        self.assertTrue(matches(e, "ŽLU"))


class TestFilterInstalled(unittest.TestCase):
    APPS = [
        installed_app("com.discordapp.Discord", "Discord", summary="Chat", developer="Discord Inc."),
        installed_app("org.signal.Signal", "Signal Desktop", summary="Private messenger"),
        installed_app("org.mozilla.firefox", "Firefox", summary="Web browser",
                      developer="Mozilla"),
    ]

    def test_empty_query_returns_all(self):
        self.assertEqual(len(filter_installed(self.APPS, "")), 3)

    def test_whitespace_only_returns_all(self):
        self.assertEqual(len(filter_installed(self.APPS, "   ")), 3)

    def test_filter_by_name(self):
        result = filter_installed(self.APPS, "firefox")
        self.assertEqual([a["id"] for a in result], ["org.mozilla.firefox"])

    def test_filter_by_developer(self):
        result = filter_installed(self.APPS, "discord inc")
        self.assertEqual([a["id"] for a in result], ["com.discordapp.Discord"])

    def test_filter_by_summary(self):
        result = filter_installed(self.APPS, "browser")
        self.assertEqual([a["id"] for a in result], ["org.mozilla.firefox"])

    def test_filter_preserves_order(self):
        result = filter_installed(self.APPS, "i")  # matches all three
        self.assertEqual([a["id"] for a in result],
                         [a["id"] for a in self.APPS])

    def test_returns_new_list(self):
        result = filter_installed(self.APPS, "")
        result.clear()
        self.assertEqual(len(self.APPS), 3)


class TestSearchCatalog(unittest.TestCase):
    CATALOG = {
        "com.discordapp.Discord": catalog_entry(
            "com.discordapp.Discord", name="Discord", summary="Chat"),
        "org.videolan.VLC": catalog_entry(
            "org.videolan.VLC", name="VLC", summary="Multimedia player"),
        "org.kde.Kdenlive": catalog_entry(
            "org.kde.Kdenlive", name="Kdenlive", summary="Video editor"),
        "org.mozilla.firefox": catalog_entry(
            "org.mozilla.firefox", name="Firefox", summary="Web browser"),
    }

    def test_empty_query_returns_empty(self):
        self.assertEqual(search_catalog(self.CATALOG, set(), ""), [])
        self.assertEqual(search_catalog(self.CATALOG, set(), "   "), [])

    def test_filter_by_summary(self):
        result = search_catalog(self.CATALOG, set(), "video")
        ids = [r["id"] for r in result]
        self.assertEqual(set(ids), {"org.videolan.VLC", "org.kde.Kdenlive"})

    def test_sorted_by_name(self):
        result = search_catalog(self.CATALOG, set(), "video")
        # Kdenlive should come before VLC.
        self.assertEqual([r["name"] for r in result], ["Kdenlive", "VLC"])

    def test_installed_flag_marks_known_ids(self):
        installed = {"org.videolan.VLC"}
        result = search_catalog(self.CATALOG, installed, "video")
        flags = {r["id"]: r["installed"] for r in result}
        self.assertTrue(flags["org.videolan.VLC"])
        self.assertFalse(flags["org.kde.Kdenlive"])

    def test_limit_caps_results(self):
        result = search_catalog(self.CATALOG, set(), "e", limit=2)
        # 'e' is in Discord, Firefox, Kdenlive — 3 matches, cap at 2.
        self.assertEqual(len(result), 2)

    def test_limit_default_is_50(self):
        large = {
            f"org.x.App{n:04}": catalog_entry(f"org.x.App{n:04}", name=f"App{n:04}",
                                              summary="x")
            for n in range(120)
        }
        result = search_catalog(large, set(), "x")
        self.assertEqual(len(result), 50)

    def test_does_not_mutate_catalog(self):
        cat_before = {k: dict(v) for k, v in self.CATALOG.items()}
        search_catalog(self.CATALOG, set(), "video")
        # Catalog entries should still equal their pristine copies.
        for k, v in cat_before.items():
            self.assertEqual(self.CATALOG[k], v)
            self.assertNotIn("installed", self.CATALOG[k])

    def test_sort_by_popularity_ranks_first(self):
        # Discord rank=2, Firefox rank=1, Kdenlive unranked, VLC rank=3
        pop_idx = {
            "org.mozilla.firefox": {"rank": 1, "installs_last_month": 192_000},
            "com.discordapp.Discord": {"rank": 2, "installs_last_month": 212_000},
            "org.videolan.VLC": {"rank": 3, "installs_last_month": 109_000},
        }
        result = search_catalog(
            self.CATALOG, set(), "o",
            sort_by="popularity", popularity_idx=pop_idx,
        )
        # 'o' matches Discord, VLC, Firefox, Kdenlive (in some letters).
        ids = [r["id"] for r in result]
        # Ranked apps come before unranked, in rank order.
        ranked_prefix = [i for i in ids if i in pop_idx]
        self.assertEqual(ranked_prefix,
                         ["org.mozilla.firefox", "com.discordapp.Discord", "org.videolan.VLC"])
        # Unranked Kdenlive must come after the ranked ones.
        self.assertEqual(ids[-1], "org.kde.Kdenlive")

    def test_sort_by_popularity_falls_back_to_name_without_index(self):
        # Even with sort_by="popularity", if no index is given we use name.
        result = search_catalog(self.CATALOG, set(), "video", sort_by="popularity")
        self.assertEqual([r["name"] for r in result], ["Kdenlive", "VLC"])

    def test_popularity_field_attached_when_index_provided(self):
        pop_idx = {"com.discordapp.Discord": {"rank": 1, "installs_last_month": 212_000}}
        result = search_catalog(self.CATALOG, set(), "Discord", popularity_idx=pop_idx)
        self.assertEqual(result[0]["popularity"]["rank"], 1)
        self.assertEqual(result[0]["popularity"]["installs_last_month"], 212_000)


class TestPopularShelf(unittest.TestCase):
    CATALOG = {
        "com.discordapp.Discord": catalog_entry(
            "com.discordapp.Discord", name="Discord", summary="Chat"),
        "org.videolan.VLC": catalog_entry(
            "org.videolan.VLC", name="VLC", summary="Multimedia player"),
        "org.mozilla.firefox": catalog_entry(
            "org.mozilla.firefox", name="Firefox", summary="Web browser"),
    }

    HITS = [
        {"app_id": "com.discordapp.Discord", "installs_last_month": 212_000,
         "favorites_count": 500, "trending": 17.0},
        {"app_id": "org.mozilla.firefox", "installs_last_month": 192_000,
         "favorites_count": 300, "trending": 12.0},
        # Unknown to local catalog → skipped.
        {"app_id": "com.example.Missing", "installs_last_month": 999_000},
        {"app_id": "org.videolan.VLC", "installs_last_month": 109_000,
         "favorites_count": 200, "trending": 6.0},
    ]

    def test_keeps_order_from_hits(self):
        rows = popular_shelf(self.HITS, self.CATALOG, installed_ids=set(), limit=20)
        self.assertEqual([r["id"] for r in rows],
                         ["com.discordapp.Discord", "org.mozilla.firefox", "org.videolan.VLC"])

    def test_skips_missing_from_catalog(self):
        rows = popular_shelf(self.HITS, self.CATALOG, installed_ids=set())
        self.assertNotIn("com.example.Missing", [r["id"] for r in rows])

    def test_attaches_popularity_with_rank(self):
        rows = popular_shelf(self.HITS, self.CATALOG, installed_ids=set())
        self.assertEqual(rows[0]["popularity"]["rank"], 1)
        # VLC was hits[3] (one missing) — but its rank in the shelf reflects
        # ORIGINAL hits position, not filtered position.
        vlc = next(r for r in rows if r["id"] == "org.videolan.VLC")
        self.assertEqual(vlc["popularity"]["rank"], 4)

    def test_tags_installed(self):
        rows = popular_shelf(
            self.HITS, self.CATALOG, installed_ids={"org.videolan.VLC"}
        )
        flags = {r["id"]: r["installed"] for r in rows}
        self.assertTrue(flags["org.videolan.VLC"])
        self.assertFalse(flags["com.discordapp.Discord"])

    def test_respects_limit(self):
        rows = popular_shelf(self.HITS, self.CATALOG, installed_ids=set(), limit=2)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
