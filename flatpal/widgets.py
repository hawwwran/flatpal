"""Shared UI primitives used across the three tabs.

Pill variants (``make_sort_pill`` / ``make_freeze_pill`` /
``make_installed_pill`` / ``make_update_pill``) plus a small set of layout
helpers (``make_list_clamp``, ``make_status_label``, ``clear_listbox``) that
keep the Running / Installed / Explore tabs visually identical without each
page re-implementing the same Gtk plumbing.

The CSS provider for the pills is installed lazily once per display so
importing this module from a non-GTK context (or a unit test) doesn't
side-effect.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402


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


# ----- Layout helpers -------------------------------------------------------

# Width allocated to the boxed lists on each tab. Encapsulated inside the
# clamp builder below so a future tweak updates all three tabs in lockstep
# instead of needing to edit the same constant in three page files.
_LIST_MAX_WIDTH = 900


def make_list_clamp(child: Gtk.Widget, *, vexpand: bool = False) -> Adw.Clamp:
    """Wrap `child` in the boxed-list Adw.Clamp shared by every tab.

    `tightening_threshold == maximum_size` flattens Adw.Clamp's default
    cubic-ease window (~400..1150 px) into a hard `min(for_size, max)`.
    Combined with `hexpand=True` propagating up the widget tree, the result
    is "fixed at max on wide windows; shrinks linearly only when the window
    itself is narrower than max." Without the tightening, content-driven
    jitter in the clamp's allocation rides the easing curve and the search
    bar visibly shifts a few pixels when the stack or status text changes.
    """
    clamp = Adw.Clamp()
    clamp.set_maximum_size(_LIST_MAX_WIDTH)
    clamp.set_tightening_threshold(_LIST_MAX_WIDTH)
    clamp.set_child(child)
    clamp.set_hexpand(True)
    if vexpand:
        clamp.set_vexpand(True)
    return clamp


def make_status_label() -> Gtk.Label:
    """Dim caption label used by each tab's status row.

    Left-aligned, single line, brand-uniform across Running / Installed /
    Explore so all three status rows look identical.
    """
    label = Gtk.Label()
    label.add_css_class("dim-label")
    label.add_css_class("caption")
    label.set_halign(Gtk.Align.START)
    label.set_xalign(0.0)
    return label


def clear_listbox(listbox: Gtk.ListBox) -> None:
    """Remove every child of `listbox` via the first-child / next-sibling walk."""
    child = listbox.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        listbox.remove(child)
        child = nxt

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

    .flatpal-update-pill {
        background-color: @flatpal_terracotta;
        color: #FFFFFF;
        padding: 1px 8px;
        border-radius: 9999px;
        font-weight: 500;
    }

    /* Detail-page "Update available" callout. Soft terracotta wash so the
       box reads as the same family as the per-row Update pill above —
       same hue, different weight. The 0.12 alpha on the background and
       0.35 on the border are matched against Adwaita's `.card`
       contrast budget so body text inside stays readable in both
       light and dark themes without recolouring it. */
    .flatpal-update-card {
        background-color: alpha(@flatpal_terracotta, 0.12);
        border: 1px solid alpha(@flatpal_terracotta, 0.35);
        border-radius: 12px;
        padding: 16px;
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


def make_update_pill(
    label: str = "Update available", tooltip: Optional[str] = None,
) -> Gtk.Label:
    """Non-interactive Warm Terracotta pill flagging an available update.

    Used by every tab + the detail page so a row hops into attention without
    the user having to dig into Software. The caller passes a tooltip
    describing the new version (see `update_tooltip()` for the shared
    wording) because the pill itself stays short.
    """
    install_pill_css()
    pill = Gtk.Label(label=label)
    pill.add_css_class("caption")
    pill.add_css_class("flatpal-update-pill")
    pill.set_valign(Gtk.Align.CENTER)
    if tooltip:
        pill.set_tooltip_text(tooltip)
    return pill


def update_tooltip(
    installed_version: Optional[str], update_info: dict,
) -> str:
    """Shared tooltip wording for every Update-pill caller.

    Keeping the format in one place avoids the Installed / Explore /
    Running tabs drifting into different phrasings. When the caller can
    supply the installed version (Installed + Running tabs) the full
    "{current} → {new} (on {origin})" form is used; the Explore tab,
    which only sees catalog entries, gets the short form without the
    "{current} →" prefix rather than a noisy "?" placeholder.
    """
    new_v = update_info.get("version") or "?"
    origin = update_info.get("origin") or "remote"
    if installed_version:
        return f"Update available: {installed_version} → {new_v} (on {origin})"
    return f"Update available → {new_v} (on {origin})"


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
