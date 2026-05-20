"""Unit tests for flatpal.core — locale-independent, no GTK, no subprocess."""

import locale
import unittest
from datetime import datetime

from flatpal.core import (
    SORT_KEYS,
    format_date,
    parse_history_output,
    parse_history_time,
    parse_list_output,
    parse_size,
    sort_apps,
)


class TestParseSize(unittest.TestCase):
    def test_english_decimal(self):
        self.assertEqual(parse_size("32.6 MB"), 32_600_000)

    def test_czech_decimal_comma(self):
        # This is the locale we actually run under; was a real bug surface.
        self.assertEqual(parse_size("32,6 MB"), 32_600_000)

    def test_units_decimal(self):
        self.assertEqual(parse_size("1 KB"), 1_000)
        self.assertEqual(parse_size("1 GB"), 1_000_000_000)
        self.assertEqual(parse_size("2 TB"), 2_000_000_000_000)

    def test_units_binary(self):
        self.assertEqual(parse_size("1 KiB"), 1024)
        self.assertEqual(parse_size("1 MiB"), 1024 * 1024)

    def test_bare_bytes(self):
        self.assertEqual(parse_size("512 B"), 512)

    def test_case_insensitive_unit(self):
        self.assertEqual(parse_size("1 mb"), 1_000_000)

    def test_unknown_marker(self):
        self.assertEqual(parse_size("?"), 0)

    def test_empty(self):
        self.assertEqual(parse_size(""), 0)
        self.assertEqual(parse_size(None), 0)
        self.assertEqual(parse_size("   "), 0)

    def test_garbage(self):
        self.assertEqual(parse_size("nope"), 0)


class TestParseHistoryTime(unittest.TestCase):
    """Critical: must work under cs_CZ.UTF-8 LC_TIME — the original bug."""

    @classmethod
    def setUpClass(cls):
        # Force the locale that broke the original implementation, if available.
        try:
            cls._old = locale.getlocale(locale.LC_TIME)
            locale.setlocale(locale.LC_TIME, "cs_CZ.UTF-8")
            cls._locale_set = True
        except locale.Error:
            cls._locale_set = False

    @classmethod
    def tearDownClass(cls):
        if cls._locale_set:
            try:
                locale.setlocale(locale.LC_TIME, cls._old)
            except (locale.Error, TypeError):
                locale.setlocale(locale.LC_TIME, "C")

    def test_no_year_recent(self):
        now = datetime(2026, 5, 20, 12, 0, 0)
        got = parse_history_time("Apr 22 17:03:17", now=now)
        self.assertEqual(got, datetime(2026, 4, 22, 17, 3, 17))

    def test_no_year_future_wraps(self):
        # If the parsed date would be in the future under current year, roll back one year.
        now = datetime(2026, 5, 20, 12, 0, 0)
        got = parse_history_time("Dec 30 17:03:17", now=now)
        self.assertEqual(got, datetime(2025, 12, 30, 17, 3, 17))

    def test_with_explicit_year(self):
        got = parse_history_time("Apr 22 2024")
        self.assertEqual(got, datetime(2024, 4, 22, 0, 0, 0))

    def test_unknown_month_returns_none(self):
        self.assertIsNone(parse_history_time("Xxx 22 17:03:17"))

    def test_czech_month_returns_none(self):
        # We explicitly do NOT accept locale-specific month names; LC_ALL=C is the contract.
        self.assertIsNone(parse_history_time("dub 22 17:03:17"))

    def test_malformed_input_returns_none(self):
        self.assertIsNone(parse_history_time(""))
        self.assertIsNone(parse_history_time("garbage"))
        self.assertIsNone(parse_history_time("Apr 22"))
        self.assertIsNone(parse_history_time("Apr xx 17:03:17"))
        self.assertIsNone(parse_history_time("Apr 22 17:03"))

    def test_day_boundary(self):
        got = parse_history_time("Jan 1 00:00:00",
                                 now=datetime(2026, 5, 20))
        self.assertEqual(got, datetime(2026, 1, 1, 0, 0, 0))


class TestParseHistoryOutput(unittest.TestCase):
    def test_picks_earliest_install_per_app(self):
        sample = (
            "Apr 22 17:03:17\tdeploy install\tcom.discordapp.Discord\n"
            "May 15 09:12:00\tdeploy install\tcom.discordapp.Discord\n"
            "Apr 22 17:09:45\tpull\tcom.discordapp.Discord\n"  # ignored: not 'deploy install'
        )
        out = parse_history_output(sample)
        self.assertIn("com.discordapp.Discord", out)
        self.assertEqual(
            out["com.discordapp.Discord"].strftime("%Y-%m-%d %H:%M:%S"),
            f"{datetime.now().year}-04-22 17:03:17",
        )

    def test_skips_uninstalled_and_updates(self):
        sample = (
            "Apr 22 17:03:17\tpull\torg.example.App\n"
            "Apr 22 17:03:18\tuninstall\torg.example.App\n"
            "Apr 22 17:03:19\tdeploy update\torg.example.App\n"
        )
        # We only treat 'deploy install' as the install event.
        # 'deploy update' contains the substring 'install'? No — 'install' != 'update'.
        self.assertEqual(parse_history_output(sample), {})

    def test_empty_input(self):
        self.assertEqual(parse_history_output(""), {})

    def test_malformed_lines_ignored(self):
        sample = (
            "not enough columns\n"
            "Apr 22 17:03:17\tdeploy install\torg.example.App\n"
            "\n"
            "junk\tjunk\n"
        )
        out = parse_history_output(sample)
        self.assertEqual(set(out.keys()), {"org.example.App"})


class TestParseListOutput(unittest.TestCase):
    SAMPLE = (
        "org.signal.Signal\tSignal Desktop\t8.10.0\tstable\tflathub\tsystem\t214,3 MB\n"
        "com.discordapp.Discord\tDiscord\t1.0.139\tstable\tflathub\tsystem\t17,6 MB\n"
    )

    def test_basic_parsing(self):
        apps = parse_list_output(self.SAMPLE)
        self.assertEqual(len(apps), 2)
        signal = apps[0]
        self.assertEqual(signal["id"], "org.signal.Signal")
        self.assertEqual(signal["name"], "Signal Desktop")
        self.assertEqual(signal["version"], "8.10.0")
        self.assertEqual(signal["size_bytes"], 214_300_000)
        self.assertIsNone(signal["installed"])

    def test_applies_install_dates(self):
        dates = {"org.signal.Signal": datetime(2026, 4, 22, 17, 3, 17)}
        apps = parse_list_output(self.SAMPLE, install_dates=dates)
        self.assertEqual(apps[0]["installed"], datetime(2026, 4, 22, 17, 3, 17))
        self.assertIsNone(apps[1]["installed"])

    def test_falls_back_to_id_when_name_empty(self):
        apps = parse_list_output(
            "org.example\t\t1.0\tstable\tflathub\tsystem\t1 KB\n"
        )
        self.assertEqual(apps[0]["name"], "org.example")

    def test_skips_blank_lines(self):
        apps = parse_list_output("\n\n" + self.SAMPLE + "\n\n")
        self.assertEqual(len(apps), 2)

    def test_short_rows_get_padded(self):
        # If a future flatpak version drops a column, don't crash.
        apps = parse_list_output("org.example\tName\t1.0\n")
        self.assertEqual(apps[0]["id"], "org.example")
        self.assertEqual(apps[0]["size_str"], "")
        self.assertEqual(apps[0]["size_bytes"], 0)


class TestFormatDate(unittest.TestCase):
    def test_iso_format(self):
        self.assertEqual(format_date(datetime(2026, 4, 22, 17, 3, 17)), "2026-04-22")

    def test_none_dash(self):
        self.assertEqual(format_date(None), "—")


class TestSortApps(unittest.TestCase):
    APPS = [
        {"id": "b", "name": "Bravo",  "installed": datetime(2026, 5, 1),  "size_bytes": 300},
        {"id": "a", "name": "alpha",  "installed": datetime(2025, 1, 1),  "size_bytes": 100},
        {"id": "c", "name": "Charlie","installed": None,                  "size_bytes": 200},
    ]

    def test_sort_by_name_case_insensitive(self):
        names = [a["name"] for a in sort_apps(self.APPS, "name")]
        self.assertEqual(names, ["alpha", "Bravo", "Charlie"])

    def test_sort_by_name_reverse(self):
        names = [a["name"] for a in sort_apps(self.APPS, "name", reverse=True)]
        self.assertEqual(names, ["Charlie", "Bravo", "alpha"])

    def test_sort_by_date_none_sorts_first_ascending(self):
        names = [a["name"] for a in sort_apps(self.APPS, "date")]
        self.assertEqual(names[0], "Charlie")  # None → datetime.min → earliest
        self.assertEqual(names[1], "alpha")
        self.assertEqual(names[2], "Bravo")

    def test_sort_by_size(self):
        names = [a["name"] for a in sort_apps(self.APPS, "size")]
        self.assertEqual(names, ["alpha", "Charlie", "Bravo"])

    def test_unknown_key_falls_back_to_name(self):
        names = [a["name"] for a in sort_apps(self.APPS, "bogus")]
        self.assertEqual(names, ["alpha", "Bravo", "Charlie"])

    def test_sort_keys_constant(self):
        self.assertEqual(set(SORT_KEYS), {"name", "date", "size"})


if __name__ == "__main__":
    unittest.main()
