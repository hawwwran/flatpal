"""AppStream metainfo loader and parser.

Pure logic, no GTK. Tested via fixture XML files under tests/fixtures/.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

URL_KEYS = ("homepage", "bugtracker", "donation", "help", "vcs-browser", "contribute", "contact")

# Collapse internal whitespace runs in `<p>`/`<li>` text. AppStream metainfo
# frequently wraps paragraph content across multiple indented source lines
# (see GIMP / Audacity fixtures) — without normalising, Gtk.Label preserves
# the source newlines and the continuation lines look indented inward.
_WHITESPACE_RUN = re.compile(r"\s+")


def _normalise_whitespace(text: str) -> str:
    return _WHITESPACE_RUN.sub(" ", text).strip()


def find_metainfo_path(app_id: str) -> Optional[Path]:
    """Locate metainfo XML for a deployed flatpak app, system or user install."""
    bases = [
        Path("/var/lib/flatpak/app") / app_id,
        Path(os.path.expanduser("~/.local/share/flatpak/app")) / app_id,
    ]
    relatives = [
        Path("current/active/files/share/metainfo") / f"{app_id}.metainfo.xml",
        Path("current/active/files/share/appdata") / f"{app_id}.appdata.xml",
        Path("current/active/files/share/metainfo") / f"{app_id}.appdata.xml",
    ]
    for base in bases:
        for rel in relatives:
            candidate = base / rel
            if candidate.is_file():
                return candidate
    return None


def _lang_score(node_lang: Optional[str], wanted: Optional[str]) -> int:
    """Higher = better match. 0 = no match. Untagged English = baseline 1."""
    if node_lang is None:
        return 1  # baseline: untagged is canonical English
    if wanted is None:
        return 0
    if node_lang == wanted:
        return 100
    base_wanted = wanted.split("_")[0].split("-")[0].lower()
    base_node = node_lang.split("_")[0].split("-")[0].lower()
    if base_wanted == base_node:
        return 50
    return 0


def _pick_localised(parent: ET.Element, tag: str, lang: Optional[str]) -> Optional[str]:
    best_text, best_score = None, -1
    for el in parent.findall(tag):
        score = _lang_score(el.attrib.get(XML_LANG), lang)
        if score > best_score and el.text:
            best_text = el.text.strip()
            best_score = score
    return best_text


def _description_markup(desc: Optional[ET.Element], lang: Optional[str]) -> str:
    """Convert AppStream <description> to Pango-friendly markup.

    Picks the best-matching language for each <p>/<ul>/<ol> child by scoring
    its xml:lang. Pango supports <b>/<i>/<tt>/<a> but not <p>/<ul>; render
    <p> as paragraphs separated by blank lines and <ul>/<ol> items as bullets.
    """
    if desc is None:
        return ""

    def best_text(children, tag):
        best, score = None, -1
        for c in children:
            if c.tag != tag:
                continue
            s = _lang_score(c.attrib.get(XML_LANG), lang)
            if s > score:
                best, score = c, s
        return best if best is not None else None

    # Group by tag-and-rough-position. AppStream description children are a flat list
    # like [p, p, ul, p, ...]. We render in order, but for each "logical block" we
    # pick the localised variant if multiple are present in sequence.
    # Simpler approach: render every untagged-or-best-lang block.

    # Build a list of (index, element) where index identifies a logical block by its
    # rank among same-tag siblings of the same xml:lang.
    parts: list[str] = []

    seen_keys: set[tuple[str, int]] = set()
    block_indices: dict[tuple[str, Optional[str]], int] = {}

    # First pass: pick best xml:lang for each (tag, block-position) pair
    # by tracking positions within each lang.
    counters: dict[Optional[str], dict[str, int]] = {}
    blocks: list[tuple[str, int, Optional[str], ET.Element]] = []
    for el in desc:
        tag = el.tag
        node_lang = el.attrib.get(XML_LANG)
        c = counters.setdefault(node_lang, {})
        idx = c.get(tag, 0)
        c[tag] = idx + 1
        blocks.append((tag, idx, node_lang, el))

    # For each (tag, idx) pair pick the best-scoring lang variant.
    chosen: dict[tuple[str, int], ET.Element] = {}
    chosen_scores: dict[tuple[str, int], int] = {}
    chosen_order: dict[tuple[str, int], int] = {}
    order = 0
    for tag, idx, node_lang, el in blocks:
        key = (tag, idx)
        s = _lang_score(node_lang, lang)
        if s > chosen_scores.get(key, -1):
            chosen[key] = el
            chosen_scores[key] = s
            if key not in chosen_order:
                chosen_order[key] = order
                order += 1

    ordered = sorted(chosen.items(), key=lambda kv: chosen_order[kv[0]])
    for (tag, _idx), el in ordered:
        if tag == "p" and el.text:
            parts.append(_normalise_whitespace(el.text))
        elif tag in ("ul", "ol"):
            for li in el.findall("li"):
                if li.text:
                    parts.append(f"• {_normalise_whitespace(li.text)}")

    return "\n\n".join(p for p in parts if p)


def _developer_name(root: ET.Element, lang: Optional[str]) -> Optional[str]:
    # New form: <developer id="..."><name>...</name></developer>
    dev = root.find("developer")
    if dev is not None:
        name = _pick_localised(dev, "name", lang)
        if name:
            return name
    # Legacy form: <developer_name>...</developer_name>
    return _pick_localised(root, "developer_name", lang)


def _screenshots(root: ET.Element) -> list:
    out = []
    ss = root.find("screenshots")
    if ss is None:
        return out
    for shot in ss.findall("screenshot"):
        # Prefer image with type="source", fall back to first <image>.
        chosen_url = None
        for img in shot.findall("image"):
            if img.attrib.get("type") == "source" and img.text:
                chosen_url = img.text.strip()
                break
        if chosen_url is None:
            for img in shot.findall("image"):
                if img.text:
                    chosen_url = img.text.strip()
                    break
        if not chosen_url:
            continue
        # Caption: prefer untagged (English baseline); we don't localise captions
        # for now — they're typically descriptive enough in English.
        caption_el = shot.find("caption")
        caption = (caption_el.text or "").strip() if caption_el is not None else ""
        out.append({
            "source_url": chosen_url,
            "caption": caption,
            "default": shot.attrib.get("type") == "default",
        })
    return out


def _urls(root: ET.Element) -> dict:
    out = {}
    for url in root.findall("url"):
        kind = url.attrib.get("type")
        if kind and url.text and kind not in out:
            out[kind] = url.text.strip()
    return out


def _categories(root: ET.Element) -> list:
    cats = root.find("categories")
    if cats is None:
        return []
    return [c.text.strip() for c in cats.findall("category") if c.text]


def _releases(root: ET.Element, lang: Optional[str], limit: int = 5) -> list:
    rel = root.find("releases")
    if rel is None:
        return []
    out = []
    for r in rel.findall("release"):
        version = r.attrib.get("version", "").strip()
        date = r.attrib.get("date", "").strip()
        desc = r.find("description")
        markup = _description_markup(desc, lang) if desc is not None else ""
        out.append({"version": version, "date": date, "description_markup": markup})
        if len(out) >= limit:
            break
    return out


def parse_component(root: ET.Element, lang: Optional[str] = None) -> dict:
    """Build the metainfo dict from a single `<component>` element.

    Used by `parse_metainfo` (single-app metainfo file) and `catalog.parse_catalog`
    (Flathub aggregated catalog, which is `<components>` containing many of these).
    """
    return {
        "id": (root.findtext("id") or "").strip(),
        "name": _pick_localised(root, "name", lang) or "",
        "summary": _pick_localised(root, "summary", lang) or "",
        "description_markup": _description_markup(root.find("description"), lang),
        "developer_name": _developer_name(root, lang),
        "project_license": (root.findtext("project_license") or "").strip() or None,
        "categories": _categories(root),
        "urls": _urls(root),
        "screenshots": _screenshots(root),
        "releases": _releases(root, lang),
    }


def parse_metainfo(xml_text: str, lang: Optional[str] = None) -> dict:
    """Parse AppStream metainfo XML into a flat dict.

    `lang` is a posix locale string like 'cs_CZ' — used to prefer matching
    xml:lang variants of `<summary>`, `<description>`, `<developer_name>`,
    `<developer><name>`. Untagged elements (English) are the fallback.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _empty_result()
    return parse_component(root, lang)


def _empty_result() -> dict:
    return {
        "id": "",
        "name": "",
        "summary": "",
        "description_markup": "",
        "developer_name": None,
        "project_license": None,
        "categories": [],
        "urls": {},
        "screenshots": [],
        "releases": [],
    }


def load_metainfo(app_id: str, lang: Optional[str] = None) -> dict:
    """Read + parse metainfo for an installed app. Returns empty dict if missing.

    Catches both filesystem (`OSError`) and decoding errors
    (`UnicodeDecodeError`/`ValueError`) — a corrupt metainfo file should
    degrade the detail page, not crash it.
    """
    path = find_metainfo_path(app_id)
    if path is None:
        return _empty_result()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return _empty_result()
    return parse_metainfo(text, lang)
