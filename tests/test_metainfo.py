"""Tests for flatpal.metainfo using real metainfo XML fixtures."""

import unittest
from pathlib import Path

from flatpal.metainfo import (
    parse_metainfo,
    _description_markup,
    _empty_result,
)
import xml.etree.ElementTree as ET


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParseMetainfoDiscord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = parse_metainfo(load_fixture("discord.metainfo.xml"))

    def test_id_and_name(self):
        self.assertEqual(self.data["id"], "com.discordapp.Discord")
        self.assertEqual(self.data["name"], "Discord")

    def test_summary(self):
        self.assertEqual(self.data["summary"], "Talk, play, hang out")

    def test_developer_new_form(self):
        # Discord uses the modern <developer id=..><name>...</name></developer> shape.
        self.assertEqual(self.data["developer_name"], "Discord Inc.")

    def test_license(self):
        self.assertEqual(self.data["project_license"], "LicenseRef-proprietary")

    def test_urls(self):
        self.assertEqual(self.data["urls"].get("homepage"), "https://discord.com")
        self.assertIn("bugtracker", self.data["urls"])

    def test_screenshots(self):
        shots = self.data["screenshots"]
        self.assertEqual(len(shots), 2)
        self.assertTrue(all(s["source_url"].startswith("https://") for s in shots))
        self.assertEqual(shots[0]["caption"], "Dark Mode Window")
        self.assertTrue(shots[0]["default"])
        self.assertFalse(shots[1]["default"])

    def test_categories(self):
        self.assertIn("InstantMessaging", self.data["categories"])
        self.assertIn("Network", self.data["categories"])

    def test_description_has_paragraphs_and_bullets(self):
        md = self.data["description_markup"]
        self.assertIn("Discord is a free", md)
        # The metainfo includes a <ul><li>...</li></ul>; we render bullets.
        self.assertIn("•", md)


class TestParseMetainfoGimpLocalisation(unittest.TestCase):
    """GIMP ships dozens of xml:lang variants. We must prefer the right one."""

    @classmethod
    def setUpClass(cls):
        cls.xml = load_fixture("gimp.metainfo.xml")

    def test_summary_english_when_no_lang(self):
        data = parse_metainfo(self.xml)
        self.assertEqual(data["summary"], "High-end image creation and manipulation")

    def test_summary_czech_when_cs(self):
        data = parse_metainfo(self.xml, lang="cs")
        self.assertEqual(
            data["summary"],
            "Vytváření a úprava obrázků na špičkové úrovni",
        )

    def test_summary_czech_when_cs_CZ(self):
        # Match by base language code: cs_CZ falls back to cs.
        data = parse_metainfo(self.xml, lang="cs_CZ")
        self.assertEqual(
            data["summary"],
            "Vytváření a úprava obrázků na špičkové úrovni",
        )

    def test_summary_falls_back_to_english_for_unknown(self):
        # Klingon: no variant; should land on untagged English baseline.
        data = parse_metainfo(self.xml, lang="tlh")
        self.assertEqual(data["summary"], "High-end image creation and manipulation")

    def test_developer_legacy_form(self):
        # GIMP uses <developer_name> (legacy AppStream).
        data = parse_metainfo(self.xml)
        self.assertEqual(data["developer_name"], "The GIMP team")

    def test_releases_capped_to_five(self):
        data = parse_metainfo(self.xml)
        self.assertLessEqual(len(data["releases"]), 5)
        self.assertGreater(len(data["releases"]), 0)
        # First release dict should have version + date strings.
        first = data["releases"][0]
        self.assertIn("version", first)
        self.assertIn("date", first)


class TestParseMetainfoEdgeCases(unittest.TestCase):
    def test_empty_string_returns_empty_dict(self):
        result = parse_metainfo("")
        self.assertEqual(result, _empty_result())

    def test_malformed_xml_returns_empty_dict(self):
        result = parse_metainfo("<not valid")
        self.assertEqual(result, _empty_result())

    def test_minimal_component_does_not_crash(self):
        result = parse_metainfo("<component><id>x</id></component>")
        self.assertEqual(result["id"], "x")
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["screenshots"], [])
        self.assertEqual(result["urls"], {})

    def test_screenshot_without_type_source(self):
        xml = """<component>
          <screenshots>
            <screenshot><image>https://e/a.png</image><caption>c</caption></screenshot>
          </screenshots>
        </component>"""
        result = parse_metainfo(xml)
        self.assertEqual(len(result["screenshots"]), 1)
        self.assertEqual(result["screenshots"][0]["source_url"], "https://e/a.png")

    def test_screenshot_prefers_source_image(self):
        xml = """<component>
          <screenshots>
            <screenshot>
              <image type="thumbnail">https://t/thumb.png</image>
              <image type="source">https://t/src.png</image>
            </screenshot>
          </screenshots>
        </component>"""
        result = parse_metainfo(xml)
        self.assertEqual(result["screenshots"][0]["source_url"], "https://t/src.png")


class TestDescriptionMarkup(unittest.TestCase):
    def test_paragraphs_separated_by_blank_line(self):
        desc = ET.fromstring("<description><p>One.</p><p>Two.</p></description>")
        self.assertEqual(_description_markup(desc, None), "One.\n\nTwo.")

    def test_ul_renders_bullets(self):
        desc = ET.fromstring(
            "<description><p>Intro</p><ul><li>A</li><li>B</li></ul></description>"
        )
        md = _description_markup(desc, None)
        self.assertIn("Intro", md)
        self.assertIn("• A", md)
        self.assertIn("• B", md)

    def test_empty_description_returns_empty(self):
        self.assertEqual(_description_markup(None, None), "")

    def test_localised_p_picked_when_matching_lang(self):
        desc = ET.fromstring(
            '<description>'
            '<p>English first</p>'
            '<p xml:lang="cs">Český první</p>'
            '<p>English second</p>'
            '<p xml:lang="cs">Český druhý</p>'
            '</description>'
        )
        md = _description_markup(desc, "cs")
        self.assertIn("Český první", md)
        self.assertIn("Český druhý", md)
        self.assertNotIn("English first", md)

    def test_multiline_p_collapses_to_single_line(self):
        # AppStream metainfo often wraps <p> across multiple indented lines;
        # without normalisation, Gtk.Label would render those continuations as
        # weirdly indented (closer to the side) inside a paragraph.
        desc = ET.fromstring(
            "<description><p>\n"
            "         GIMP is a freely distributed program for such tasks\n"
            "         as photo retouching, image composition and image\n"
            "         authoring.\n"
            "       </p></description>"
        )
        md = _description_markup(desc, None)
        self.assertEqual(
            md,
            "GIMP is a freely distributed program for such tasks "
            "as photo retouching, image composition and image authoring.",
        )
        # No newlines inside a single-paragraph rendering.
        self.assertNotIn("\n", md)

    def test_multiline_li_normalises_whitespace(self):
        desc = ET.fromstring(
            "<description><ul>"
            "<li>\n   first item that spans\n   two source lines\n  </li>"
            "<li>second item</li>"
            "</ul></description>"
        )
        md = _description_markup(desc, None)
        self.assertIn("• first item that spans two source lines", md)
        self.assertIn("• second item", md)


class TestParseMetainfoGIMPDescription(unittest.TestCase):
    """End-to-end check on the GIMP fixture — the description used to leak
    leading whitespace on continuation lines."""

    def test_gimp_paragraphs_have_no_indented_continuations(self):
        data = parse_metainfo(load_fixture("gimp.metainfo.xml"))
        md = data["description_markup"]
        # Every line either starts at column 0 (paragraph start, bullet, or
        # blank line) — there should be no lines beginning with whitespace
        # (which would have meant a continuation line is indented inward).
        for line in md.splitlines():
            self.assertFalse(
                line and line[0].isspace(),
                f"continuation line is indented: {line!r}",
            )


if __name__ == "__main__":
    unittest.main()
