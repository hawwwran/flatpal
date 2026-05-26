"""Guard against drift between `flatpal/*.py` on disk and the explicit
`install_sources([...])` list in `flatpal/meson.build`.

The Flatpak build silently skips modules not in that list, so a newly
added file works under `./install.sh` (which copies the whole
directory) but crashes at import time when the Flatpak is launched.
This test fails loudly the moment the two diverge.
"""

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "flatpal"
MESON_FILE = PKG_DIR / "meson.build"


def _listed_in_meson() -> set:
    text = MESON_FILE.read_text()
    # Single-quoted *.py filenames inside the install_sources list.
    # `subdir: 'flatpal'` and any other non-.py string is skipped by
    # the explicit `.py` suffix in the pattern.
    return set(re.findall(r"'([A-Za-z_][A-Za-z0-9_]*\.py)'", text))


def _on_disk() -> set:
    return {
        p.name for p in PKG_DIR.iterdir()
        if p.is_file() and p.suffix == ".py"
    }


class TestMesonManifest(unittest.TestCase):
    def test_every_module_is_registered(self):
        missing = _on_disk() - _listed_in_meson()
        self.assertFalse(
            missing,
            "These .py files exist under flatpal/ but aren't registered "
            "in flatpal/meson.build, so the Flatpak build will silently "
            f"drop them and crash at import time: {sorted(missing)}",
        )

    def test_no_stale_entries(self):
        stale = _listed_in_meson() - _on_disk()
        self.assertFalse(
            stale,
            "These names appear in flatpal/meson.build but no matching "
            ".py file exists; the Flatpak build will fail looking for "
            f"missing sources: {sorted(stale)}",
        )


if __name__ == "__main__":
    unittest.main()
