"""Tests for the Flathub catalog parser."""

import gzip
import tempfile
import unittest
from pathlib import Path

from flatpal import catalog


SAMPLE_CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<components version="0.14" origin="flathub">

  <component type="desktop-application">
    <id>com.example.Alpha</id>
    <name>Alpha</name>
    <name xml:lang="cs">Alfa</name>
    <summary>Make alphas happen</summary>
    <summary xml:lang="cs">Vytvořte alfy</summary>
    <developer><name>Alpha Team</name></developer>
    <project_license>MIT</project_license>
    <url type="homepage">https://alpha.example</url>
    <description>
      <p>Alpha does cool things.</p>
    </description>
    <screenshots>
      <screenshot type="default">
        <image type="source">https://alpha.example/shot.png</image>
        <caption>Main window</caption>
      </screenshot>
    </screenshots>
    <icon type="cached" width="128" height="128">com.example.Alpha.png</icon>
  </component>

  <component type="desktop-application">
    <id>com.example.Beta</id>
    <name>Beta</name>
    <summary>The other one</summary>
    <developer_name>Beta Author</developer_name>
    <project_license>GPL-3.0</project_license>
    <url type="homepage">https://beta.example</url>
    <url type="bugtracker">https://beta.example/bugs</url>
  </component>

  <!-- Edge case: no <id>, should be skipped. -->
  <component type="desktop-application">
    <name>Anonymous</name>
    <summary>No id, ignored</summary>
  </component>

</components>
"""


class TestParseCatalog(unittest.TestCase):
    def setUp(self):
        catalog.clear_cache()

    def test_basic_parse_returns_dict_by_app_id(self):
        cat = catalog.parse_catalog(SAMPLE_CATALOG)
        self.assertEqual(set(cat.keys()), {"com.example.Alpha", "com.example.Beta"})

    def test_entry_shape_matches_metainfo(self):
        cat = catalog.parse_catalog(SAMPLE_CATALOG)
        alpha = cat["com.example.Alpha"]
        for key in (
            "id", "name", "summary", "description_markup",
            "developer_name", "project_license", "categories",
            "urls", "screenshots", "releases", "cached_icon",
        ):
            self.assertIn(key, alpha)

    def test_localisation_picked(self):
        cat_cz = catalog.parse_catalog(SAMPLE_CATALOG, lang="cs")
        self.assertEqual(cat_cz["com.example.Alpha"]["name"], "Alfa")
        self.assertEqual(cat_cz["com.example.Alpha"]["summary"], "Vytvořte alfy")

    def test_localisation_falls_back_to_english(self):
        cat = catalog.parse_catalog(SAMPLE_CATALOG)
        self.assertEqual(cat["com.example.Alpha"]["name"], "Alpha")
        self.assertEqual(cat["com.example.Beta"]["summary"], "The other one")

    def test_developer_legacy_form(self):
        cat = catalog.parse_catalog(SAMPLE_CATALOG)
        self.assertEqual(cat["com.example.Beta"]["developer_name"], "Beta Author")

    def test_developer_new_form(self):
        cat = catalog.parse_catalog(SAMPLE_CATALOG)
        self.assertEqual(cat["com.example.Alpha"]["developer_name"], "Alpha Team")

    def test_screenshots_parsed(self):
        cat = catalog.parse_catalog(SAMPLE_CATALOG)
        shots = cat["com.example.Alpha"]["screenshots"]
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["source_url"], "https://alpha.example/shot.png")

    def test_urls_parsed(self):
        cat = catalog.parse_catalog(SAMPLE_CATALOG)
        self.assertEqual(cat["com.example.Beta"]["urls"]["homepage"], "https://beta.example")
        self.assertEqual(cat["com.example.Beta"]["urls"]["bugtracker"], "https://beta.example/bugs")

    def test_id_less_component_skipped(self):
        cat = catalog.parse_catalog(SAMPLE_CATALOG)
        self.assertEqual(len(cat), 2)
        for entry in cat.values():
            self.assertTrue(entry["id"])

    def test_empty_string(self):
        self.assertEqual(catalog.parse_catalog(""), {})

    def test_malformed_xml(self):
        self.assertEqual(catalog.parse_catalog("<bad"), {})

    def test_cached_icon_resolves_with_custom_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "128x128").mkdir()
            png = root / "128x128" / "com.example.Alpha.png"
            png.write_bytes(b"FAKE")
            cat = catalog.parse_catalog(SAMPLE_CATALOG, icon_root=root)
            self.assertEqual(cat["com.example.Alpha"]["cached_icon"], png)
            self.assertIsNone(cat["com.example.Beta"]["cached_icon"])

    def test_cached_icon_prefers_128(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "128x128").mkdir()
            (root / "64x64").mkdir()
            big = root / "128x128" / "com.example.Alpha.png"
            small = root / "64x64" / "com.example.Alpha.png"
            big.write_bytes(b"BIG")
            small.write_bytes(b"sm")
            self.assertEqual(
                catalog.catalog_icon_path("com.example.Alpha", icon_root=root),
                big,
            )

    def test_cached_icon_falls_back_to_64(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "64x64").mkdir()
            small = root / "64x64" / "com.example.Alpha.png"
            small.write_bytes(b"sm")
            self.assertEqual(
                catalog.catalog_icon_path("com.example.Alpha", icon_root=root),
                small,
            )


class TestLoadCatalog(unittest.TestCase):
    def setUp(self):
        catalog.clear_cache()

    def test_loads_gzipped_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz_path = Path(tmp) / "appstream.xml.gz"
            with gzip.open(gz_path, "wt", encoding="utf-8") as f:
                f.write(SAMPLE_CATALOG)
            cat = catalog.load_catalog(path=gz_path)
        self.assertIn("com.example.Alpha", cat)

    def test_missing_file_returns_empty_dict(self):
        cat = catalog.load_catalog(path=Path("/nonexistent/nowhere.xml.gz"))
        self.assertEqual(cat, {})

    def test_memoised(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz_path = Path(tmp) / "appstream.xml.gz"
            with gzip.open(gz_path, "wt", encoding="utf-8") as f:
                f.write(SAMPLE_CATALOG)
            first = catalog.load_catalog(path=gz_path)
            # Mutate the underlying file; should not be re-read.
            with gzip.open(gz_path, "wt", encoding="utf-8") as f:
                f.write("<components/>")
            second = catalog.load_catalog(path=gz_path)
            self.assertIs(first, second)

    def test_force_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz_path = Path(tmp) / "appstream.xml.gz"
            with gzip.open(gz_path, "wt", encoding="utf-8") as f:
                f.write(SAMPLE_CATALOG)
            catalog.load_catalog(path=gz_path)
            with gzip.open(gz_path, "wt", encoding="utf-8") as f:
                f.write("<components/>")
            refreshed = catalog.load_catalog(path=gz_path, force=True)
            self.assertEqual(refreshed, {})

    def test_auto_path_falls_back_to_user_install(self):
        # Simulate a machine where Flathub is `--user`-only: system path
        # missing, user path real.
        import unittest.mock as _mock
        with tempfile.TemporaryDirectory() as tmp:
            user_path = Path(tmp) / "user" / "appstream.xml.gz"
            user_path.parent.mkdir(parents=True)
            with gzip.open(user_path, "wt", encoding="utf-8") as f:
                f.write(SAMPLE_CATALOG)

            with _mock.patch.object(
                catalog, "_catalog_candidates",
                return_value=[Path(tmp) / "missing-system.xml.gz", user_path],
            ):
                cat = catalog.load_catalog()
            self.assertIn("com.example.Alpha", cat)

    def test_auto_path_returns_empty_when_no_candidate_exists(self):
        import unittest.mock as _mock
        with _mock.patch.object(
            catalog, "_catalog_candidates",
            return_value=[Path("/nonexistent/a"), Path("/nonexistent/b")],
        ):
            self.assertEqual(catalog.load_catalog(), {})


class TestIconRoots(unittest.TestCase):
    def test_lookup_walks_multiple_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root1 = Path(tmp) / "r1"
            root2 = Path(tmp) / "r2"
            (root2 / "128x128").mkdir(parents=True)
            target = root2 / "128x128" / "org.example.App.png"
            target.write_bytes(b"PNG")
            found = catalog.catalog_icon_path(
                "org.example.App", icon_roots=[root1, root2],
            )
            self.assertEqual(found, target)

    def test_first_root_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root1 = Path(tmp) / "r1"
            root2 = Path(tmp) / "r2"
            (root1 / "128x128").mkdir(parents=True)
            (root2 / "128x128").mkdir(parents=True)
            first = root1 / "128x128" / "org.example.App.png"
            second = root2 / "128x128" / "org.example.App.png"
            first.write_bytes(b"FIRST")
            second.write_bytes(b"SECOND")
            found = catalog.catalog_icon_path(
                "org.example.App", icon_roots=[root1, root2],
            )
            self.assertEqual(found, first)


if __name__ == "__main__":
    unittest.main()
