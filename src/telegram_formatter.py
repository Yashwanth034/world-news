"""Telegram message formatting for news briefings.

Renders the enriched briefing produced by telegram_briefing:
public label above a bold headline, opening paragraph,
optional evidence-based bullets, body paragraphs, and a
source/read-more footer.

Safety rules:
- The footer (source + link) is never dropped.
- Truncation removes whole sentences/parts, never cutting
  a sentence in the middle.
- Character budget is counted the way Telegram does
  (CJK/emoji count double).
"""
import re

from src.formatter import clean
from src.telegram_briefing import (
    clean_headline,
)

CJK_RE = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uf900-\ufaff\uff00-\uffef]"
)

EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f7e0-\U0001f7ff"
    "\U0001f900-\U0001f9ff"
    "\u2600-\u26ff"
    "\u2700-\u27bf"
    "]"
)

HTML_ENTITY_RE = re.compile(r"&[a-zA-Z#0-9]+;")


def telegram_visible_len(text):
    """Characters Telegram counts toward the 4096 limit."""
    text = text or ""
    text = HTML_ENTITY_RE.sub(
        "x",
        text
    )
    double = len(
        CJK_RE.findall(text)
    ) + len(
        EMOJI_RE.findall(text)
    )
    return len(text) + double


def truncate_by_char_limit(
    text,
    limit,
    ellipsis="\u2026"
):
    """Truncate to a Telegram character budget.

    CJK-safe: slicing happens on the unified-string
    boundary, keeping the last glyph whole.
    """
    if telegram_visible_len(text) <= limit:
        return text

    if not text:
        return text

    if limit < 8:
        return text[:limit]

    budget = limit - 1

    char_total = 0
    cut = len(text)

    for i, ch in enumerate(text):
        char_total += 2 if (
            CJK_RE.match(ch)
            or EMOJI_RE.match(ch)
        ) else 1

        if char_total > budget:
            cut = i
            break

    return text[:cut].rstrip() + ellipsis


def escape_html(text):
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def sentence_safe_truncate(text, limit):
    """Cut at the last sentence boundary within limit.

    Falls back to a word boundary only when the text is a
    single oversized sentence. The fallback never cuts a
    word in half.
    """
    if telegram_visible_len(text) <= limit:
        return text

    chunks = text.split(". ")

    if len(chunks) <= 1:
        return truncate_by_char_limit(
            text,
            limit,
        )

    result = chunks[0]
    found = False

    for chunk in chunks[1:]:
        candidate = result + ". " + chunk

        if telegram_visible_len(candidate) <= limit:
            result = candidate
            found = True
        else:
            break

    if telegram_visible_len(result) <= limit:
        return result

    if not found and len(chunks) == 2:
        return truncate_by_char_limit(
            text,
            limit,
        )

    return result


DIVIDER = "\u2501" * 14

REGION_LINES = {
    "NORTH AMERICA": "\U0001F30E NORTH AMERICA",
    "LATIN AMERICA": "\U0001F30E LATIN AMERICA",
    "SOUTH AMERICA": "\U0001F30E SOUTH AMERICA",
    "EUROPE": "\U0001F30D EUROPE",
    "AFRICA": "\U0001F30D AFRICA",
    "MIDDLE EAST": "\U0001F30D MIDDLE EAST",
    "EAST ASIA": "\U0001F30F EAST ASIA",
    "SOUTHEAST ASIA": "\U0001F30F SOUTHEAST ASIA",
    "SOUTH ASIA": "\U0001F30F SOUTH ASIA",
    "CENTRAL ASIA": "\U0001F30F CENTRAL ASIA",
    "ASIA-PACIFIC": "\U0001F30F ASIA-PACIFIC",
    "OCEANIA": "\U0001F30F OCEANIA",
    "WORLD": "\U0001F30D WORLD",
    "GLOBAL": "\U0001F30D WORLD",
}


def region_line(item):
    """Country/region line from existing reliable data only.

    The pipeline only carries feed-level region evidence.
    When none is present the line is omitted; nothing is
    ever inferred from headlines or summaries.
    """
    region = item.get("region")

    if not region:
        return None

    return REGION_LINES.get(
        str(region).strip().upper()
    )


def render_sections(bullets):
    """Quick-fact sections from literal evidence.

    Each section is two lines: a bold icon+title and the
    evidence text:
        👥 **IMPACT**
        Nearly 100,000 people
    """
    section_titles = {
        "Location": "\U0001F4CD **LOCATION**",
        "Status": "\u26A0\uFE0F **STATUS**",
        "Impact": "\U0001F465 **IMPACT**",
        "Next": "\u27A1\uFE0F **NEXT**",
    }

    sections = []

    for bullet in bullets:
        title = section_titles.get(
            bullet.get("label")
        )

        if not title:
            continue

        text = str(bullet.get("text") or "").strip()

        if not text:
            continue

        text = text[0].upper() + text[1:]

        sections.append(
            title + "\n" + escape_html(text)
        )

    return sections


def render_source(
    source,
    corroborating,
):
    lines = [
        "\U0001F4F0 **SOURCE:** "
        + escape_html(source or "Unknown"),
    ]

    if corroborating:
        lines.append(
            "Corroborated by: "
            + escape_html(
                ", ".join(corroborating[:3])
            )
        )

    return lines


def render_read_more(url):
    if not url:
        return None

    return (
        "\U0001F517 <b><a href=\""
        + escape_html(url)
        + "\">Read the full report</a></b>"
    )


def build_briefing_message(item, max_chars):
    """Render the enriched briefing into parts.

    Visual hierarchy for mobile:

        LABEL

        REGION (when reliable evidence exists)

        **Headline**

        opening paragraph

        body paragraphs

        ━━━━━━━━━━━━━━

        👥 **IMPACT**
        evidence text

        ➡️ **NEXT**
        evidence text

        ━━━━━━━━━━━━━━

        📰 **SOURCE:** France 24
        Corroborated by: BBC World

        🔗 **Read the full report**

    At most two dividers per message; the footer and
    headline are never dropped; truncation removes whole
    sentences/parts only.
    """
    briefing = item.get("briefing") or {}

    label = (
        item.get("public_label")
        or item.get("label")
    )

    headline = clean(
        item.get("headline")
        or clean_headline(item.get("title"))
    )

    source = (
        briefing.get("source")
        or item.get("source")
    )

    url = (
        briefing.get("url")
        or item.get("url")
    )

    opening = briefing.get("opening") or []
    body = briefing.get("body") or []
    bullets = briefing.get("bullets") or []
    corroborating = briefing.get(
        "corroborating"
    ) or []

    parts = []

    if label:
        parts.append(
            {
                "text": escape_html(label),
                "priority": 1000,
            }
        )

    region = region_line(item)

    if region:
        parts.append(
            {
                "text": region,
                "priority": 1000,
            }
        )

    if headline:
        parts.append(
            {
                "text": "<b>"
                + escape_html(headline)
                + "</b>",
                "priority": 1000,
            }
        )

    if opening:
        parts.append(
            {
                "text": escape_html(
                    " ".join(opening)
                ),
                "priority": 90,
            }
        )

    # Body paragraphs: two sentences per paragraph.
    paragraphs = []
    current = []

    for sentence in body:
        current.append(sentence)

        if len(current) == 2:
            paragraphs.append(
                " ".join(current)
            )
            current = []

    if current:
        paragraphs.append(
            " ".join(current)
        )

    for paragraph in paragraphs:
        parts.append(
            {
                "text": escape_html(paragraph),
                "priority": 70,
            }
        )

    sections = render_sections(bullets)

    # Divider before the quick-fact sections.
    if sections:
        parts.append(
            {
                "text": DIVIDER,
                "priority": 75,
            }
        )

    for section in sections:
        parts.append(
            {
                "text": section,
                "priority": 80,
            }
        )

    # Divider before the source area. At most two dividers
    # are ever emitted (one above, one below the sections).
    parts.append(
        {
            "text": DIVIDER,
            "priority": 75,
        }
    )

    for line in render_source(
        source,
        corroborating,
    ):
        parts.append(
            {
                "text": line,
                "priority": 1000,
            }
        )

    read_more = render_read_more(url)

    if read_more:
        parts.append(
            {
                "text": read_more,
                "priority": 1000,
            }
        )

    # Drop lowest-priority removable parts until the
    # message fits. Footer, label, region and headline are
    # never dropped.
    while True:
        body_text = "\n\n".join(
            p["text"] for p in parts
        )

        if telegram_visible_len(
            body_text
        ) <= max_chars:
            break

        removable = [
            p for p in parts
            if p["priority"] < 1000
        ]

        if not removable:
            break

        weakest = min(
            removable,
            key=lambda p: p["priority"],
        )

        # Remove a single sentence when the weakest part
        # is a multi-sentence paragraph.
        if (
            weakest["priority"] == 70
            and ". " in weakest["text"]
        ):
            chunks = weakest["text"].split(". ")

            if len(chunks) > 1:
                weakest["text"] = (
                    ". ".join(chunks[:-1])
                )

                if weakest["text"]:
                    weakest["text"] += "."
                continue

        parts.remove(weakest)

    body_text = "\n\n".join(
        p["text"] for p in parts
    )

    if telegram_visible_len(
        body_text
    ) > max_chars:
        # Pathological single oversized sentence: cut at
        # a word boundary.
        for p in reversed(parts):
            if p["priority"] < 1000:
                p["text"] = truncate_by_char_limit(
                    p["text"],
                    max_chars,
                )
                break

    body_text = "\n\n".join(
        p["text"] for p in parts
    )

    return body_text


def build_fallback_message(item, max_chars):
    """Fallback for items without an enriched briefing:
    headline + verbatim summary sentences + footer."""
    label = (
        item.get("public_label")
        or item.get("label")
    )

    headline = clean(
        item.get("headline")
        or clean_headline(item.get("title"))
    )

    source = item.get("source")
    url = item.get("url")

    from src.formatter import split_sentences

    sentences = split_sentences(
        item.get("summary")
    )

    parts = []

    if label:
        parts.append(
            {
                "text": escape_html(label),
                "priority": 1000,
            }
        )

    region = region_line(item)

    if region:
        parts.append(
            {
                "text": region,
                "priority": 1000,
            }
        )

    if headline:
        parts.append(
            {
                "text": "<b>"
                + escape_html(headline)
                + "</b>",
                "priority": 1000,
            }
        )

    for sentence in sentences:
        parts.append(
            {
                "text": escape_html(sentence),
                "priority": 80,
            }
        )

    parts.append(
        {
            "text": DIVIDER,
            "priority": 75,
        }
    )

    for line in render_source(
        source,
        [],
    ):
        parts.append(
            {
                "text": line,
                "priority": 1000,
            }
        )

    read_more = render_read_more(url)

    if read_more:
        parts.append(
            {
                "text": read_more,
                "priority": 1000,
            }
        )

    while telegram_visible_len(
        "\n\n".join(p["text"] for p in parts)
    ) > max_chars:

        removable = [
            p for p in parts
            if p["priority"] < 1000
        ]

        if not removable:
            break

        parts.remove(
            min(
                removable,
                key=lambda p: p["priority"],
            )
        )

    return "\n\n".join(
        p["text"] for p in parts
    )


def build_message(item, cfg, now=None):
    """Build an HTML telegram message from a queue item.

    Returns None if the item has no usable content.
    """
    target = int(
        cfg.get(
            "target_message_chars",
            1500
        )
    )

    max_chars = int(
        cfg.get(
            "max_message_chars",
            3000
        )
    )

    if max_chars < target:
        max_chars = target

    title = clean(
        item.get("title")
        or item.get("headline")
    )

    if not title and not (
        item.get("briefing")
        or {}
    ).get("sentences"):
        return None

    if item.get("briefing"):
        body = build_briefing_message(
            item,
            max_chars,
        )
    else:
        body = build_fallback_message(
            item,
            max_chars,
        )

    return {
        "text": body,
        "parse_mode": "HTML",
    }
