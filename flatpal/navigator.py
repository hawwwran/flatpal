"""Tiny wraparound navigator for the fullscreen image viewer.

Pure logic, no GTK — lives in its own module so it's easy to unit-test.
"""

from __future__ import annotations

from typing import Generic, List, Optional, TypeVar

T = TypeVar("T")


class ImageNavigator(Generic[T]):
    """Wraps a list of items + a current index. Wraparound on next/prev.

    Empty list is supported: current() returns None and next/prev are no-ops.
    Out-of-range starting indices are clamped, not rejected.
    """

    def __init__(self, items: List[T], index: int = 0):
        self.items: List[T] = list(items)
        if not self.items:
            self.index = 0
        else:
            self.index = max(0, min(index, len(self.items) - 1))

    def __len__(self) -> int:
        return len(self.items)

    def current(self) -> Optional[T]:
        if not self.items:
            return None
        return self.items[self.index]

    def go_next(self) -> Optional[T]:
        if self.items:
            self.index = (self.index + 1) % len(self.items)
        return self.current()

    def go_prev(self) -> Optional[T]:
        if self.items:
            self.index = (self.index - 1) % len(self.items)
        return self.current()

    @property
    def has_multiple(self) -> bool:
        return len(self.items) > 1
