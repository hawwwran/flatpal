"""Tests for flatpal.cache — no network, urlopen is mocked."""

import io
import unittest
from pathlib import Path
from unittest import mock

from flatpal import cache


class TestScreenshotCachePath(unittest.TestCase):
    def test_path_is_deterministic(self):
        url = "https://example.com/foo.png"
        self.assertEqual(
            cache.screenshot_cache_path("org.x.App", url),
            cache.screenshot_cache_path("org.x.App", url),
        )

    def test_extension_preserved_for_known_image_types(self):
        png = cache.screenshot_cache_path("org.x", "https://e/a.png")
        jpg = cache.screenshot_cache_path("org.x", "https://e/a.jpg")
        webp = cache.screenshot_cache_path("org.x", "https://e/a.webp")
        self.assertEqual(png.suffix, ".png")
        self.assertEqual(jpg.suffix, ".jpg")
        self.assertEqual(webp.suffix, ".webp")

    def test_extension_falls_back_to_img(self):
        path = cache.screenshot_cache_path("org.x", "https://e/no-extension")
        self.assertEqual(path.suffix, ".img")

    def test_different_urls_different_paths(self):
        a = cache.screenshot_cache_path("org.x", "https://e/a.png")
        b = cache.screenshot_cache_path("org.x", "https://e/b.png")
        self.assertNotEqual(a, b)

    def test_path_lives_under_cache_dir(self):
        p = cache.screenshot_cache_path("org.x", "https://e/a.png")
        self.assertTrue(str(p).startswith(str(cache.CACHE_DIR)))
        self.assertIn("org.x", p.parts)

    def test_query_string_changes_path(self):
        a = cache.screenshot_cache_path("org.x", "https://e/img.png?v=1")
        b = cache.screenshot_cache_path("org.x", "https://e/img.png?v=2")
        self.assertNotEqual(a.name, b.name)

    def test_capital_extension_normalised(self):
        # Some servers serve .PNG / .JPEG. Should still pick a known extension.
        p = cache.screenshot_cache_path("org.x", "https://e/A.PNG")
        self.assertEqual(p.suffix, ".png")


class TestDownloadScreenshot(unittest.TestCase):
    def setUp(self):
        import shutil
        self.tmp = Path(self.id().replace(".", "_") + "_tmp")
        self.tmp_dir = Path("/tmp") / self.tmp
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)
        self.tmp_dir.mkdir(parents=True)
        self.dest = self.tmp_dir / "shot.png"

    def tearDown(self):
        import shutil
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)

    def test_success_writes_file(self):
        fake_response = mock.MagicMock()
        fake_response.read.return_value = b"PNGDATA"
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot("https://x/a.png", self.dest)
        self.assertTrue(ok)
        self.assertTrue(self.dest.is_file())
        self.assertEqual(self.dest.read_bytes(), b"PNGDATA")

    def test_network_error_returns_false(self):
        with mock.patch.object(
            cache.urllib.request, "urlopen",
            side_effect=cache.urllib.error.URLError("boom"),
        ):
            ok = cache.download_screenshot("https://x/a.png", self.dest)
        self.assertFalse(ok)
        self.assertFalse(self.dest.exists())

    def test_partial_file_cleaned_on_failure(self):
        # Even if urlopen raises after we tried to create temp, no .part should remain.
        with mock.patch.object(
            cache.urllib.request, "urlopen",
            side_effect=TimeoutError("slow"),
        ):
            ok = cache.download_screenshot("https://x/a.png", self.dest)
        self.assertFalse(ok)
        leftover = list(self.tmp_dir.glob("*.part"))
        self.assertEqual(leftover, [])

    def test_atomic_replace(self):
        # If urlopen reads bytes and replace runs, dest exists and matches.
        fake_response = mock.MagicMock()
        fake_response.read.return_value = b"BODY"
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot("https://x/img.png", self.dest)
        self.assertTrue(ok)
        # No leftover .part
        self.assertEqual(list(self.tmp_dir.glob("*.part")), [])

    def test_rejects_non_image_content_type(self):
        # 404 page returning text/html should be discarded, not cached.
        fake_response = mock.MagicMock()
        fake_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        fake_response.read.return_value = b"<html>not found</html>"
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot("https://x/404.png", self.dest)
        self.assertFalse(ok)
        self.assertFalse(self.dest.exists())

    def test_accepts_image_content_type(self):
        fake_response = mock.MagicMock()
        fake_response.headers = {"Content-Type": "image/png"}
        fake_response.read.return_value = b"\x89PNGreal"
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot("https://x/img.png", self.dest)
        self.assertTrue(ok)
        self.assertEqual(self.dest.read_bytes(), b"\x89PNGreal")

    def test_accepts_application_octet_stream(self):
        # Common for raw.githubusercontent.com etc. — Content-Type isn't always image/*.
        fake_response = mock.MagicMock()
        fake_response.headers = {"Content-Type": "application/octet-stream"}
        fake_response.read.return_value = b"data"
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot("https://x/img.png", self.dest)
        self.assertTrue(ok)

    def test_missing_content_type_is_permitted(self):
        # Some servers omit Content-Type. We don't punish them.
        fake_response = mock.MagicMock()
        fake_response.headers = {}
        fake_response.read.return_value = b"data"
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot("https://x/img.png", self.dest)
        self.assertTrue(ok)

    def test_rejects_oversized_body(self):
        fake_response = mock.MagicMock()
        fake_response.headers = {"Content-Type": "image/png"}
        # Return more than max_bytes (use a tiny cap to keep the test fast).
        fake_response.read.return_value = b"x" * 2048
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot(
                "https://x/big.png", self.dest, max_bytes=1024,
            )
        self.assertFalse(ok)
        self.assertFalse(self.dest.exists())

    def _nested_dest(self):
        """A path whose parent doesn't exist yet — to detect spurious mkdir."""
        return self.tmp_dir / "would-be-app-id" / "shot.png"

    def test_no_empty_dir_after_rejected_content_type(self):
        # The destination dir must NOT exist after a rejected download — the
        # mkdir is gated on a successful response.
        dest = self._nested_dest()
        fake_response = mock.MagicMock()
        fake_response.headers = {"Content-Type": "text/html"}
        fake_response.read.return_value = b"<html>nope</html>"
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot("https://x/404.png", dest)
        self.assertFalse(ok)
        self.assertFalse(dest.parent.exists(),
                         "destination dir should not have been created")

    def test_no_empty_dir_after_oversized(self):
        dest = self._nested_dest()
        fake_response = mock.MagicMock()
        fake_response.headers = {"Content-Type": "image/png"}
        fake_response.read.return_value = b"x" * 2048
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake_response):
            ok = cache.download_screenshot(
                "https://x/big.png", dest, max_bytes=1024,
            )
        self.assertFalse(ok)
        self.assertFalse(dest.parent.exists())

    def test_no_empty_dir_after_network_failure(self):
        dest = self._nested_dest()
        with mock.patch.object(
            cache.urllib.request, "urlopen",
            side_effect=cache.urllib.error.URLError("offline"),
        ):
            ok = cache.download_screenshot("https://x/img.png", dest)
        self.assertFalse(ok)
        self.assertFalse(dest.parent.exists())


class TestPruneCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/flatpal_prune_test")
        if self.tmp.exists():
            import shutil
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True)

    def tearDown(self):
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)

    def _seed(self, app_id, filename, size, mtime):
        d = self.tmp / app_id
        d.mkdir(exist_ok=True)
        p = d / filename
        p.write_bytes(b"x" * size)
        import os as _os
        _os.utime(p, (mtime, mtime))
        return p

    def test_under_budget_does_nothing(self):
        self._seed("org.a", "1.png", 1000, 100.0)
        removed = cache.prune_cache(max_total_bytes=10_000, root=self.tmp)
        self.assertEqual(removed, 0)
        self.assertTrue((self.tmp / "org.a" / "1.png").exists())

    def test_drops_oldest_first(self):
        # Three files totalling 3000 bytes; budget 1500 → keep newest.
        self._seed("org.a", "old.png", 1000, 100.0)
        self._seed("org.a", "mid.png", 1000, 200.0)
        self._seed("org.a", "new.png", 1000, 300.0)
        removed = cache.prune_cache(max_total_bytes=1500, root=self.tmp)
        self.assertEqual(removed, 2000)  # two files removed
        self.assertFalse((self.tmp / "org.a" / "old.png").exists())
        self.assertFalse((self.tmp / "org.a" / "mid.png").exists())
        self.assertTrue((self.tmp / "org.a" / "new.png").exists())

    def test_removes_empty_app_directories(self):
        self._seed("org.a", "f.png", 1000, 100.0)
        cache.prune_cache(max_total_bytes=0, root=self.tmp)
        self.assertFalse((self.tmp / "org.a").exists())

    def test_keeps_app_dirs_with_remaining_files(self):
        self._seed("org.a", "old.png", 1000, 100.0)
        self._seed("org.a", "new.png", 1000, 200.0)
        cache.prune_cache(max_total_bytes=1500, root=self.tmp)
        self.assertTrue((self.tmp / "org.a").exists())  # still has new.png

    def test_missing_root_is_safe(self):
        # rmtree the dir we just made.
        import shutil
        shutil.rmtree(self.tmp)
        self.assertEqual(cache.prune_cache(max_total_bytes=100, root=self.tmp), 0)

    def test_ignores_files_at_root_level(self):
        # Stray file directly under root — shouldn't trip iteration.
        (self.tmp / "stray.txt").write_text("ignore me")
        self._seed("org.a", "f.png", 100, 100.0)
        # Stray isn't in app-dir form so it gets skipped; total = 100 only.
        cache.prune_cache(max_total_bytes=10_000, root=self.tmp)
        self.assertTrue((self.tmp / "stray.txt").exists())


class TestGetCachedOrDownload(unittest.TestCase):
    def setUp(self):
        # Override cache dir for the test
        self.real_cache = cache.CACHE_DIR
        self.tmp = Path("/tmp/flatpal_test_cache_xyz")
        cache.CACHE_DIR = self.tmp

    def tearDown(self):
        cache.CACHE_DIR = self.real_cache
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)

    def test_returns_cached_path_without_download_when_present(self):
        path = cache.screenshot_cache_path("org.x", "https://e/a.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"already-here")

        with mock.patch.object(cache.urllib.request, "urlopen") as m:
            result = cache.get_cached_or_download("org.x", "https://e/a.png")

        m.assert_not_called()
        self.assertEqual(result, path)

    def test_downloads_when_absent(self):
        fake = mock.MagicMock()
        fake.read.return_value = b"NEW"
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        with mock.patch.object(cache.urllib.request, "urlopen", return_value=fake):
            result = cache.get_cached_or_download("org.x", "https://e/new.png")
        self.assertIsNotNone(result)
        self.assertEqual(result.read_bytes(), b"NEW")

    def test_returns_none_on_failure(self):
        with mock.patch.object(
            cache.urllib.request, "urlopen",
            side_effect=cache.urllib.error.URLError("nope"),
        ):
            result = cache.get_cached_or_download("org.x", "https://e/bad.png")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
