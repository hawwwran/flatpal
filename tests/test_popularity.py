"""Tests for popularity fetching, caching and indexing."""

import json
import os
import time
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from flatpal import popularity


SAMPLE_HITS = [
    {"app_id": "com.discordapp.Discord", "installs_last_month": 211_771,
     "favorites_count": 534, "trending": 17.68},
    {"app_id": "org.mozilla.firefox", "installs_last_month": 191_786,
     "favorites_count": 300, "trending": 12.3},
    {"app_id": "org.videolan.VLC", "installs_last_month": 108_508,
     "favorites_count": 200, "trending": 6.5},
]


class TestFormatInstallCount(unittest.TestCase):
    def test_under_1000_is_raw(self):
        self.assertEqual(popularity.format_install_count(0), "")
        self.assertEqual(popularity.format_install_count(None), "")
        self.assertEqual(popularity.format_install_count(42), "42")
        self.assertEqual(popularity.format_install_count(999), "999")

    def test_thousands_under_10k_keeps_one_decimal(self):
        self.assertEqual(popularity.format_install_count(1234), "1.2k")
        self.assertEqual(popularity.format_install_count(9999), "10.0k")

    def test_thousands_rounded_above_10k(self):
        self.assertEqual(popularity.format_install_count(27_259), "27k")
        self.assertEqual(popularity.format_install_count(211_771), "212k")
        self.assertEqual(popularity.format_install_count(999_000), "999k")

    def test_millions(self):
        self.assertEqual(popularity.format_install_count(1_234_567), "1.2M")
        self.assertEqual(popularity.format_install_count(10_000_000), "10.0M")


class TestPopularityIndex(unittest.TestCase):
    def test_assigns_ranks(self):
        idx = popularity.popularity_index(SAMPLE_HITS)
        self.assertEqual(idx["com.discordapp.Discord"]["rank"], 1)
        self.assertEqual(idx["org.mozilla.firefox"]["rank"], 2)
        self.assertEqual(idx["org.videolan.VLC"]["rank"], 3)

    def test_carries_numeric_fields(self):
        idx = popularity.popularity_index(SAMPLE_HITS)
        d = idx["com.discordapp.Discord"]
        self.assertEqual(d["installs_last_month"], 211_771)
        self.assertEqual(d["favorites_count"], 534)
        self.assertAlmostEqual(d["trending"], 17.68)

    def test_skips_missing_app_id(self):
        hits = [{"installs_last_month": 1}, {"app_id": "x", "installs_last_month": 2}]
        idx = popularity.popularity_index(hits)
        self.assertEqual(list(idx.keys()), ["x"])

    def test_skips_duplicate_app_id(self):
        hits = [
            {"app_id": "x", "installs_last_month": 9},
            {"app_id": "x", "installs_last_month": 1},
        ]
        idx = popularity.popularity_index(hits)
        self.assertEqual(idx["x"]["installs_last_month"], 9)

    def test_coerces_none_to_zero(self):
        idx = popularity.popularity_index([
            {"app_id": "x", "installs_last_month": None}
        ])
        self.assertEqual(idx["x"]["installs_last_month"], 0)
        self.assertEqual(idx["x"]["favorites_count"], 0)
        self.assertEqual(idx["x"]["trending"], 0.0)


class TestLoadPopular(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = TemporaryDirectory()
        self.cache_path = Path(self.tmp_dir.name) / "popular.json"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_fresh_fetch_writes_cache(self):
        fake = mock.Mock(return_value=list(SAMPLE_HITS))
        hits = popularity.load_popular(
            cache_path=self.cache_path,
            fetcher=fake,
            now=lambda: 1000.0,
        )
        self.assertEqual(len(hits), 3)
        fake.assert_called_once()
        self.assertTrue(self.cache_path.exists())
        cached = json.loads(self.cache_path.read_text())
        self.assertEqual(cached["fetched_at"], 1000.0)
        self.assertEqual(len(cached["hits"]), 3)

    def test_fresh_cache_skips_fetch(self):
        # Pre-populate cache.
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({
            "fetched_at": 1000.0, "hits": SAMPLE_HITS,
        }))
        fake = mock.Mock(side_effect=AssertionError("must not fetch"))
        hits = popularity.load_popular(
            cache_path=self.cache_path,
            max_age_seconds=3600,
            fetcher=fake,
            now=lambda: 2000.0,  # 1000 seconds later — still fresh
        )
        self.assertEqual(len(hits), 3)

    def test_stale_cache_triggers_fetch(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({
            "fetched_at": 1000.0, "hits": [{"app_id": "old"}],
        }))
        fake = mock.Mock(return_value=list(SAMPLE_HITS))
        hits = popularity.load_popular(
            cache_path=self.cache_path,
            max_age_seconds=10,
            fetcher=fake,
            now=lambda: 2000.0,  # 1000s later — stale
        )
        fake.assert_called_once()
        self.assertEqual(hits[0]["app_id"], "com.discordapp.Discord")

    def test_fetch_failure_falls_back_to_stale_cache(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({
            "fetched_at": 1000.0, "hits": [{"app_id": "stale"}],
        }))
        fake = mock.Mock(side_effect=urllib.error.URLError("no network"))
        hits = popularity.load_popular(
            cache_path=self.cache_path,
            max_age_seconds=10,
            fetcher=fake,
            now=lambda: 2000.0,
        )
        self.assertEqual(hits[0]["app_id"], "stale")

    def test_fetch_failure_and_no_cache_returns_empty(self):
        fake = mock.Mock(side_effect=urllib.error.URLError("no network"))
        hits = popularity.load_popular(
            cache_path=self.cache_path,  # doesn't exist
            fetcher=fake,
            now=lambda: 100.0,
        )
        self.assertEqual(hits, [])

    def test_corrupt_cache_treated_as_missing(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text("not-json")
        fake = mock.Mock(return_value=list(SAMPLE_HITS))
        hits = popularity.load_popular(
            cache_path=self.cache_path,
            fetcher=fake,
            now=lambda: 100.0,
        )
        fake.assert_called_once()
        self.assertEqual(len(hits), 3)

    def test_cache_write_failure_does_not_raise(self):
        # Point at an unwritable path; load_popular swallows OSError.
        unwritable = Path("/nonexistent/dir/popular.json")
        fake = mock.Mock(return_value=list(SAMPLE_HITS))
        hits = popularity.load_popular(
            cache_path=unwritable,
            fetcher=fake,
            now=lambda: 100.0,
        )
        self.assertEqual(len(hits), 3)

    def test_partial_fetch_returned_but_not_cached(self):
        # Fetcher returns (hits, complete=False) — should pass hits through
        # to caller but NOT write the cache, so the next launch retries.
        fake = mock.Mock(return_value=(list(SAMPLE_HITS), False))
        hits = popularity.load_popular(
            cache_path=self.cache_path,
            fetcher=fake,
            now=lambda: 1000.0,
        )
        self.assertEqual(len(hits), 3)
        self.assertFalse(self.cache_path.exists())

    def test_complete_fetch_caches(self):
        fake = mock.Mock(return_value=(list(SAMPLE_HITS), True))
        hits = popularity.load_popular(
            cache_path=self.cache_path,
            fetcher=fake,
            now=lambda: 1000.0,
        )
        self.assertEqual(len(hits), 3)
        self.assertTrue(self.cache_path.exists())

    def test_legacy_list_return_treated_as_complete(self):
        # Test stubs (and the old API) return a bare list. load_popular should
        # still accept it and cache as if complete.
        fake = mock.Mock(return_value=list(SAMPLE_HITS))
        popularity.load_popular(
            cache_path=self.cache_path,
            fetcher=fake,
            now=lambda: 1000.0,
        )
        self.assertTrue(self.cache_path.exists())


class TestFetchPopular(unittest.TestCase):
    """Test the parallel-paged fetcher; _fetch_page is mocked."""

    def _fake_page(self, page, per_page, timeout=10.0):
        # Build a 250-item page where installs decreases with page number and position.
        base = (5 - page) * 1000  # page 1 → 4000+; page 4 → 1000+
        return [
            {"app_id": f"org.example.p{page}.app{i:03}",
             "installs_last_month": base - i}
            for i in range(per_page)
        ]

    def test_combines_all_pages_sorted_descending(self):
        with mock.patch.object(popularity, "_fetch_page", side_effect=self._fake_page):
            hits, complete = popularity.fetch_popular(per_page=250, pages=4)
        self.assertTrue(complete)
        self.assertEqual(len(hits), 1000)
        # First hit should be the highest install count from page 1.
        self.assertEqual(hits[0]["app_id"], "org.example.p1.app000")
        # Verify globally sorted by installs_last_month descending.
        installs = [h["installs_last_month"] for h in hits]
        self.assertEqual(installs, sorted(installs, reverse=True))

    def test_on_progress_invoked_per_page(self):
        progress_calls = []
        with mock.patch.object(popularity, "_fetch_page", side_effect=self._fake_page):
            popularity.fetch_popular(
                per_page=250, pages=4,
                on_progress=lambda done, total, hits: progress_calls.append(
                    (done, total, len(hits))
                ),
            )
        # Four pages → four callbacks, with `done` monotonically increasing.
        self.assertEqual(len(progress_calls), 4)
        dones = [c[0] for c in progress_calls]
        self.assertEqual(sorted(dones), [1, 2, 3, 4])
        totals = {c[1] for c in progress_calls}
        self.assertEqual(totals, {4})
        # Cumulative hit count must be 250, 500, 750, 1000 in some order.
        counts = sorted(c[2] for c in progress_calls)
        self.assertEqual(counts, [250, 500, 750, 1000])

    def test_partial_failure_returns_succeeded_pages(self):
        def patchy(page, per_page, timeout=10.0):
            if page == 2:
                raise urllib.error.URLError("simulated")
            return self._fake_page(page, per_page, timeout)

        with mock.patch.object(popularity, "_fetch_page", side_effect=patchy):
            hits, complete = popularity.fetch_popular(per_page=250, pages=4)
        # 3 successful pages → 750 hits, marked incomplete.
        self.assertEqual(len(hits), 750)
        self.assertFalse(complete)

    def test_total_failure_raises(self):
        with mock.patch.object(
            popularity, "_fetch_page",
            side_effect=urllib.error.URLError("everything dead"),
        ):
            with self.assertRaises(urllib.error.URLError):
                popularity.fetch_popular(per_page=250, pages=4)

    def test_returns_empty_when_page_lacks_hits(self):
        with mock.patch.object(
            popularity, "_fetch_page", return_value=[]
        ):
            hits, complete = popularity.fetch_popular(per_page=250, pages=4)
            self.assertEqual(hits, [])
            self.assertTrue(complete)  # all 4 pages succeeded, just with empty content

    def test_rejects_non_positive_pages(self):
        with self.assertRaises(ValueError):
            popularity.fetch_popular(per_page=250, pages=0)
        with self.assertRaises(ValueError):
            popularity.fetch_popular(per_page=250, pages=-1)

    def test_rejects_non_positive_per_page(self):
        with self.assertRaises(ValueError):
            popularity.fetch_popular(per_page=0, pages=4)
        with self.assertRaises(ValueError):
            popularity.fetch_popular(per_page=-10, pages=4)

    def test_fetch_page_builds_correct_url(self):
        captured = {}

        class FakeResp:
            def read(self):
                return json.dumps({"hits": [{"app_id": "x"}]}).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            return FakeResp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            popularity._fetch_page(page=3, per_page=250)
        self.assertIn("page=3", captured["url"])
        self.assertIn("per_page=250", captured["url"])


if __name__ == "__main__":
    unittest.main()
