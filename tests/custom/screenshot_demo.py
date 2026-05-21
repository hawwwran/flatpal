#!/usr/bin/env python3
"""Run Flatpal with simulated data — for screenshot capture, not real use.

Patches the data-fetching surfaces (`core.fetch_apps`, `metainfo.load_metainfo`,
`running.RunningTracker`, `detail._load_permissions`) with curated fakes,
then launches the real GTK shell. The Installed tab shows a dozen well-known
Flathub apps; the Running tab shows four of them mid-flight, including
Inkscape with three separate sandboxes so the multi-instance expander row
is visible.

Settings and the GTK application-id are redirected to a throw-away location
so the demo can run side-by-side with a real Flatpal install without
clobbering preferences.

Run with:
    ./run-screenshot-demo.sh
    # or: python3 tests/custom/screenshot_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path


# Make the repo importable when this file is executed as a path-style script.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ----- 1. Load leaf modules so we can patch them before the pages bind. ---

import flatpal.settings as _settings  # noqa: E402
import flatpal.core as _core          # noqa: E402
import flatpal.metainfo as _metainfo  # noqa: E402
import flatpal.running as _running    # noqa: E402


# ----- 2. Fake data ------------------------------------------------------

NOW = datetime.now()


def _days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


# Curated list of well-known Flathub apps. Sizes / versions are plausible
# but invented; install dates spread across two years so the date sort
# produces visible variety. All IDs match the real Flathub catalog so the
# Flathub-cached icons resolve when the IconTheme search path is set up
# below.
INSTALLED_APPS = [
    {
        "id": "org.gimp.GIMP",
        "name": "GIMP",
        "version": "2.10.36",
        "branch": "stable",
        "origin": "flathub",
        "installation": "system",
        "size_str": "514,7 MB",
        "size_bytes": 514_700_000,
        "installed": _days_ago(420),
        "summary": "Create images and edit photographs",
        "developer_name": "The GIMP Team",
    },
    {
        "id": "org.inkscape.Inkscape",
        "name": "Inkscape",
        "version": "1.4",
        "branch": "stable",
        "origin": "flathub",
        "installation": "system",
        "size_str": "382,1 MB",
        "size_bytes": 382_100_000,
        "installed": _days_ago(385),
        "summary": "Vector graphics editor",
        "developer_name": "Inkscape Project",
    },
    {
        "id": "org.kde.krita",
        "name": "Krita",
        "version": "5.2.6",
        "branch": "stable",
        "origin": "flathub",
        "installation": "system",
        "size_str": "478,3 MB",
        "size_bytes": 478_300_000,
        "installed": _days_ago(310),
        "summary": "Digital painting and illustration",
        "developer_name": "Krita Foundation",
    },
    {
        "id": "org.signal.Signal",
        "name": "Signal Desktop",
        "version": "7.27.0",
        "branch": "stable",
        "origin": "flathub",
        "installation": "user",
        "size_str": "292,4 MB",
        "size_bytes": 292_400_000,
        "installed": _days_ago(204),
        "summary": "Private messenger",
        "developer_name": "Signal Foundation",
    },
    {
        "id": "com.spotify.Client",
        "name": "Spotify",
        "version": "1.2.45",
        "branch": "stable",
        "origin": "flathub",
        "installation": "user",
        "size_str": "203,8 MB",
        "size_bytes": 203_800_000,
        "installed": _days_ago(178),
        "summary": "Online music streaming service",
        "developer_name": "Spotify AB",
    },
    {
        "id": "com.discordapp.Discord",
        "name": "Discord",
        "version": "0.0.71",
        "branch": "stable",
        "origin": "flathub",
        "installation": "user",
        "size_str": "245,9 MB",
        "size_bytes": 245_900_000,
        "installed": _days_ago(112),
        "summary": "Chat for communities and friends",
        "developer_name": "Discord Inc.",
    },
    {
        "id": "com.obsproject.Studio",
        "name": "OBS Studio",
        "version": "31.0.0",
        "branch": "stable",
        "origin": "flathub",
        "installation": "system",
        "size_str": "318,2 MB",
        "size_bytes": 318_200_000,
        "installed": _days_ago(95),
        "summary": "Record and stream live video",
        "developer_name": "OBS Project",
    },
    {
        "id": "org.audacityteam.Audacity",
        "name": "Audacity",
        "version": "3.6.4",
        "branch": "stable",
        "origin": "flathub",
        "installation": "system",
        "size_str": "189,5 MB",
        "size_bytes": 189_500_000,
        "installed": _days_ago(72),
        "summary": "Multi-track audio editor and recorder",
        "developer_name": "Audacity Team",
    },
    {
        "id": "org.blender.Blender",
        "name": "Blender",
        "version": "4.3.0",
        "branch": "stable",
        "origin": "flathub",
        "installation": "system",
        "size_str": "743,6 MB",
        "size_bytes": 743_600_000,
        "installed": _days_ago(38),
        "summary": "3D modeling, animation, rendering and post-production",
        "developer_name": "Blender Foundation",
    },
    {
        "id": "org.mozilla.firefox",
        "name": "Firefox",
        "version": "133.0",
        "branch": "stable",
        "origin": "flathub",
        "installation": "user",
        "size_str": "412,3 MB",
        "size_bytes": 412_300_000,
        "installed": _days_ago(22),
        "summary": "Fast, Private & Safe Web Browser",
        "developer_name": "Mozilla",
    },
    {
        "id": "com.bitwarden.desktop",
        "name": "Bitwarden",
        "version": "2024.10.0",
        "branch": "stable",
        "origin": "flathub",
        "installation": "user",
        "size_str": "224,7 MB",
        "size_bytes": 224_700_000,
        "installed": _days_ago(11),
        "summary": "Open-source password manager",
        "developer_name": "Bitwarden Inc.",
    },
    {
        "id": "org.libreoffice.LibreOffice",
        "name": "LibreOffice",
        "version": "24.8.3",
        "branch": "stable",
        "origin": "flathub",
        "installation": "system",
        "size_str": "684,1 MB",
        "size_bytes": 684_100_000,
        "installed": _days_ago(4),
        "summary": "The LibreOffice productivity suite",
        "developer_name": "The Document Foundation",
    },
]


# Running tab — subset of the installed apps. Inkscape carries three
# sandboxes so the multi-instance expander row is on display. Stats are
# constant across refreshes (see _FakeProcess.cpu_percent below) so
# screenshots don't flicker between samples.
RUNNING_SPEC = [
    {
        "id": "com.bitwarden.desktop",
        "sandboxes": [
            {
                "pid": 41101,
                "cpu": 0.4,
                "rss": 138 * 1024 * 1024,
                "cmdline": ["/app/bin/bitwarden",
                            "--enable-features=UseOzonePlatform",
                            "--ozone-platform=wayland"],
                "comm": "bitwarden",
                "started_offset": -45 * 60,
            },
        ],
    },
    {
        "id": "org.signal.Signal",
        "sandboxes": [
            {
                "pid": 39820,
                "cpu": 1.2,
                "rss": 284 * 1024 * 1024,
                "cmdline": ["/app/bin/signal-desktop",
                            "--enable-features=UseOzonePlatform",
                            "--ozone-platform=wayland"],
                "comm": "signal-desktop",
                "started_offset": -3 * 3600,
            },
        ],
    },
    {
        "id": "com.spotify.Client",
        "sandboxes": [
            {
                "pid": 38110,
                "cpu": 4.7,
                "rss": 517 * 1024 * 1024,
                "cmdline": ["/app/extra/spotify",
                            "--enable-features=UseOzonePlatform",
                            "--ozone-platform=wayland"],
                "comm": "spotify",
                "started_offset": -90 * 60,
            },
        ],
    },
    {
        "id": "org.inkscape.Inkscape",
        "sandboxes": [
            {
                "pid": 42010,
                "cpu": 0.2,
                "rss": 198 * 1024 * 1024,
                "cmdline": ["/app/bin/inkscape",
                            "/home/me/Documents/logo-final.svg"],
                "comm": "inkscape",
                "started_offset": -25 * 60,
            },
            {
                "pid": 43221,
                "cpu": 1.4,
                "rss": 224 * 1024 * 1024,
                "cmdline": ["/app/bin/inkscape",
                            "/home/me/Documents/poster-draft.svg"],
                "comm": "inkscape",
                "started_offset": -12 * 60,
            },
            {
                "pid": 44519,
                "cpu": 6.8,
                "rss": 271 * 1024 * 1024,
                "cmdline": ["/app/bin/inkscape",
                            "/home/me/Pictures/banner-export.svg"],
                "comm": "inkscape",
                "started_offset": -3 * 60,
            },
        ],
    },
]


def _running_instances():
    """Flat list of fake instances, matching `flatpak ps` parser output."""
    out = []
    for spec in RUNNING_SPEC:
        for sb in spec["sandboxes"]:
            out.append({
                "instance": f"i{sb['pid']}",
                "pid": sb["pid"],
                "child_pid": sb["pid"],
                "id": spec["id"],
                "branch": "stable",
            })
    return out


def _build_process_data():
    now = time.time()
    out = {}
    for spec in RUNNING_SPEC:
        for sb in spec["sandboxes"]:
            out[sb["pid"]] = {
                "cpu": sb["cpu"],
                "rss": sb["rss"],
                "cmdline": sb["cmdline"],
                "comm": sb["comm"],
                "started_at": now + sb["started_offset"],
            }
    return out


_PROCESS_DATA = _build_process_data()


# ----- 3. Fakes for psutil.Process ---------------------------------------

class _FakeMemInfo:
    def __init__(self, rss):
        self.rss = rss


class _FakeProcess:
    """Stable stand-in for psutil.Process — values don't change across samples.

    Mirrors the priming semantics of test_running._FakeProcess (first
    cpu_percent call returns 0.0 baseline) so the tracker's own priming
    path in `_get_process` doesn't double-count the first reading.
    """

    def __init__(self, pid):
        data = _PROCESS_DATA.get(pid, {})
        self.pid = pid
        self._cpu = float(data.get("cpu", 0.0))
        self._rss = int(data.get("rss", 0))
        self._cmdline = list(data.get("cmdline", []))
        self._comm = data.get("comm", "")
        self._started_at = data.get("started_at")
        self._primed = False

    def cpu_percent(self, interval=None):
        if not self._primed:
            self._primed = True
            return 0.0
        return self._cpu

    def memory_info(self):
        return _FakeMemInfo(self._rss)

    def children(self, recursive=False):
        return []

    def cmdline(self):
        return list(self._cmdline)

    def name(self):
        return self._comm

    def create_time(self):
        if self._started_at is None:
            raise AttributeError("no create_time fixture")
        return float(self._started_at)


# ----- 4. Fake metainfo --------------------------------------------------

def _make_meta(app):
    return {
        "id": app["id"],
        "name": app["name"],
        "summary": app["summary"],
        "description_markup": (
            f"{app['name']} — {app['summary']}.\n\n"
            "Screenshot-demo metainfo so the detail page renders without "
            "consulting the host's AppStream cache or fetching screenshots."
        ),
        "developer_name": app["developer_name"],
        "project_license": None,
        "categories": [],
        "urls": {},
        "screenshots": [],
        "releases": [],
        "cached_icon": None,
    }


_META_BY_ID = {a["id"]: _make_meta(a) for a in INSTALLED_APPS}


def _fake_load_metainfo(app_id, lang=None):
    return _META_BY_ID.get(app_id) or {
        "id": app_id, "name": app_id, "summary": "", "description_markup": "",
        "developer_name": None, "project_license": None, "categories": [],
        "urls": {}, "screenshots": [], "releases": [],
    }


# ----- 5. Apply patches BEFORE flatpal.app loads the page modules. -------
# (Settings + icon-cache setup defer to main() so importing this module is
# side-effect-free on the filesystem.)

# Installed tab — bypass `flatpak list` and `flatpak history`.
_core.fetch_apps = lambda: [dict(a) for a in INSTALLED_APPS]
_core.fetch_install_dates = lambda: {}

# Metainfo — bypass /var/lib/flatpak/.../metainfo.xml reads.
_metainfo.load_metainfo = _fake_load_metainfo


class _DemoRunningTracker(_running.RunningTracker):
    """RunningTracker pre-wired with the fake process_factory + lister."""

    def __init__(self):
        super().__init__(
            process_factory=_FakeProcess,
            lister=_running_instances,
        )


_running.RunningTracker = _DemoRunningTracker


# ----- 6. Load flatpal.app (this also loads installed_page, running_page,
#         explore_page, detail — all of which see the patches above). ----

import flatpal.app as _app_module  # noqa: E402
import flatpal.detail as _detail   # noqa: E402

_app_module.APP_ID = "com.hawwwran.flatpal.ScreenshotDemo"

# Detail page — bypass `flatpak info -m`; sandbox permissions panel is
# hidden when the list is empty.
_detail._load_permissions = lambda _app_id: []


# ----- 7. Runtime setup helpers (call site is main(), not module top) ----

def _setup_scratch_dir() -> Path:
    """Create the per-run scratch dir + seeded settings.json.

    Writing a real settings file (rather than mutating `_settings.DEFAULTS`
    in place) keeps package state untouched, so importing this module from
    elsewhere — a REPL session, a test runner — doesn't leak demo
    preferences into the host.
    """
    home = Path(tempfile.mkdtemp(prefix="flatpal-screenshot-demo-"))
    settings_path = home / "settings.json"
    settings_path.write_text(
        json.dumps({
            "last_tab": "installed",
            "installed_sort_key": "date",
            "installed_reverse": True,
            "running_sort_key": "cpu",
            "running_refresh_seconds": 2,
            # Skip the popularity API fetch so the Explore tab doesn't
            # talk to flathub.org while the demo is running.
            "show_popular": False,
        }, indent=2),
        encoding="utf-8",
    )
    _settings.DEFAULT_PATH = settings_path
    return home


def _setup_demo_icons(scratch_dir: Path):
    """Symlink Flathub's flat icon cache into a hicolor-structured temp dir.

    Flatpak stores cached icons at
        /var/lib/flatpak/appstream/flathub/<arch>/active/icons/<size>/<id>.png
    in a *flat* layout, but `Gtk.IconTheme` looks them up via the hicolor
    convention `<theme>/<size>/<category>/<name>.png`. A directory of
    symlinks reorganised into the hicolor layout is enough; we add it to
    the IconTheme search path on `startup`.

    Returns the temp dir path, or None if no Flathub icon cache is on
    disk (in which case the demo still runs, with generic fallback icons).
    """
    bases = [
        Path("/var/lib/flatpak/appstream/flathub") / os.uname().machine /
            "active" / "icons",
        Path(os.path.expanduser(
            "~/.local/share/flatpak/appstream/flathub"
        )) / os.uname().machine / "active" / "icons",
    ]
    src = next((b for b in bases if b.is_dir()), None)
    if src is None:
        return None
    dst_root = scratch_dir / "icons"
    for size in ("128x128", "64x64"):
        size_dir = src / size
        if not size_dir.is_dir():
            continue
        out_dir = dst_root / "hicolor" / size / "apps"
        out_dir.mkdir(parents=True, exist_ok=True)
        for png in size_dir.glob("*.png"):
            target = out_dir / png.name
            try:
                target.symlink_to(png)
            except FileExistsError:
                pass
    return dst_root


def main():
    scratch_dir = _setup_scratch_dir()

    print(
        f"[flatpal-demo] scratch dir: {scratch_dir}\n"
        f"[flatpal-demo] settings:    {_settings.DEFAULT_PATH}\n"
        f"[flatpal-demo] app-id:      {_app_module.APP_ID}",
        file=sys.stderr,
    )

    from flatpal.app import FlatpalApp

    icon_dir = _setup_demo_icons(scratch_dir)

    app = FlatpalApp()

    def on_startup(_app):
        if icon_dir is None:
            return
        from gi.repository import Gdk, Gtk
        display = Gdk.Display.get_default()
        if display is None:
            return
        Gtk.IconTheme.get_for_display(display).add_search_path(str(icon_dir))

    app.connect("startup", on_startup)
    # Pass only argv[0] so the underlying GApplication.command_line parser
    # doesn't see any flags meant for unittest / shells / etc.
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
