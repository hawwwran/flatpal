"""GTK-agnostic differ between a list of row dicts and an ordered container.

Used by the Running tab to diff a new sample of running apps against the
widgets currently in the listbox. Lives in its own module (no `gi` import)
so the diff logic can be exercised by unit tests without GTK.
"""

from __future__ import annotations

from typing import Callable, Iterator, Optional


class RowCache:
    """Diff sorted_rows against a widget container.

    `container` only needs `append(widget)` and `remove(widget)`. Widgets
    must carry `.app_id` and `.update(row)`. `make_widget(row)` produces a
    fresh widget; `expected_type(row)` is the type a row of this shape
    should be, so we can evict on single↔multi-instance flips.
    `on_new_widget` is an optional post-create hook (used to wire up signal
    handlers).
    """

    def __init__(
        self,
        container,
        make_widget: Callable[[dict], object],
        expected_type: Callable[[dict], type],
        on_new_widget: Optional[Callable[[object], None]] = None,
    ):
        self._container = container
        self._make = make_widget
        self._expected = expected_type
        self._on_new = on_new_widget
        self._cache: dict = {}
        self.rendered_order: list[str] = []

    def get(self, app_id: str):
        return self._cache.get(app_id)

    def iter_widgets(self) -> Iterator[object]:
        for app_id in self.rendered_order:
            widget = self._cache.get(app_id)
            if widget is not None:
                yield widget

    def clear(self) -> None:
        for widget in list(self.iter_widgets()):
            try:
                self._container.remove(widget)
            except Exception:  # noqa: BLE001
                pass
        self._cache.clear()
        self.rendered_order = []

    def reset_order(self) -> None:
        """Forget the previous render order without touching widgets.

        Used by set_sort so the next render() falls into the cold-path
        re-append branch and lays widgets out in the new sort order.
        """
        self.rendered_order = []

    def render(self, sorted_rows: list[dict]) -> None:
        new_data_by_id = {r["id"]: r for r in sorted_rows}
        new_ids = set(new_data_by_id.keys())

        # 1) Apps that vanished → drop their widgets.
        for app_id in list(self._cache.keys()):
            if app_id not in new_ids:
                widget = self._cache.pop(app_id)
                try:
                    self._container.remove(widget)
                except Exception:  # noqa: BLE001
                    pass

        # 2) Sandbox count flipped between 1 and >1 → need a different widget
        #    class (ActionRow ↔ ExpanderRow). Evict; step 3 will recreate.
        for app_id in list(self._cache.keys()):
            if not isinstance(
                self._cache[app_id], self._expected(new_data_by_id[app_id])
            ):
                widget = self._cache.pop(app_id)
                try:
                    self._container.remove(widget)
                except Exception:  # noqa: BLE001
                    pass

        # 3) Decide whether we can update in place.
        desired_order = [r["id"] for r in sorted_rows]
        current_order = [aid for aid in self.rendered_order if aid in self._cache]

        if current_order == desired_order and current_order:
            # Hot path: same apps, same order. Mutate each row's labels.
            # No container structural change — tooltips and hover states all
            # survive the refresh.
            for app_id in desired_order:
                self._cache[app_id].update(new_data_by_id[app_id])
        else:
            # Cold path: order changed or rows added. Detach every cached
            # widget that's still in the container (post-eviction _cache
            # values are exactly that set), then re-append in the new order.
            # Trusting `current_order` instead would miss widgets after
            # `reset_order()` wipes rendered_order.
            for widget in list(self._cache.values()):
                try:
                    self._container.remove(widget)
                except Exception:  # noqa: BLE001
                    pass
            for app_id in desired_order:
                existing = self._cache.get(app_id)
                if existing is not None:
                    existing.update(new_data_by_id[app_id])
                else:
                    existing = self._make(new_data_by_id[app_id])
                    if self._on_new is not None:
                        self._on_new(existing)
                    self._cache[app_id] = existing
                self._container.append(existing)

        self.rendered_order = desired_order
