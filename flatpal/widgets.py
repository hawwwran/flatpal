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


# Brand palette is canonical in flatpal/palette.py — kept there as plain
# constants (no GTK dep) so tools/preview_flathub.py and any future style-
# guide tooling can read the same source. Adding a new colour? Edit
# palette.py and PALETTE_ENTRIES, then wire it into the CSS block below.
from .palette import (
    BRAND_PURPLE as _BRAND_PURPLE,
    DEEP_PETROL_BLUE as _DEEP_PETROL_BLUE,
    FREEZE_ON_BLUE as _FREEZE_ON_BLUE,
    MINT_TEAL as _MINT_TEAL,
    WARM_TERRACOTTA as _WARM_TERRACOTTA,
)

_PILL_CSS = (
    """
    /* Brand palette as GTK named colours — the rules below reference
       them by name (e.g. @flatpal_purple) so the Flatpak release ships a
       single source of truth for every brand surface. Adding a new hex
       inline is a code-smell: add it here first and reference it via
       its name. */
    @define-color flatpal_purple %(purple)s;
    @define-color flatpal_mint %(mint)s;
    @define-color flatpal_freeze_on %(freeze_on)s;
    @define-color flatpal_terracotta %(terracotta)s;
    @define-color flatpal_petrol %(petrol)s;

    /* Re-bind libadwaita's accent palette to the brand purple so every
       widget that reads the accent — Gtk.Switch in the "on" state, focus
       rings, .suggested-action buttons (e.g. the Retry button on the
       Explore network-error page), hyperlinks, Adw spinners — picks up
       the primary brand colour. Done via @define-color rather than
       AdwStyleManager.set_accent_color so we can pin the exact brand
       hex instead of libadwaita's PURPLE preset (#8757a4) which sits
       lighter and less saturated than our brand. */
    @define-color accent_bg_color @flatpal_purple;
    @define-color accent_color @flatpal_purple;
    @define-color accent_fg_color #FFFFFF;

    .flatpal-sort-pill {
        background-color: @flatpal_purple;
        color: #FFFFFF;
        padding: 1px 8px;
        border-radius: 9999px;
        font-weight: 500;
    }

    .flatpal-installed-pill {
        background-color: @flatpal_mint;
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
        background-color: @flatpal_freeze_on;
        color: #FFFFFF;
    }
    .flatpal-freeze-pill.flatpal-freeze-pill-on:hover,
    .flatpal-freeze-pill.flatpal-freeze-pill-on:checked:hover {
        /* GTK CSS shade(c, k) scales the colour's HSL lightness by k.
           1.15 lifts Freeze Blue (HSL L=55%%) to L=~63%%, matching the
           old hand-picked #5BA0EC hover constant within 1-2 units per
           channel - close enough that the perceptual hover delta is
           preserved without carrying a second hex through the palette. */
        background-color: shade(@flatpal_freeze_on, 1.15);
    }

    /* AdwViewSwitcher tabs (Running / Installed / Explore). Set the base
       colour to Deep Petrol Blue so inactive tab labels + icons read in
       brand colour against the header. libadwaita keeps its own accent
       background for the active tab; this only re-tones the unselected
       state. */
    .view-switcher button {
        color: @flatpal_petrol;
    }
    """ % {
        "purple": _BRAND_PURPLE,
        "mint": _MINT_TEAL,
        "freeze_on": _FREEZE_ON_BLUE,
        "terracotta": _WARM_TERRACOTTA,
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
    """Non-interactive brand-purple pill for the current sort key.

    The pill itself is a plain Gtk.Label so it stays visually quiet;
    `app.py` attaches a `Gtk.GestureClick` to it post-construction that
    pops the existing top-left sort button — clicking the pill teaches
    the user where the sort menu lives without duplicating UI affordances.
    """
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
