"""Test the metainfo enrichment that makes 'search by summary' actually work.

`core.fetch_apps` returns rows built from `flatpak list --columns=…`, which
doesn't include summary or developer_name. Without enrichment, the Installed
tab's search box silently fails to match those fields — a real user-visible
bug we just fixed. This test pins the behavior.
"""

import unittest

from flatpal.installed_page import enrich_with_metainfo


class TestEnrichWithMetainfo(unittest.TestCase):
    def test_populates_summary_and_developer(self):
        apps = [
            {"id": "org.signal.Signal", "name": "Signal"},
            {"id": "org.gimp.GIMP", "name": "GIMP"},
        ]

        def fake_loader(app_id, lang):
            return {
                "org.signal.Signal": {
                    "summary": "Private messenger",
                    "developer_name": "Signal Foundation",
                },
                "org.gimp.GIMP": {
                    "summary": "Image manipulation",
                    "developer_name": "The GIMP team",
                },
            }.get(app_id, {})

        result = enrich_with_metainfo(apps, lang="en", loader=fake_loader)

        self.assertEqual(result[0]["summary"], "Private messenger")
        self.assertEqual(result[0]["developer_name"], "Signal Foundation")
        self.assertEqual(result[1]["summary"], "Image manipulation")
        self.assertEqual(result[1]["developer_name"], "The GIMP team")

    def test_missing_metainfo_falls_back_to_empty_strings(self):
        apps = [{"id": "com.example.Mystery", "name": "Mystery"}]

        result = enrich_with_metainfo(
            apps, lang=None,
            loader=lambda app_id, lang: {},  # nothing on disk
        )
        # Keys must be PRESENT (not missing) so search.matches() can safely
        # look them up; values are empty strings rather than None.
        self.assertEqual(result[0]["summary"], "")
        self.assertEqual(result[0]["developer_name"], "")

    def test_none_values_become_empty_strings(self):
        # parse_metainfo returns developer_name=None when neither <developer>
        # nor <developer_name> is present — must not leak through.
        apps = [{"id": "org.example.App", "name": "App"}]
        result = enrich_with_metainfo(
            apps, lang=None,
            loader=lambda app_id, lang: {"summary": None, "developer_name": None},
        )
        self.assertEqual(result[0]["summary"], "")
        self.assertEqual(result[0]["developer_name"], "")

    def test_search_matches_summary_after_enrichment(self):
        from flatpal.search import filter_installed

        apps = [
            {"id": "org.signal.Signal", "name": "Signal"},
            {"id": "org.mozilla.firefox", "name": "Firefox"},
        ]
        enrich_with_metainfo(
            apps, lang=None,
            loader=lambda app_id, lang: {
                "org.signal.Signal": {
                    "summary": "Private messenger",
                    "developer_name": "Signal Foundation",
                },
                "org.mozilla.firefox": {
                    "summary": "Web browser",
                    "developer_name": "Mozilla",
                },
            }.get(app_id, {}),
        )

        # Filter by summary terms — these used to silently fail.
        self.assertEqual(
            [a["id"] for a in filter_installed(apps, "messenger")],
            ["org.signal.Signal"],
        )
        self.assertEqual(
            [a["id"] for a in filter_installed(apps, "browser")],
            ["org.mozilla.firefox"],
        )
        # Developer name search.
        self.assertEqual(
            [a["id"] for a in filter_installed(apps, "Mozilla")],
            ["org.mozilla.firefox"],
        )


if __name__ == "__main__":
    unittest.main()
