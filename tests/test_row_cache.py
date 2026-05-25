"""Tests for the running-tab row differ (no GTK required)."""

import unittest

from flatpal.row_cache import RowCache


class _FakeContainer:
    """Stand-in for Gtk.ListBox: ordered list with append/remove."""

    def __init__(self):
        self.children: list = []

    def append(self, widget):
        self.children.append(widget)

    def remove(self, widget):
        self.children.remove(widget)


class _FakeRow:
    """Stand-in for RunningRow/RunningExpanderRow.

    Records every update() call so we can tell hot-path mutation from
    cold-path re-creation.
    """

    def __init__(self, row: dict):
        self.app_id = row["id"]
        self.kind = row.get("kind", "single")  # "single" or "expander"
        self.update_calls: list[dict] = [dict(row)]

    def update(self, row: dict) -> None:
        self.update_calls.append(dict(row))


class _FakeSingle(_FakeRow):
    pass


class _FakeExpander(_FakeRow):
    pass


def _expected_type(row: dict):
    return _FakeExpander if row.get("kind") == "expander" else _FakeSingle


def _make_widget(row: dict):
    if row.get("kind") == "expander":
        return _FakeExpander(row)
    return _FakeSingle(row)


def _cache():
    container = _FakeContainer()
    cache = RowCache(
        container=container,
        make_widget=_make_widget,
        expected_type=_expected_type,
    )
    return container, cache


class TestRowCacheRender(unittest.TestCase):
    def test_first_render_appends_all_rows(self):
        container, cache = _cache()
        cache.render([{"id": "a"}, {"id": "b"}])
        self.assertEqual([w.app_id for w in container.children], ["a", "b"])
        self.assertEqual(cache.rendered_order, ["a", "b"])

    def test_hot_path_mutates_in_place(self):
        container, cache = _cache()
        cache.render([{"id": "a", "v": 1}, {"id": "b", "v": 1}])
        widgets_after_first = list(container.children)

        cache.render([{"id": "a", "v": 2}, {"id": "b", "v": 2}])

        # Same widget instances (not recreated) because order matched.
        self.assertIs(container.children[0], widgets_after_first[0])
        self.assertIs(container.children[1], widgets_after_first[1])
        # Each got one extra update() call for the new payload.
        self.assertEqual(container.children[0].update_calls[-1], {"id": "a", "v": 2})
        self.assertEqual(container.children[1].update_calls[-1], {"id": "b", "v": 2})

    def test_vanished_row_is_evicted(self):
        container, cache = _cache()
        cache.render([{"id": "a"}, {"id": "b"}])
        cache.render([{"id": "a"}])
        self.assertEqual([w.app_id for w in container.children], ["a"])
        self.assertIsNone(cache.get("b"))

    def test_reorder_preserves_widget_instances(self):
        container, cache = _cache()
        cache.render([{"id": "a"}, {"id": "b"}])
        a_widget = cache.get("a")
        b_widget = cache.get("b")

        cache.render([{"id": "b"}, {"id": "a"}])

        # Same instances, different order — cold path re-appended them.
        self.assertEqual([w.app_id for w in container.children], ["b", "a"])
        self.assertIs(cache.get("a"), a_widget)
        self.assertIs(cache.get("b"), b_widget)

    def test_type_flip_evicts_and_recreates(self):
        container, cache = _cache()
        cache.render([{"id": "a", "kind": "single"}])
        first = cache.get("a")
        self.assertIsInstance(first, _FakeSingle)

        cache.render([{"id": "a", "kind": "expander"}])

        second = cache.get("a")
        self.assertIsNot(second, first)
        self.assertIsInstance(second, _FakeExpander)
        self.assertEqual([w.app_id for w in container.children], ["a"])

    def test_clear_drops_widgets_and_state(self):
        container, cache = _cache()
        cache.render([{"id": "a"}, {"id": "b"}])
        cache.clear()
        self.assertEqual(container.children, [])
        self.assertEqual(cache.rendered_order, [])
        self.assertIsNone(cache.get("a"))

    def test_reset_order_forces_cold_path(self):
        container, cache = _cache()
        cache.render([{"id": "a"}, {"id": "b"}])
        first_a = cache.get("a")

        cache.reset_order()
        cache.render([{"id": "a"}, {"id": "b"}])

        # Widgets are reused (cache wasn't cleared) but they were detached
        # and re-appended via the cold path — proven by the fact reset_order
        # alone wiped rendered_order, so hot-path comparison fails.
        self.assertIs(cache.get("a"), first_a)
        self.assertEqual([w.app_id for w in container.children], ["a", "b"])

    def test_on_new_widget_fires_once_per_creation(self):
        container = _FakeContainer()
        wired: list = []
        cache = RowCache(
            container=container,
            make_widget=_make_widget,
            expected_type=_expected_type,
            on_new_widget=wired.append,
        )

        cache.render([{"id": "a"}])
        self.assertEqual(len(wired), 1)

        # Re-rendering the same row should NOT re-wire (widget is cached).
        cache.render([{"id": "a"}])
        self.assertEqual(len(wired), 1)

        # A new row triggers another wire.
        cache.render([{"id": "a"}, {"id": "b"}])
        self.assertEqual(len(wired), 2)


if __name__ == "__main__":
    unittest.main()
