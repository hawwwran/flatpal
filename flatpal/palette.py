"""Flatpal brand palette — single source of truth, no GTK dependencies.

Imported by both `flatpal.widgets` (to feed the CSS provider that ships in
the app) and by `tools/preview_flathub.py` (to render the brand palette
card on the Flathub release preview). Keeping the values here rather than
duplicating them between the two callers means the preview page and the
running app can't drift.

Primary purple matches the icon and pairs cleanly with white text
(contrast ratio ~9.7:1 against #4B2E5F — WCAG AAA).
"""

from __future__ import annotations


BRAND_PURPLE = "#4B2E5F"
MINT_TEAL = "#1F9D8A"
FREEZE_ON_BLUE = "#3584E4"
WARM_TERRACOTTA = "#B85C3B"
DEEP_PETROL_BLUE = "#063B4C"


# Ordered list of (name, hex, role) tuples for tooling that wants to
# enumerate the palette (release preview, future style-guide doc, etc.).
# Add new colours to PALETTE_ENTRIES, not as bare module constants, so
# downstream tooling automatically picks them up. Hover variants are not
# listed here — derive them in the CSS with `shade()` instead of adding a
# new palette entry per state.
PALETTE_ENTRIES: list[tuple[str, str, str]] = [
    ("Brand purple", BRAND_PURPLE,
     "Primary — libadwaita accent (switches, suggested-action, focus rings) and sort pill"),
    ("Mint teal", MINT_TEAL,
     "Secondary — 'Installed' marker pill in the Explore tab"),
    ("Freeze blue", FREEZE_ON_BLUE,
     "Freeze-position pill, active state (Running tab)"),
    ("Warm terracotta", WARM_TERRACOTTA,
     "Reserved — future warning / attention surfaces"),
    ("Deep petrol blue", DEEP_PETROL_BLUE,
     "AdwViewSwitcher tab label colour (Running / Installed / Explore)"),
]
