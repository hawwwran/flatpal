#!/usr/bin/env python3
"""Render the full Flathub release surface as a single preview page.

Pulls in every artefact Flathub reviewers (and end users) will see:

  - AppStream metainfo  (data/<APP_ID>.metainfo.xml)
  - Desktop entry       (data/<APP_ID>.desktop)
  - Flatpak manifest    (<APP_ID>.yaml)
  - LICENSE             (root)
  - Screenshots         (data/screenshots/*.png)

Validates the metainfo + desktop file locally (where the tools exist),
copies the icon and screenshots into temp/build/ so the page renders
standalone, writes temp/build/preview.html, and opens it in the default
browser. Re-run after editing any release artefact.

Usage:
    python3 tools/preview_flathub.py [--no-open]
"""

from __future__ import annotations

import argparse
import configparser
import json
import re
import shutil
import subprocess
import sys
import webbrowser
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path

try:
    import yaml  # PyYAML; ships with most Linux distros.
except ImportError:
    yaml = None  # type: ignore


# Known lint findings — each maps to (kind, explanation). The render layer
# uses `kind` to colour-code and group the issues; the explanation tells the
# maintainer why the lint is happening and what (if anything) to do.
#
#   resolves-at-tag : auto-fixes the moment `release-flatpal.sh` tags + pushes
#                     a real version. No action needed.
#   justify-in-pr   : a Flathub-flagged permission that is required for the
#                     app to function; reviewer will accept with rationale in
#                     the submission/update PR description.
#   informational   : present in output but doesn't block — desktop-file
#                     hints, AppStream notices, etc.
LINT_CLASSIFICATIONS: dict = {
    "screenshot-image-not-found": (
        "resolves-at-tag",
        "Screenshot URLs pin a tag (currently /v0.0.0/ placeholder). "
        "release-flatpal.sh rewrites them to the new tag on release.",
    ),
    "url-not-reachable": (
        "resolves-at-tag",
        "URL becomes reachable once the repo is public and the tag is pushed.",
    ),
    "appid-url-not-reachable": (
        "resolves-at-tag",
        "Homepage URL must point at a public, reachable page. Auto-resolves "
        "after the repo is public and the tag exists.",
    ),
    "finish-args-flatpak-spawn-access": (
        "justify-in-pr",
        "Required: app spawns host flatpak via flatpak-spawn (its whole "
        "purpose). Flatseal precedent — justify in the PR description.",
    ),
    "finish-args-flatpak-system-folder-ro-access": (
        "justify-in-pr",
        "Required: read host AppStream cache + per-app metainfo XML from "
        "/var/lib/flatpak. Flatseal precedent — justify in the PR description.",
    ),
    "finish-args-unnecessary-xdg-data-flatpak-ro-access": (
        "justify-in-pr",
        "Required: same as system-folder above but for the per-user install "
        "at xdg-data/flatpak. Flatseal precedent — justify in the PR.",
    ),
    "module-flatpal-source-git-no-commit-with-tag": (
        "resolves-at-tag",
        "Per Option A in the release plan, upstream stays tag-only; "
        "release-flatpal.sh Mode 2 pins the commit SHA on the Flathub-managed "
        "manifest before submitting the update PR.",
    ),
}


def _classify_validation(name: str, raw: str, rc: int) -> tuple[str, list[dict], list[str]]:
    """Categorise lint findings for prettier rendering.

    Returns (overall_class, known_issues, unknown_findings).

    - overall_class: "ok" (nothing to worry about), "expected" (all findings
      are in LINT_CLASSIFICATIONS), or "attention" (at least one unknown
      finding — needs human eyes).
    - known_issues: list of {id, kind, explanation} for findings we recognise.
    - unknown_findings: list of finding identifiers we don't have a
      classification for. Surfacing them prompts an update to
      LINT_CLASSIFICATIONS or actual fixes.
    """
    known: list[dict] = []
    unknown: list[str] = []

    def push(lint_id: str) -> None:
        cls = LINT_CLASSIFICATIONS.get(lint_id)
        if cls is None:
            unknown.append(lint_id)
        else:
            kind, expl = cls
            known.append({"id": lint_id, "kind": kind, "explanation": expl})

    if name.startswith("AppStream"):
        # appstreamcli lines look like: "W: io.…flatpal:65: screenshot-image-not-found"
        for line in raw.splitlines():
            m = re.match(r"^[WIEH]:\s*\S+:\s*(\S+)", line)
            if m:
                push(m.group(1))

    elif name.startswith("Flatpak manifest"):
        # flatpak-builder-lint emits JSON {"errors":[…], "warnings":[…], …}.
        try:
            data = json.loads(raw)
            for ident in data.get("errors", []) + data.get("warnings", []):
                push(str(ident))
        except (json.JSONDecodeError, TypeError):
            if rc != 0:
                unknown.append("(unparseable lint output)")

    elif name.startswith("Desktop entry"):
        # desktop-file-validate emits free-form text; rc=0 means hint-only.
        if rc != 0:
            unknown.append(f"desktop-file-validate rc={rc}")

    if unknown:
        overall = "attention"
    elif known:
        overall = "expected"
    else:
        overall = "ok"
    return overall, known, unknown


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # so `flatpal.palette` is importable below

from flatpal.palette import PALETTE_ENTRIES  # noqa: E402

DATA = REPO / "data"
METAINFO = DATA / "io.github.hawwwran.flatpal.metainfo.xml"
DESKTOP = DATA / "io.github.hawwwran.flatpal.desktop"
MANIFEST = REPO / "io.github.hawwwran.flatpal.yaml"
DEV_MANIFEST = REPO / "io.github.hawwwran.flatpal.dev.yaml"
LICENSE_FILE = REPO / "LICENSE"
SCREENSHOT_DIR = DATA / "screenshots"
ICON_PNG = DATA / "flatpal-icon-256x256.png"
OUT_DIR = REPO / "temp" / "build"
PREVIEW = OUT_DIR / "preview.html"


# ---------- parsing ----------

def collapse(text: str | None) -> str:
    """Strip + join multi-line indented XML text into a single line."""
    if not text:
        return ""
    return " ".join(line.strip() for line in text.split("\n") if line.strip())


def parse_rich(elem) -> list[dict]:
    """Description / release-description body: <p>, <ul>, <ol>."""
    blocks: list[dict] = []
    if elem is None:
        return blocks
    for child in elem:
        if child.tag == "p":
            blocks.append({"type": "p", "text": collapse(child.text)})
        elif child.tag in ("ul", "ol"):
            items = [collapse(li.text) for li in child.findall("li")]
            blocks.append({"type": child.tag, "items": items})
    return blocks


def parse_metainfo(path: Path) -> dict:
    root = ET.parse(path).getroot()

    def t(tag: str) -> str | None:
        e = root.find(tag)
        return collapse(e.text) if (e is not None and e.text) else None

    info: dict = {
        "id": t("id"),
        "name": t("name"),
        "summary": t("summary"),
        "metadata_license": t("metadata_license"),
        "project_license": t("project_license"),
        "description": parse_rich(root.find("description")),
    }

    launchable = root.find("launchable")
    info["launchable"] = (collapse(launchable.text) if launchable is not None else None)

    dev = root.find("developer")
    if dev is not None:
        n = dev.find("name")
        info["developer"] = {
            "id": dev.get("id"),
            "name": collapse(n.text) if n is not None else None,
        }
    else:
        info["developer"] = {}

    info["urls"] = [
        {"type": u.get("type"), "href": collapse(u.text)}
        for u in root.findall("url")
    ]
    info["categories"] = [collapse(c.text) for c in root.findall("./categories/category")]
    info["keywords"] = [collapse(k.text) for k in root.findall("./keywords/keyword")]

    cr = root.find("content_rating")
    if cr is not None:
        info["content_rating"] = {
            "type": cr.get("type"),
            "attributes": [
                {"id": a.get("id"), "value": collapse(a.text)}
                for a in cr.findall("content_attribute")
            ],
        }
    else:
        info["content_rating"] = {}

    info["branding"] = [
        {
            "type": c.get("type"),
            "scheme_preference": c.get("scheme_preference"),
            "value": collapse(c.text),
        }
        for c in root.findall("./branding/color")
    ]

    info["screenshots"] = []
    for s in root.findall("./screenshots/screenshot"):
        cap = s.find("caption")
        img = s.find("image")
        info["screenshots"].append({
            "is_default": s.get("type") == "default",
            "caption": collapse(cap.text) if cap is not None else "",
            "url": collapse(img.text) if img is not None else "",
        })

    info["releases"] = []
    for r in root.findall("./releases/release"):
        info["releases"].append({
            "version": r.get("version"),
            "date": r.get("date"),
            "description": parse_rich(r.find("description")),
        })
    return info


def parse_desktop(path: Path) -> dict:
    """Read the [Desktop Entry] block as an ordered dict-of-strings."""
    if not path.exists():
        return {}
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    # configparser lowercases keys by default; we want exact case to mirror
    # what GNOME Shell / desktop-file-validate actually see.
    cp.optionxform = lambda opt: opt  # type: ignore[assignment]
    cp.read(path, encoding="utf-8")
    section = "Desktop Entry"
    if section not in cp:
        return {}
    return {k: cp[section][k] for k in cp[section]}


def parse_manifest(path: Path) -> dict:
    """Load the YAML Flatpak manifest. Returns {} when PyYAML is unavailable."""
    if not path.exists() or yaml is None:
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def license_summary(path: Path) -> dict:
    """Title (first non-blank line), line count, size, first ~6 lines for context."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    head = [line for line in lines[:8] if line.strip()][:6]
    title = head[0].strip() if head else "(empty)"
    return {
        "path": str(path.relative_to(REPO)),
        "title": title,
        "lines": len(lines),
        "bytes": path.stat().st_size,
        "head": head,
    }


# ---------- rendering ----------

def remote_to_local(url: str) -> str:
    return f"screenshots/{Path(url).name}"


def render_rich(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        if b["type"] == "p":
            out.append(f"<p>{escape(b['text'])}</p>")
        else:
            items = "".join(f"<li>{escape(i)}</li>" for i in b["items"])
            out.append(f"<{b['type']}>{items}</{b['type']}>")
    return "\n".join(out)


CSS = """
:root {
  --bg: #f6f5f4;
  --card-bg: #ffffff;
  --text: #1f1f1f;
  --muted: #6f6f6f;
  --border: #dcdcdc;
  --accent: #3584e4;
  --radius: 18px;
  --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
  --brand-light: %(brand_light)s;
  --brand-dark:  %(brand_dark)s;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 32px 16px 64px;
  font-family: -apple-system, "Inter", "Segoe UI", Cantarell, Ubuntu, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.55;
}
.container {
  max-width: 1100px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.card {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 28px 32px;
  box-shadow: var(--shadow);
}
h2 {
  margin: 0 0 16px;
  font-size: 1.3rem;
  font-weight: 700;
  letter-spacing: -0.01em;
}
h2.section-sub {
  margin-top: 24px;
}

/* Validation banner */
.banner {
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.82rem;
  padding: 12px 16px;
  border-radius: 10px;
  background: #e7f4ec;
  color: #205c2f;
  border: 1px solid #b5dec3;
  white-space: pre-wrap;
}
.banner.warn {
  background: #fff5d8;
  color: #6b4d0c;
  border-color: #f0d077;
}
.banner.fail {
  background: #fbeaea;
  color: #7c2828;
  border-color: #e6a1a1;
}

.lint-summary {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.lint-summary-ok {
  font-family: -apple-system, "Inter", "Segoe UI", sans-serif;
}
.lint-pill {
  display: inline-block;
  padding: 1px 10px;
  border-radius: 9999px;
  font-size: 0.78rem;
  font-weight: 500;
  font-family: -apple-system, "Inter", "Segoe UI", sans-serif;
}
.lint-pill.resolves-at-tag {
  background: #d2e8ff;
  color: #0b3a73;
}
.lint-pill.justify-in-pr {
  background: #ffe1c2;
  color: #6b3a05;
}
.lint-pill.attention {
  background: #f8c8c8;
  color: #6f1818;
}

.lint-issues {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.lint-issue {
  display: grid;
  grid-template-columns: minmax(0, 18em) 1fr;
  gap: 12px;
  font-family: -apple-system, "Inter", "Segoe UI", sans-serif;
  font-size: 0.8rem;
  align-items: baseline;
}
.lint-issue .lint-id {
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.78rem;
  background: rgba(0, 0, 0, 0.05);
  padding: 1px 8px;
  border-radius: 6px;
  word-break: break-word;
}
.lint-issue.justify-in-pr .lint-id { background: rgba(179, 95, 5, 0.15); }
.lint-issue.attention .lint-id { background: rgba(124, 24, 24, 0.15); }
.lint-issue .lint-expl {
  line-height: 1.45;
}

details.lint-raw {
  margin-top: 8px;
  font-family: -apple-system, "Inter", "Segoe UI", sans-serif;
  font-size: 0.78rem;
}
details.lint-raw summary {
  cursor: pointer;
  color: inherit;
  opacity: 0.7;
}
details.lint-raw[open] summary {
  margin-bottom: 6px;
}
details.lint-raw pre {
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.75rem;
  background: rgba(0, 0, 0, 0.04);
  padding: 8px 10px;
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}

/* Hero */
.hero {
  background: linear-gradient(135deg, var(--brand-light) 0%%, var(--card-bg) 70%%);
  display: flex;
  gap: 28px;
  align-items: center;
}
.hero img.icon {
  width: 144px;
  height: 144px;
  border-radius: 28px;
  flex-shrink: 0;
  background: var(--card-bg);
}
.hero h1 {
  margin: 0 0 6px;
  font-size: 2.6rem;
  font-weight: 800;
  letter-spacing: -0.02em;
}
.hero .summary {
  font-size: 1.3rem;
  margin: 0 0 12px;
  opacity: 0.85;
}
.hero .developer {
  font-size: 0.95rem;
  color: var(--muted);
}
.hero .developer strong { color: var(--accent); font-weight: 600; }

/* Metadata grid */
.metadata-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.metadata-grid .item {
  background: var(--bg);
  padding: 12px 14px;
  border-radius: 10px;
}
.metadata-grid .label {
  font-size: 0.72rem;
  text-transform: uppercase;
  color: var(--muted);
  letter-spacing: 0.05em;
  margin-bottom: 4px;
}
.metadata-grid .value {
  font-size: 0.95rem;
  font-weight: 600;
  word-break: break-word;
}

/* Screenshots */
.screenshots {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 20px;
}
.screenshot {
  background: var(--bg);
  border-radius: 12px;
  overflow: hidden;
  margin: 0;
}
.screenshot img {
  width: 100%%;
  height: auto;
  display: block;
}
.screenshot figcaption {
  padding: 12px 14px;
  font-size: 0.92rem;
}
.screenshot .badge {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 8px;
  font-size: 0.68rem;
  background: var(--accent);
  color: white;
  border-radius: 999px;
  text-transform: uppercase;
  vertical-align: middle;
}
.screenshot .remote-url {
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.72rem;
  color: var(--muted);
  word-break: break-all;
  display: block;
  margin-top: 4px;
}

/* Description */
.description p, .description ul, .description ol { margin: 0 0 12px; }
.description ul, .description ol { padding-left: 24px; }

/* Tag pills */
.tags { display: flex; flex-wrap: wrap; gap: 8px; }
.tag {
  background: var(--bg);
  border-radius: 999px;
  padding: 5px 12px;
  font-size: 0.85rem;
}

/* Link cards */
.links {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.links a {
  display: block;
  background: var(--bg);
  padding: 12px 16px;
  border-radius: 10px;
  text-decoration: none;
  color: var(--text);
}
.links a:hover { background: #ececec; }
.links a .label {
  display: block;
  color: var(--muted);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 4px;
}
.links a .url {
  font-size: 0.9rem;
  word-break: break-all;
  color: var(--accent);
}

/* Branding swatches */
.branding {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}
.swatch-info {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 14px;
  background: var(--bg);
  border-radius: 10px;
}
.swatch {
  width: 48px;
  height: 48px;
  border-radius: 10px;
  border: 1px solid var(--border);
  flex-shrink: 0;
}
.swatch-info .scheme {
  font-size: 0.72rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.swatch-info .hex {
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.95rem;
  font-weight: 600;
}
.swatch-info .palette-role {
  margin-top: 2px;
  font-size: 0.78rem;
  color: var(--muted);
}

/* Releases */
.release {
  border-left: 3px solid var(--accent);
  padding: 0 0 0 16px;
  margin-bottom: 24px;
}
.release:last-child { margin-bottom: 0; }
.release h3 {
  margin: 0 0 8px;
  font-size: 1.1rem;
}
.release .date {
  font-size: 0.85rem;
  color: var(--muted);
  font-weight: 400;
}
.release p, .release ul { margin: 0 0 8px; }
.release ul { padding-left: 24px; }

/* Banner stack */
.banners {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.banner strong { display: block; margin-bottom: 4px; font-family: -apple-system, "Inter", "Segoe UI", sans-serif; }

/* Key-value tables (desktop + manifest top fields) */
table.kv {
  width: 100%%;
  border-collapse: collapse;
  font-size: 0.92rem;
}
table.kv tr + tr th, table.kv tr + tr td {
  border-top: 1px solid var(--border);
}
table.kv th {
  text-align: left;
  font-weight: 600;
  color: var(--muted);
  padding: 8px 12px 8px 0;
  width: 30%%;
  vertical-align: top;
  white-space: nowrap;
}
table.kv td {
  padding: 8px 0;
  vertical-align: top;
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.88rem;
  word-break: break-word;
}

/* finish-args list */
.finish-args {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.finish-args li {
  background: var(--bg);
  padding: 8px 12px;
  border-radius: 8px;
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.finish-args code {
  font-size: 0.85rem;
  background: transparent;
}
.finish-args .note {
  color: var(--muted);
  font-size: 0.82rem;
  flex: 1;
}

/* Module + source pinning */
.module { margin-bottom: 16px; }
.module:last-child { margin-bottom: 0; }
.module h3 {
  font-size: 1rem;
  margin: 0 0 8px;
}
.module h3 .meta {
  font-weight: 400;
  color: var(--muted);
  font-size: 0.85rem;
}
dl.source {
  display: grid;
  grid-template-columns: 90px 1fr;
  gap: 4px 12px;
  background: var(--bg);
  padding: 10px 14px;
  border-radius: 8px;
  margin: 0;
}
dl.source dt {
  font-weight: 600;
  color: var(--muted);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding-top: 2px;
}
dl.source dd {
  margin: 0;
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.85rem;
  word-break: break-all;
}

/* License head excerpt */
.license-head {
  margin-top: 14px;
  padding: 12px 14px;
  background: var(--bg);
  border-radius: 10px;
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.82rem;
  color: var(--text);
}
.license-head div { padding: 1px 0; }

footer {
  text-align: center;
  font-size: 0.78rem;
  color: var(--muted);
  padding: 8px 16px;
}
footer code {
  background: var(--card-bg);
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
}
"""


def render(info: dict, desktop: dict, manifest: dict, dev_manifest: dict,
           license_info: dict,
           validations: list[tuple[str, str, int]]) -> str:
    brand_light = info["branding"][0]["value"] if info["branding"] else "#e6e6e6"
    brand_dark = info["branding"][1]["value"] if len(info["branding"]) > 1 else "#1c1c1c"
    css = CSS % {"brand_light": brand_light, "brand_dark": brand_dark}

    # Each banner: classify findings (resolves-at-tag / justify-in-pr /
    # unknown), summarise visibly, keep the raw lint output behind a <details>
    # so verbatim text is one click away when debugging.
    banner_rows = []
    for name, text, code in validations:
        overall, known, unknown = _classify_validation(name, text or "", code)

        if overall == "ok":
            cls = "ok"
        elif overall == "expected":
            cls = "warn"
        else:
            cls = "fail"

        if not known and not unknown:
            summary_html = (
                '<div class="lint-summary lint-summary-ok">Clean.</div>'
            )
        else:
            n_resolves = sum(1 for i in known if i["kind"] == "resolves-at-tag")
            n_justify = sum(1 for i in known if i["kind"] == "justify-in-pr")
            n_unknown = len(unknown)

            parts = []
            if n_resolves:
                parts.append(
                    f'<span class="lint-pill resolves-at-tag">'
                    f'{n_resolves} resolves at tag</span>'
                )
            if n_justify:
                parts.append(
                    f'<span class="lint-pill justify-in-pr">'
                    f'{n_justify} justify in PR</span>'
                )
            if n_unknown:
                parts.append(
                    f'<span class="lint-pill attention">'
                    f'{n_unknown} needs attention</span>'
                )

            issue_rows = []
            for issue in known:
                issue_rows.append(
                    f'<li class="lint-issue {issue["kind"]}">'
                    f'<code class="lint-id">{escape(issue["id"])}</code>'
                    f'<span class="lint-expl">{escape(issue["explanation"])}</span>'
                    f'</li>'
                )
            for ident in unknown:
                issue_rows.append(
                    f'<li class="lint-issue attention">'
                    f'<code class="lint-id">{escape(ident)}</code>'
                    f'<span class="lint-expl">Unknown lint — investigate, '
                    f'and add to LINT_CLASSIFICATIONS in tools/preview_flathub.py '
                    f'so future runs categorise it.</span>'
                    f'</li>'
                )

            summary_html = (
                f'<div class="lint-summary">{" ".join(parts)}</div>'
                f'<ul class="lint-issues">{"".join(issue_rows)}</ul>'
            )

        # Raw output is folded into a <details> so the banner stays scannable.
        raw_html = ""
        if text and text.strip():
            raw_html = (
                f'<details class="lint-raw"><summary>raw lint output</summary>'
                f'<pre>{escape(text)}</pre></details>'
            )

        banner_rows.append(
            f'<div class="banner {cls}">'
            f'<strong>{escape(name)}</strong>'
            f'{summary_html}'
            f'{raw_html}'
            f'</div>'
        )
    banners_html = "\n".join(banner_rows)

    # Description
    desc_html = render_rich(info["description"])

    # Screenshots
    shot_cards = []
    for s in info["screenshots"]:
        local = remote_to_local(s["url"])
        badge = ' <span class="badge">default</span>' if s["is_default"] else ""
        shot_cards.append(
            f'<figure class="screenshot">'
            f'<img src="{escape(local)}" alt="{escape(s["caption"])}">'
            f'<figcaption>{escape(s["caption"])}{badge}'
            f'<small class="remote-url">{escape(s["url"])}</small>'
            f'</figcaption>'
            f'</figure>'
        )
    shots_html = "\n".join(shot_cards)

    # URLs
    url_cards = "\n".join(
        f'<a href="{escape(u["href"])}" target="_blank" rel="noopener">'
        f'<span class="label">{escape((u["type"] or "").replace("_", " "))}</span>'
        f'<span class="url">{escape(u["href"])}</span></a>'
        for u in info["urls"]
    )

    # Tags
    cat_tags = "".join(f'<span class="tag">{escape(c)}</span>' for c in info["categories"])
    kw_tags = "".join(f'<span class="tag">{escape(k)}</span>' for k in info["keywords"])

    # Branding (AppStream-spec: only `type="primary"` with optional
    # light/dark scheme_preference, so max two entries here).
    branding_html = "\n".join(
        f'<div class="swatch-info">'
        f'<span class="swatch" style="background:{escape(c["value"])}"></span>'
        f'<div><div class="scheme">{escape(c["scheme_preference"] or "—")} scheme</div>'
        f'<div class="hex">{escape(c["value"])}</div></div></div>'
        for c in info["branding"]
    )

    # Full Flatpal theme palette — read from flatpal/palette.py so this
    # card stays in sync with the CSS provider that ships in the app.
    # AppStream's <branding> only carries the primary light/dark pair;
    # this block surfaces the rest (Mint Teal, Freeze Blue, …) that the
    # running app actually paints with.
    palette_html = "\n".join(
        f'<div class="swatch-info">'
        f'<span class="swatch" style="background:{escape(value)}"></span>'
        f'<div><div class="scheme">{escape(name)}</div>'
        f'<div class="hex">{escape(value)}</div>'
        f'<div class="palette-role">{escape(role)}</div></div></div>'
        for name, value, role in PALETTE_ENTRIES
    )

    # Releases
    release_blocks = []
    for r in info["releases"]:
        body = render_rich(r["description"])
        release_blocks.append(
            f'<div class="release">'
            f'<h3>{escape(r["version"] or "")} '
            f'<span class="date">— {escape(r["date"] or "")}</span></h3>'
            f'{body}'
            f'</div>'
        )
    releases_html = "\n".join(release_blocks)

    # Developer
    dev = info["developer"]
    dev_name = escape(dev.get("name") or "—")
    dev_id = escape(dev.get("id") or "")

    # Content rating
    cr = info["content_rating"]
    cr_type = cr.get("type") or "—"
    if cr.get("attributes"):
        cr_body = ", ".join(f"{a['id']}: {a['value']}" for a in cr["attributes"])
    else:
        cr_body = "no explicit attributes (implicit: no objectionable content)"

    # Metadata grid
    meta_rows = [
        ("App ID", info["id"]),
        ("Summary length", f'{len(info["summary"] or "")} chars'),
        ("License (project)", info["project_license"]),
        ("License (metadata)", info["metadata_license"]),
        ("Launchable", info["launchable"]),
    ]
    if info["releases"]:
        meta_rows.insert(2, ("Latest version", info["releases"][0]["version"]))
        meta_rows.insert(3, ("Released", info["releases"][0]["date"]))
    meta_grid = "\n".join(
        f'<div class="item"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value or "—")}</div></div>'
        for label, value in meta_rows
    )

    title = escape(info["name"] or "metainfo preview")

    # Desktop entry card — every key shown verbatim, with Categories /
    # Keywords lifted out as tag pills for parity with the metainfo card.
    if desktop:
        desktop_categories = [c for c in desktop.get("Categories", "").split(";") if c]
        desktop_keywords = [k for k in desktop.get("Keywords", "").split(";") if k]
        desktop_rows = "\n".join(
            f'<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>'
            for k, v in desktop.items()
        )
        desktop_cats = "".join(
            f'<span class="tag">{escape(c)}</span>' for c in desktop_categories
        )
        desktop_keys = "".join(
            f'<span class="tag">{escape(k)}</span>' for k in desktop_keywords
        )
        desktop_card = (
            f'<section class="card"><h2>Desktop entry</h2>'
            f'<table class="kv">{desktop_rows}</table>'
            + (f'<h2 class="section-sub">Categories</h2>'
               f'<div class="tags">{desktop_cats}</div>' if desktop_cats else "")
            + (f'<h2 class="section-sub">Keywords</h2>'
               f'<div class="tags">{desktop_keys}</div>' if desktop_keys else "")
            + f'</section>'
        )
    else:
        desktop_card = ""

    # Flatpak manifest card — split into top-level meta, finish-args (each
    # call-out with a small explanation pulled from the standard set), and
    # modules with their source pinning.
    if manifest:
        top_rows = []
        for key in ("app-id", "runtime", "runtime-version", "sdk", "command"):
            if key in manifest:
                top_rows.append(
                    f'<tr><th>{escape(key)}</th>'
                    f'<td>{escape(str(manifest[key]))}</td></tr>'
                )
        finish_args = manifest.get("finish-args", []) or []
        finish_items = "".join(
            f'<li><code>{escape(str(a))}</code> '
            f'<span class="note">{escape(_finish_note(a))}</span></li>'
            for a in finish_args
        )
        module_blocks = []
        for m in manifest.get("modules", []) or []:
            name = escape(str(m.get("name", "")))
            bs = escape(str(m.get("buildsystem", "—")))
            src_lines = []
            for s in m.get("sources", []) or []:
                src_lines.append(
                    "<dl class=\"source\">" +
                    "".join(
                        f'<dt>{escape(k)}</dt><dd><code>{escape(str(v))}</code></dd>'
                        for k, v in s.items()
                    ) +
                    "</dl>"
                )
            module_blocks.append(
                f'<div class="module"><h3>{name} '
                f'<span class="meta">({bs})</span></h3>'
                f'{"".join(src_lines)}</div>'
            )
        manifest_card = (
            f'<section class="card"><h2>Flatpak manifest</h2>'
            f'<table class="kv">{"".join(top_rows)}</table>'
            f'<h2 class="section-sub">finish-args ({len(finish_args)})</h2>'
            f'<ul class="finish-args">{finish_items}</ul>'
            f'<h2 class="section-sub">Modules ({len(manifest.get("modules") or [])})</h2>'
            f'{"".join(module_blocks)}</section>'
        )
    elif yaml is None:
        manifest_card = (
            '<section class="card"><h2>Flatpak manifest</h2>'
            '<p style="color:var(--muted);margin:0">'
            'PyYAML is not installed — install <code>python3-yaml</code> '
            '(Debian/Ubuntu) or <code>pip install pyyaml</code> to render '
            'this card.</p></section>'
        )
    else:
        manifest_card = ""

    # Dev-manifest card — same shape as the canonical manifest but tighter,
    # because the only thing that differs is the source override (type: dir
    # instead of type: git). Surfaced so the maintainer can sanity-check the
    # local-build setup at a glance.
    if dev_manifest:
        dev_top_rows = []
        for key in ("app-id", "runtime", "runtime-version", "sdk", "command"):
            if key in dev_manifest:
                dev_top_rows.append(
                    f'<tr><th>{escape(key)}</th>'
                    f'<td>{escape(str(dev_manifest[key]))}</td></tr>'
                )
        dev_module_blocks = []
        for m in dev_manifest.get("modules", []) or []:
            name = escape(str(m.get("name", "")))
            bs = escape(str(m.get("buildsystem", "—")))
            src_lines = []
            for s in m.get("sources", []) or []:
                src_lines.append(
                    "<dl class=\"source\">" +
                    "".join(
                        f'<dt>{escape(k)}</dt><dd><code>{escape(str(v))}</code></dd>'
                        for k, v in s.items()
                    ) +
                    "</dl>"
                )
            dev_module_blocks.append(
                f'<div class="module"><h3>{name} '
                f'<span class="meta">({bs})</span></h3>'
                f'{"".join(src_lines)}</div>'
            )
        dev_manifest_card = (
            f'<section class="card"><h2>Dev manifest '
            f'<span style="font-size:0.7em;color:var(--muted);font-weight:400">'
            f'(local-build override, not seen by Flathub)</span></h2>'
            f'<p style="margin:0 0 12px;color:var(--muted);font-size:0.9rem">'
            f'Used by <code>release-flatpal.sh</code> Mode 1 and §9 lint runs. '
            f'Same finish-args as the canonical manifest above; only the source '
            f'differs — <code>type: dir, path: .</code> so flatpak-builder copies '
            f'the working tree instead of cloning a tag.</p>'
            f'<table class="kv">{"".join(dev_top_rows)}</table>'
            f'<h2 class="section-sub">Modules ({len(dev_manifest.get("modules") or [])})</h2>'
            f'{"".join(dev_module_blocks)}</section>'
        )
    else:
        dev_manifest_card = ""

    # LICENSE card — title, line count, file size, head excerpt.
    if license_info:
        head_html = "".join(
            f'<div>{escape(line)}</div>' for line in license_info.get("head", [])
        )
        license_card = (
            f'<section class="card"><h2>License</h2>'
            f'<div class="metadata-grid">'
            f'<div class="item"><div class="label">SPDX</div>'
            f'<div class="value">{escape(info.get("project_license") or "—")}</div></div>'
            f'<div class="item"><div class="label">Title</div>'
            f'<div class="value">{escape(license_info["title"])}</div></div>'
            f'<div class="item"><div class="label">Lines</div>'
            f'<div class="value">{license_info["lines"]}</div></div>'
            f'<div class="item"><div class="label">Size</div>'
            f'<div class="value">{license_info["bytes"]:,} bytes</div></div>'
            f'<div class="item"><div class="label">Path</div>'
            f'<div class="value">{escape(license_info["path"])}</div></div>'
            f'</div>'
            f'<div class="license-head">{head_html}</div></section>'
        )
    else:
        license_card = ""

    return (
        "<!DOCTYPE html>\n"
        f'<html lang="en"><head><meta charset="utf-8">'
        f"<title>{title} — Flathub release preview</title>"
        f"<style>{css}</style></head><body>"
        f'<div class="container">'
        f'<div class="banners">{banners_html}</div>'
        f'<section class="card hero">'
        f'<img class="icon" src="icon.png" alt="{title}">'
        f'<div><h1>{title}</h1>'
        f'<p class="summary">{escape(info["summary"] or "")}</p>'
        f'<div class="developer">by <strong>{dev_name}</strong>'
        f' <span style="color:var(--muted)">({dev_id})</span></div>'
        f"</div></section>"
        f'<section class="card"><h2>Overview</h2>'
        f'<div class="metadata-grid">{meta_grid}</div></section>'
        f'<section class="card"><h2>Screenshots ({len(info["screenshots"])})</h2>'
        f'<div class="screenshots">{shots_html}</div></section>'
        f'<section class="card description"><h2>Description</h2>{desc_html}</section>'
        f'<section class="card"><h2>Links</h2>'
        f'<div class="links">{url_cards}</div></section>'
        f'<section class="card"><h2>Categories</h2>'
        f'<div class="tags">{cat_tags}</div>'
        f'<h2 class="section-sub">Keywords</h2>'
        f'<div class="tags">{kw_tags}</div>'
        f'<h2 class="section-sub">Content rating</h2>'
        f'<p style="margin:0;color:var(--muted)"><strong>{escape(cr_type)}</strong>'
        f' — {escape(cr_body)}</p>'
        f"</section>"
        f'<section class="card"><h2>Brand colors</h2>'
        f'<h2 class="section-sub">AppStream &lt;branding&gt; '
        f'<span style="font-weight:400;color:var(--muted);font-size:0.85rem">'
        f'— primary, light + dark variants</span></h2>'
        f'<div class="branding">{branding_html}</div>'
        f'<h2 class="section-sub">Theme palette '
        f'<span style="font-weight:400;color:var(--muted);font-size:0.85rem">'
        f'— flatpal/palette.py</span></h2>'
        f'<div class="branding">{palette_html}</div></section>'
        + (
            f'<section class="card"><h2>Releases ({len(info["releases"])})</h2>'
            f"{releases_html}</section>"
            if info["releases"]
            else
            f'<section class="card"><h2>Releases</h2>'
            f'<p style="margin:0;color:var(--muted)">No releases yet — the '
            f'<code>&lt;releases&gt;</code> block in the metainfo is empty. '
            f'<code>release-flatpal.sh</code> prepends a <code>&lt;release&gt;</code> '
            f'entry to the metainfo for each tagged release.</p></section>'
        )
        + f"{desktop_card}"
        + f"{manifest_card}"
        + f"{dev_manifest_card}"
        + f"{license_card}"
        + f"<footer>Generated from <code>{escape(METAINFO.name)}</code>, "
        + f"<code>{escape(DESKTOP.name)}</code>, "
        + f"<code>{escape(MANIFEST.name)}</code> + <code>{escape(DEV_MANIFEST.name)}</code>, "
        + f"and <code>LICENSE</code>. "
        + f"Refresh with <code>python3 tools/preview_flathub.py</code>.</footer>"
        + f"</div></body></html>"
    )


_FINISH_ARG_NOTES = {
    "--share=ipc": "X11 shared-memory + general IPC",
    "--share=network": "outbound HTTPS — popularity API, screenshot CDN",
    "--socket=wayland": "Wayland display",
    "--socket=fallback-x11": "X11 fallback for non-Wayland sessions",
    "--socket=x11": "X11 display",
    "--device=dri": "hardware-accelerated rendering via /dev/dri",
    "--device=all": "all host devices",
    "--filesystem=/var/lib/flatpak:ro": "host's system Flatpak install (icons / AppStream / metainfo, read-only)",
    "--filesystem=xdg-data/flatpak:ro": "host's user Flatpak install, read-only",
    "--filesystem=host": "entire host filesystem (avoid — reviewers push back)",
    "--talk-name=org.freedesktop.Flatpak": "host flatpak via flatpak-spawn --host",
    "--talk-name=org.gnome.Software": "GNOME Software for Open-in-Software action",
}


def _finish_note(arg: str) -> str:
    return _FINISH_ARG_NOTES.get(arg, "")


# ---------- runtime glue ----------

def run_validators() -> list[tuple[str, str, int]]:
    """Run every locally-available validator; return (label, output, rc) per file."""
    out: list[tuple[str, str, int]] = []

    # AppStream metainfo.
    try:
        r = subprocess.run(
            ["appstreamcli", "validate", str(METAINFO)],
            capture_output=True, text=True, timeout=60,
        )
        text = (r.stdout + r.stderr).strip() or "OK"
        out.append(("AppStream metainfo  (appstreamcli validate)", text, r.returncode))
    except FileNotFoundError:
        out.append(("AppStream metainfo", "appstreamcli not installed — skipping", -1))
    except subprocess.TimeoutExpired:
        out.append(("AppStream metainfo", "timed out (>60s — network reachability)", -1))

    # Desktop entry.
    try:
        r = subprocess.run(
            ["desktop-file-validate", str(DESKTOP)],
            capture_output=True, text=True, timeout=30,
        )
        text = (r.stdout + r.stderr).strip() or "OK"
        out.append(("Desktop entry  (desktop-file-validate)", text, r.returncode))
    except FileNotFoundError:
        out.append(("Desktop entry", "desktop-file-validate not installed — skipping", -1))

    # Flatpak manifest. flatpak-builder-lint lives inside org.flatpak.Builder
    # (`flatpak install --user flathub org.flatpak.Builder`). If installed,
    # we run the real lint; otherwise we surface the gap.
    builder_check = subprocess.run(
        ["flatpak", "--user", "info", "org.flatpak.Builder"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if builder_check.returncode == 0:
        try:
            r = subprocess.run(
                [
                    "flatpak", "run", "--user",
                    "--command=flatpak-builder-lint", "org.flatpak.Builder",
                    "manifest", str(MANIFEST),
                ],
                capture_output=True, text=True, timeout=60,
            )
            text = (r.stdout + r.stderr).strip() or "OK"
            out.append(
                ("Flatpak manifest  (flatpak-builder-lint manifest)", text, r.returncode)
            )
        except FileNotFoundError:
            out.append(("Flatpak manifest", "flatpak CLI not on PATH — skipping", -1))
        except subprocess.TimeoutExpired:
            out.append(("Flatpak manifest", "timed out (>60s)", -1))
    else:
        out.append((
            "Flatpak manifest",
            "org.flatpak.Builder not installed — run "
            "`flatpak install --user -y flathub org.flatpak.Builder` to enable "
            "the manifest lint here.",
            2,
        ))

    return out


def copy_assets() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if ICON_PNG.exists():
        shutil.copy(ICON_PNG, OUT_DIR / "icon.png")
    shot_dir = OUT_DIR / "screenshots"
    shot_dir.mkdir(exist_ok=True)
    for src in sorted(SCREENSHOT_DIR.glob("*.png")):
        shutil.copy(src, shot_dir / src.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-open", action="store_true",
                        help="write the preview but do not open a browser")
    args = parser.parse_args()

    if not METAINFO.exists():
        print(f"error: {METAINFO} not found", file=sys.stderr)
        return 1

    copy_assets()
    info = parse_metainfo(METAINFO)
    desktop = parse_desktop(DESKTOP)
    manifest = parse_manifest(MANIFEST)
    dev_manifest = parse_manifest(DEV_MANIFEST)
    license_info = license_summary(LICENSE_FILE)
    validations = run_validators()
    PREVIEW.write_text(
        render(info, desktop, manifest, dev_manifest, license_info, validations),
        encoding="utf-8",
    )

    rel = PREVIEW.relative_to(REPO)
    print(f"wrote {rel}")
    if not args.no_open:
        webbrowser.open(f"file://{PREVIEW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
