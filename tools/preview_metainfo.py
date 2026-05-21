#!/usr/bin/env python3
"""Render the AppStream metainfo as a Flathub-style preview page.

Reads data/io.github.hawwwran.flatpal.metainfo.xml, copies the icon and
screenshots into temp/build/, writes temp/build/preview.html, and opens
that file in the default browser. Re-run any time the metainfo changes.

Usage:
    python3 tools/preview_metainfo.py [--no-open]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import webbrowser
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
METAINFO = DATA / "io.github.hawwwran.flatpal.metainfo.xml"
DESKTOP = DATA / "io.github.hawwwran.flatpal.desktop"
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


def render(info: dict, validation: tuple[str, int]) -> str:
    brand_light = info["branding"][0]["value"] if info["branding"] else "#e6e6e6"
    brand_dark = info["branding"][1]["value"] if len(info["branding"]) > 1 else "#1c1c1c"
    css = CSS % {"brand_light": brand_light, "brand_dark": brand_dark}

    val_text, val_code = validation
    val_class = "ok"
    if val_code == 0:
        val_class = "ok"
    elif val_code in (2, 3):
        val_class = "warn"
    else:
        val_class = "fail"

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

    # Branding
    branding_html = "\n".join(
        f'<div class="swatch-info">'
        f'<span class="swatch" style="background:{escape(c["value"])}"></span>'
        f'<div><div class="scheme">{escape(c["scheme_preference"] or "—")} scheme</div>'
        f'<div class="hex">{escape(c["value"])}</div></div></div>'
        for c in info["branding"]
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

    return (
        "<!DOCTYPE html>\n"
        f'<html lang="en"><head><meta charset="utf-8">'
        f"<title>{title} — Flathub preview</title>"
        f"<style>{css}</style></head><body>"
        f'<div class="container">'
        f'<div class="banner {val_class}">{escape(val_text)}</div>'
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
        f'<section class="card"><h2>Branding colors</h2>'
        f'<div class="branding">{branding_html}</div></section>'
        f'<section class="card"><h2>Releases ({len(info["releases"])})</h2>'
        f"{releases_html}</section>"
        f"<footer>Generated from <code>{escape(METAINFO.name)}</code>."
        f" Refresh with <code>python3 tools/preview_metainfo.py</code>.</footer>"
        f"</div></body></html>"
    )


# ---------- runtime glue ----------

def validate(path: Path) -> tuple[str, int]:
    try:
        r = subprocess.run(
            ["appstreamcli", "validate", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        out = (r.stdout + r.stderr).strip() or "OK"
        return out, r.returncode
    except FileNotFoundError:
        return "appstreamcli not installed — skipping validation", -1
    except subprocess.TimeoutExpired:
        return "appstreamcli timed out (network reachability checks > 60s)", -1


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
    validation = validate(METAINFO)
    PREVIEW.write_text(render(info, validation), encoding="utf-8")

    rel = PREVIEW.relative_to(REPO)
    print(f"wrote {rel}")
    if not args.no_open:
        webbrowser.open(f"file://{PREVIEW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
