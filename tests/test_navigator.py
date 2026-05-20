"""Tests for the wraparound image navigator."""

import unittest

from flatpal.navigator import ImageNavigator


class TestImageNavigator(unittest.TestCase):
    def test_empty_list(self):
        nav = ImageNavigator([])
        self.assertEqual(len(nav), 0)
        self.assertIsNone(nav.current())
        self.assertIsNone(nav.go_next())
        self.assertIsNone(nav.go_prev())
        self.assertFalse(nav.has_multiple)

    def test_single_item(self):
        nav = ImageNavigator(["a"])
        self.assertEqual(nav.current(), "a")
        self.assertEqual(nav.go_next(), "a")
        self.assertEqual(nav.go_prev(), "a")
        self.assertFalse(nav.has_multiple)

    def test_starting_index(self):
        nav = ImageNavigator(["a", "b", "c"], index=1)
        self.assertEqual(nav.current(), "b")
        self.assertTrue(nav.has_multiple)

    def test_starting_index_clamped_high(self):
        nav = ImageNavigator(["a", "b"], index=99)
        self.assertEqual(nav.current(), "b")

    def test_starting_index_clamped_low(self):
        nav = ImageNavigator(["a", "b"], index=-3)
        self.assertEqual(nav.current(), "a")

    def test_starting_index_on_empty(self):
        nav = ImageNavigator([], index=5)
        self.assertEqual(nav.index, 0)

    def test_forward_wraparound(self):
        nav = ImageNavigator(["a", "b", "c"], index=2)
        self.assertEqual(nav.go_next(), "a")
        self.assertEqual(nav.index, 0)

    def test_backward_wraparound(self):
        nav = ImageNavigator(["a", "b", "c"], index=0)
        self.assertEqual(nav.go_prev(), "c")
        self.assertEqual(nav.index, 2)

    def test_full_cycle_forward(self):
        nav = ImageNavigator(["a", "b", "c"])
        seen = [nav.current()]
        for _ in range(len(nav)):
            seen.append(nav.go_next())
        self.assertEqual(seen, ["a", "b", "c", "a"])

    def test_full_cycle_backward(self):
        nav = ImageNavigator(["a", "b", "c"])
        seen = [nav.current()]
        for _ in range(len(nav)):
            seen.append(nav.go_prev())
        self.assertEqual(seen, ["a", "c", "b", "a"])

    def test_input_list_is_copied(self):
        src = ["a", "b"]
        nav = ImageNavigator(src)
        src.append("c")  # mutating the source should not affect the navigator
        self.assertEqual(len(nav), 2)


if __name__ == "__main__":
    unittest.main()
