"""AppStream metainfo loader and parser.

Pure logic, no GTK. Tested via fixture XML files under tests/fixtures/.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

_log = logging.getLogger("flatpal.metainfo")

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

URL_KEYS = ("homepage", "bugtracker", "donation", "help", "vcs-browser", "contribute", "contact")

# Env vars consulted in priority order — same chain GNU gettext uses. We can't
# rely on `locale.getlocale(LC_MESSAGES)` because Python initialises the locale
# to "C" until `setlocale(LC_ALL, "")` is called, which Flatpal never does;
# without this helper every install ran with `lang=None`, so any metainfo whose
# untagged baseline wasn't English (a common upstream bug) ended up displayed
# in the upstream's local language even on en_US systems.
_LANG_ENV_VARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


def system_lang() -> Optional[str]:
    """Return the user's preferred locale code (e.g. 'en_US', 'cs') or None.

    Iterates `LANGUAGE → LC_ALL → LC_MESSAGES → LANG` and returns the first
    set value that isn't `C`/`POSIX`. `LANGUAGE` may hold a colon-separated
    preference list ("cs:en"); we take the first entry. Encoding suffixes
    ("en_US.UTF-8") and modifiers ("de_DE@euro") are stripped so the result
    matches the shape of AppStream `xml:lang`.

    NB: GNU gettext's actual rule is more nuanced — it consults `LANGUAGE`
    only when one of `LC_ALL`/`LC_MESSAGES`/`LANG` resolves to a non-`C`
    locale (i.e. translation is enabled). Here `LANGUAGE` always wins when
    set, which matches what most users intend ("`LANGUAGE=cs` should pick
    Czech metainfo even if `LC_ALL=C`") and avoids surprising the user with
    a falsely-English detail page on systems whose `LC_*` are stuck at `C`
    by a sandbox quirk.
    """
    for var in _LANG_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if not value or value == "C" or value == "POSIX":
            continue
        first = value.split(":", 1)[0]
        first = first.split(".", 1)[0]
        first = first.split("@", 1)[0]
        _log.debug("system_lang picked %r from %s=%r", first, var, value)
        return first or None
    _log.debug("system_lang: no usable env var, returning None")
    return None

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
    best_text, best_score, best_lang = None, -1, None
    candidates: list[tuple[Optional[str], int]] = []
    for el in parent.findall(tag):
        node_lang = el.attrib.get(XML_LANG)
        score = _lang_score(node_lang, lang)
        candidates.append((node_lang, score))
        if score > best_score and el.text:
            best_text = el.text.strip()
            best_score = score
            best_lang = node_lang
    if candidates and _log.isEnabledFor(logging.DEBUG):
        _log.debug(
            "_pick_localised tag=%s wanted=%r → chose lang=%r (score=%d) from %s",
            tag, lang, best_lang, best_score, candidates,
        )
    return best_text


def _pick_localised_element(
    parent: ET.Element, tag: str, lang: Optional[str],
) -> Optional[ET.Element]:
    """Return the best-scoring child element with `tag` (or None).

    Unlike `_pick_localised` (which returns text), this returns the actual
    Element so callers can still walk its children. AppStream sometimes
    emits one whole `<description xml:lang="cs">` block per language at the
    top level (alongside an untagged English one) — without picking among
    them first, `parent.find(tag)` always returned source-order index 0,
    which for the Flathub aggregated catalog frequently isn't English.

    Empty candidates (no children and no text) are skipped during scoring
    so a more-specific-but-empty `<description xml:lang="cs"/>` can't beat
    an untagged-English variant that actually has content. As a last
    resort, if every candidate is empty we still return the first one
    encountered so the caller's `is not None` checks continue to work.
    """
    best_el, best_score, best_lang = None, -1, None
    candidates: list[tuple[Optional[str], int]] = []
    fallback: Optional[ET.Element] = None
    for el in parent.findall(tag):
        if fallback is None:
            fallback = el
        node_lang = el.attrib.get(XML_LANG)
        score = _lang_score(node_lang, lang)
        candidates.append((node_lang, score))
        if not list(el) and not (el.text or "").strip():
            continue
        if score > best_score:
            best_el = el
            best_score = score
            best_lang = node_lang
    if candidates and _log.isEnabledFor(logging.DEBUG):
        _log.debug(
            "_pick_localised_element tag=%s wanted=%r → chose lang=%r (score=%d) from %s",
            tag, lang, best_lang, best_score, candidates,
        )
    return best_el if best_el is not None else fallback


def _pick_localised_blocks(
    elements: list, lang: Optional[str],
) -> list:
    """Group `elements` by (tag, position-within-lang) and keep the variant
    with the best xml:lang score for each group.

    Two metainfo authoring styles are common:

    a) One block per language: `<ul>` + `<ul xml:lang="cs">` + …
    b) One block with interleaved children: a single `<ul>` containing
       `<li>`, `<li xml:lang="cs">`, `<li xml:lang="zh">`, … for each item.

    Both reduce to the same algorithm: bucket each element by (tag, rank-
    within-its-own-lang), then pick the best-scoring lang per bucket. The
    Resources metainfo uses style (b) for its feature list, which is why
    callers need to apply this helper inside `<ul>`/`<ol>` as well as at
    the top level of `<description>` — otherwise the `<li>` translations
    all render side-by-side.

    Empty candidates (no children and no stripped text) are skipped during
    scoring so a more-specific-but-empty `<p xml:lang="cs"/>` can't beat a
    populated untagged-English variant for that same bucket. If every
    candidate for a bucket is empty, the first one encountered is returned
    so the bucket isn't dropped entirely.

    Returns the chosen elements in their first-encountered (display) order.
    """
    counters: dict[Optional[str], dict[str, int]] = {}
    blocks: list[tuple[str, int, Optional[str], ET.Element]] = []
    for el in elements:
        node_lang = el.attrib.get(XML_LANG)
        c = counters.setdefault(node_lang, {})
        idx = c.get(el.tag, 0)
        c[el.tag] = idx + 1
        blocks.append((el.tag, idx, node_lang, el))

    chosen: dict[tuple[str, int], ET.Element] = {}
    chosen_scores: dict[tuple[str, int], int] = {}
    chosen_order: dict[tuple[str, int], int] = {}
    # Last-resort placeholder if every candidate for a bucket is empty.
    empty_fallback: dict[tuple[str, int], ET.Element] = {}
    order = 0
    for tag, idx, node_lang, el in blocks:
        key = (tag, idx)
        if key not in chosen_order:
            chosen_order[key] = order
            order += 1
        is_empty = not list(el) and not (el.text or "").strip()
        if is_empty:
            empty_fallback.setdefault(key, el)
            continue
        s = _lang_score(node_lang, lang)
        if s > chosen_scores.get(key, -1):
            chosen[key] = el
            chosen_scores[key] = s
    for key, el in empty_fallback.items():
        chosen.setdefault(key, el)
    return [
        el for _, el in sorted(chosen.items(), key=lambda kv: chosen_order[kv[0]])
    ]


def _description_markup(desc: Optional[ET.Element], lang: Optional[str]) -> str:
    """Convert AppStream <description> to Pango-friendly markup.

    Picks the best-matching language for each `<p>`/`<ul>`/`<ol>` child AND
    for each `<li>` inside a list. Pango supports `<b>`/`<i>`/`<tt>`/`<a>`
    but not `<p>`/`<ul>`; render `<p>` as paragraphs separated by blank
    lines and `<ul>`/`<ol>` items as bullets.
    """
    if desc is None:
        return ""

    if _log.isEnabledFor(logging.DEBUG):
        seen = [
            (el.tag, el.attrib.get(XML_LANG))
            for el in list(desc)
        ]
        _log.debug("_description_markup wanted=%r children=%s", lang, seen)

    parts: list[str] = []
    for el in _pick_localised_blocks(list(desc), lang):
        if el.tag == "p" and el.text:
            parts.append(_normalise_whitespace(el.text))
        elif el.tag in ("ul", "ol"):
            # Collapse bullets into a single block: items inside one list
            # are tight (`\n`), only the list-as-a-whole gets the `\n\n`
            # separator that distinguishes blocks. Otherwise each `<li>` gets
            # the same vertical breathing room as a `<p>` paragraph, and a
            # 6-bullet feature list (e.g. Resources) eats half the screen.
            bullets = [
                f"• {_normalise_whitespace(li.text)}"
                for li in _pick_localised_blocks(list(el.findall("li")), lang)
                if li.text
            ]
            if bullets:
                parts.append("\n".join(bullets))
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
        desc = _pick_localised_element(r, "description", lang)
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
        "description_markup": _description_markup(
            _pick_localised_element(root, "description", lang), lang,
        ),
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
    _log.info("load_metainfo app_id=%s lang=%r path=%s", app_id, lang, path)
    if path is None:
        return _empty_result()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return _empty_result()
    return parse_metainfo(text, lang)
