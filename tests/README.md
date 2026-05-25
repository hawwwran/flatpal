# Flatpal tests

Pure-logic test suite: no display, no network, no GTK dependency on the
test side. Mocks are stdlib `unittest.mock`. Run before every
`./install.sh`:

```sh
./run-tests.sh
# or:  python3 -m unittest discover -s tests -v
```

289 tests covering size/date/locale parsing, the AppStream metainfo and
Flathub catalog parsers, the `flatpak remote-ls --updates` parser and
the release-since-installed diff helper, screenshot cache logic,
popularity fetch and content-type/size validation, sandbox-permission
summarisation, the running tracker (sub-instance breakdown,
freeze-position ordering, process-meta enrichment), image-gallery
navigator, and search filters and sort logic. Regression tests for
previously-fixed bugs live alongside the feature tests in the same
modules.

## Layout

```
tests/
  test_*.py             unittest modules. Auto-discovered by the runner.
  fixtures/             Real-world AppStream XML + `flatpak info -m` output
                        captured from production apps, used by the parser
                        tests so we exercise the exact strings Flathub
                        ships rather than synthetic shapes.
  custom/
    screenshot_demo.py  See "Screenshot demo" below.
```

`tests/__init__.py` makes the directory a package so `unittest discover`
can import sibling fixtures with relative imports.

## Adding a test

Drop a new `test_<topic>.py` in this directory. The discovery pattern is
the default `test*.py`, so anything else in here (the `fixtures/` dir,
`custom/screenshot_demo.py`) is left alone by the runner.

When a parser test needs a new fixture, capture the input from a real
flatpak install rather than hand-rolling the XML; `parse_*` regressions
are almost always about real-world quirks. Drop the file into
`tests/fixtures/` and load it with `Path(__file__).parent / "fixtures"
/ "<name>"`.

## Screenshot demo

The screenshots in the main README were captured against a fake
dataset so they don't depend on the demo runner having the right
twelve apps installed. Run:

```sh
python3 tests/custom/screenshot_demo.py
```

It launches the real Flatpal GTK shell with monkey-patched fixtures
wired into `core.fetch_apps`, `metainfo.load_metainfo`,
`running.RunningTracker`, and `detail._load_permissions`:

- **Installed tab**: twelve well-known Flathub apps (GIMP → LibreOffice)
  with plausible versions, sizes, and install dates spread across two
  years so the date sort shows variety.
- **Running tab**: four of them running (Bitwarden, Signal, Spotify,
  and Inkscape with three sandboxes so the multi-instance expander row
  is on display with per-sub-row PID, cmdline, and start time).
- **Explore tab**: left on real data; the Flathub catalog is public.

Settings and the GTK application-id are redirected to a per-run temp
dir (`/tmp/flatpal-screenshot-demo-*`), so the demo can run alongside a
real install without clobbering preferences. Icons resolve by
symlinking `/var/lib/flatpak/appstream/flathub/<arch>/active/icons` into
a hicolor-structured temp tree and adding it to the GTK IconTheme search
path on startup, so the fake apps render with real Flathub icons instead
of generic fallbacks.

`unittest discover` ignores this file (filename doesn't match
`test*.py`), so the patches don't leak into the real suite.

To add a different app, an extra running sandbox, or a new screenshot
scenario: edit `INSTALLED_APPS` / `RUNNING_SPEC` near the top of
`custom/screenshot_demo.py`. The app's icon should be in the Flathub
appstream cache (true for anything published on Flathub); otherwise the
demo falls back to `application-x-executable`.
