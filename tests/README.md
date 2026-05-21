# Flatpal — tests

Pure-logic test suite — no display, no network, no GTK dependency on the
test side. Mocks are stdlib `unittest.mock`. Run before every
`./install.sh`:

```sh
./run-tests.sh
# or:  python3 -m unittest discover -s tests -v
```

252 tests covering size/date/locale parsing, the AppStream metainfo and
Flathub catalog parsers, screenshot cache logic, popularity fetch &
content-type/size validation, sandbox-permission summarisation, the
running tracker (including sub-instance breakdown, freeze-position
ordering and process-meta enrichment), image-gallery navigator, search
filters and sort logic — plus regression tests for every bug we've found
and fixed.

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
flatpak install rather than hand-rolling the XML — `parse_*` regressions
are almost always about real-world quirks we didn't anticipate. Drop
the file into `tests/fixtures/` and load it with `Path(__file__).parent
/ "fixtures" / "<name>"`.

## Screenshot demo

The screenshots in the main README were captured against a **fake**
dataset so the project doesn't depend on whoever's running the demo
having the right twelve apps installed. Run:

```sh
python3 tests/custom/screenshot_demo.py
```

It launches the real Flatpal GTK shell, but with monkey-patched
fixtures wired into `core.fetch_apps`, `metainfo.load_metainfo`,
`running.RunningTracker` and `detail._load_permissions`:

- **Installed tab** — twelve well-known Flathub apps (GIMP → LibreOffice)
  with plausible versions, sizes and install dates spread across two
  years so the date sort shows variety.
- **Running tab** — four of them mid-flight (Bitwarden, Signal, Spotify
  and **Inkscape with three sandboxes** so the multi-instance expander
  row is on display, complete with per-sub-row PID, cmdline and start
  time).
- **Explore tab** — left on real data; the Flathub catalog is public so
  there's nothing to fake.

Settings and the GTK application-id are redirected to a per-run temp
dir (`/tmp/flatpal-screenshot-demo-*`), so the demo can run alongside a
real install without clobbering preferences. Icons resolve by
symlinking `/var/lib/flatpak/appstream/flathub/<arch>/active/icons` into
a hicolor-structured temp tree and adding it to the GTK IconTheme search
path on startup — so the fake apps render with real Flathub icons
instead of generic fallbacks.

`unittest discover` ignores this file (filename doesn't match
`test*.py`), so the patches don't leak into the real suite.

To add a different app, an extra running sandbox, or a new screenshot
scenario: edit `INSTALLED_APPS` / `RUNNING_SPEC` near the top of
`custom/screenshot_demo.py`. Make sure the app's icon is in the Flathub
appstream cache (it should be for anything published on Flathub —
otherwise the demo falls back to `application-x-executable`).
