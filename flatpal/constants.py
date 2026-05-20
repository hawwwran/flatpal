"""Tuning knobs centralised in one place.

Anything that's "small enough to inline" gets pulled here when the same value
shows up in two modules, or when it's the kind of thing a user might
plausibly want to change without grepping the codebase.
"""

# ----- Detail page screenshots ------------------------------------------------

# Thumbnail click-target rectangle. The actual image is scaled to COVER inside.
THUMB_W = 280
THUMB_H = 160

# ----- Explore tab pagination ------------------------------------------------

# Initial visible row count for the popular shelf and search results.
INITIAL_LIMIT = 50
# How many extra rows each "Show more" click reveals.
LOAD_MORE_INCREMENT = 50
# Ceiling for both lists. The popularity API returns 1 000 items, so going
# beyond that for the shelf would be wasted; for search results the catalog
# is ~4 000 but few queries match more.
MAX_LIMIT = 1000

# ----- Running tab -----------------------------------------------------------

# How often the running-apps tab re-samples CPU/RSS while it's visible.
REFRESH_MS = 2000

# ----- Screenshot cache -------------------------------------------------------

# Soft cap on the on-disk screenshot cache. Older files are evicted by mtime
# until the remaining set fits. ~200 MB is enough for ~200 screenshots of
# 1 MB each — well over what a normal browse session generates.
SCREENSHOT_CACHE_MAX_BYTES = 200 * 1024 * 1024
