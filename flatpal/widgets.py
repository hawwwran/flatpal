"""Shared UI primitives — pills used across the three tabs.

Three pill variants today:

* ``make_sort_pill`` — purple Flatpal-brand pill (`#4B2E5F` on white text)
  used to surface the current sort key on the Running / Installed / Explore
  tabs. Non-interactive; the page updates its label whenever sort changes.

* ``make_freeze_pill`` — toggleable pill that says "Freeze position". Blue
  when active, soft gray when not. Used on the Running tab to let the user
  pin the row order across CPU/memory refreshes; the consuming page reacts to
  the callback by either preserving its `_rendered_order` or re-sorting.

* ``make_installed_pill`` — Mint Teal pill that flags a row in the Explore
  tab as "Installed" — replaces an earlier dim text label so the marker
  scans at a glance.

The CSS provider is installed lazily once per display so importing this
module from a non-GTK context (or a unit test) doesn't side-effect.
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402


# Brand palette. Primary purple matches the icon and pairs cleanly with
# white text (contrast ratio ~9.7:1 against #4B2E5F — passes WCAG AAA).
# The three later additions extend the palette for the secondary "Installed"
# marker, future warning/attention surfaces, and the AdwViewSwitcher tab
# text colour respectively.
_BRAND_PURPLE = "#4B2E5F"        # primary — sort pill, future hero accents
_FREEZE_ON_BLUE = "#3584E4"      # toggle "on" state for the freeze pill
_MINT_TEAL = "#1F9D8A"           # secondary accent — Installed marker
_WARM_TERRACOTTA = "#B85C3B"     # reserved — warning / attention surfaces
_DEEP_PETROL_BLUE = "#063B4C"    # foreground — AdwViewSwitcher tab text

_PILL_CSS = (
    """
    .flatpal-sort-pill {
        background-color: %(brand)s;
        color: #FFFFFF;
        padding: 1px 8px;
        border-radius: 9999px;
        font-weight: 500;
    }

    .flatpal-installed-pill {
        background-color: %(mint)s;
        color: #FFFFFF;
        padding: 1px 8px;
        border-radius: 9999px;
        font-weight: 500;
    }

    .flatpal-freeze-pill {
        padding: 1px 10px;
        border-radius: 9999px;
        font-weight: 500;
        min-height: 0;
        background-image: none;
        border: none;
        box-shadow: none;
        text-shadow: none;
    }
    .flatpal-freeze-pill.flatpal-freeze-pill-off {
        background-color: alpha(currentColor, 0.12);
        color: @window_fg_color;
    }
    .flatpal-freeze-pill.flatpal-freeze-pill-off:hover {
        background-color: alpha(currentColor, 0.20);
    }
    .flatpal-freeze-pill.flatpal-freeze-pill-on,
    .flatpal-freeze-pill.flatpal-freeze-pill-on:checked {
        background-color: %(freeze_on)s;
        color: #FFFFFF;
    }
    .flatpal-freeze-pill.flatpal-freeze-pill-on:hover,
    .flatpal-freeze-pill.flatpal-freeze-pill-on:checked:hover {
        background-color: #5BA0EC;
    }

    /* AdwViewSwitcher tabs (Running / Installed / Explore). Set the base
       colour to Deep Petrol Blue so inactive tab labels + icons read in
       brand colour against the header. libadwaita keeps its own accent
       background for the active tab; this only re-tones the unselected
       state. */
    .view-switcher button {
        color: %(petrol)s;
    }
    """ % {
        "brand": _BRAND_PURPLE,
        "mint": _MINT_TEAL,
        "freeze_on": _FREEZE_ON_BLUE,
        "petrol": _DEEP_PETROL_BLUE,
    }
).encode("utf-8")


_pill_provider_installed = False


def install_pill_css() -> None:
    """Add the pill CSS provider on the default display. Idempotent.

    Safe to call from any tab's __init__; the no-op shortcut means we don't
    accumulate providers if the user switches tabs many times.
    """
    global _pill_provider_installed
    if _pill_provider_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_PILL_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _pill_provider_installed = True


def make_sort_pill(initial_label: str = "") -> Gtk.Label:
    """Non-interactive brand-purple pill for the current sort key."""
    install_pill_css()
    pill = Gtk.Label(label=initial_label)
    pill.add_css_class("caption")
    pill.add_css_class("flatpal-sort-pill")
    pill.set_valign(Gtk.Align.CENTER)
    return pill


def make_installed_pill(label: str = "Installed") -> Gtk.Label:
    """Non-interactive Mint Teal pill marking an Explore row as already installed."""
    install_pill_css()
    pill = Gtk.Label(label=label)
    pill.add_css_class("caption")
    pill.add_css_class("flatpal-installed-pill")
    pill.set_valign(Gtk.Align.CENTER)
    return pill


def make_freeze_pill(
    on_toggle: Callable[[bool], None],
    *,
    initial: bool = False,
    tooltip: str = (
        "Freeze the current row order while you compare apps. New apps "
        "appear at the bottom; everything else stays put even when CPU "
        "or memory changes."
    ),
) -> Gtk.ToggleButton:
    """Toggleable pill. Blue when active, soft gray when not.

    `on_toggle(active: bool)` fires after the visual state has already
    updated. ToggleButton's native :checked pseudo-class plus our own
    `flatpal-freeze-pill-on/off` classes both follow the active state — the
    pseudo-class catches GTK's default frame draw, our class wins on colour.
    """
    install_pill_css()
    btn = Gtk.ToggleButton(label="Freeze position")
    btn.add_css_class("flatpal-freeze-pill")
    btn.add_css_class("caption")
    btn.set_valign(Gtk.Align.CENTER)
    btn.set_active(bool(initial))
    btn.set_tooltip_text(tooltip)
    _apply_freeze_classes(btn)

    def _on_toggled(b: Gtk.ToggleButton) -> None:
        _apply_freeze_classes(b)
        on_toggle(b.get_active())

    btn.connect("toggled", _on_toggled)
    return btn


def _apply_freeze_classes(btn: Gtk.ToggleButton) -> None:
    if btn.get_active():
        btn.add_css_class("flatpal-freeze-pill-on")
        btn.remove_css_class("flatpal-freeze-pill-off")
    else:
        btn.remove_css_class("flatpal-freeze-pill-on")
        btn.add_css_class("flatpal-freeze-pill-off")
