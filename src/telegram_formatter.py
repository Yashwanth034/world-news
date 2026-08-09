"""Telegram message formatting with CJK-aware truncation.

The scheduler uses `target_message_chars` as its content
budget. This module provides the character-counting helpers
that keep CJK text from being truncated mid-glyph.
"""
import re

from src.formatter import clean, clean_sentence, split_sentences

CJK_RE = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uf900-\ufaff\uff00-\uffef]"
)

EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f900-\U0001f9ff"
    "\u2600-\u26ff"
    "\u2700-\u27bf"
    "]"
)

HTML_ENTITY_RE = re.compile(r"&[a-zA-Z#0-9]+;")


def telegram_visible_len(text):
    """Characters Telegram counts toward the 4096 limit.

    Telegram counts most characters as 1 and CJK/emoji as 2.
    HTML entities are sent raw and therefore count as the
    entity text itself.
    """
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

    CJK-safe: the last glyph is kept whole because the
    budget is compared in Telegram char units, and slicing
    happens on the unified-string boundary.
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


def source_credit(item):
    parts = []
    source = item.get("source")

    if source:
        parts.append(source)

    published = item.get(
        "published_at"
    ) or item.get(
        "published"
    )

    if published:
        try:
            parts.append(
                published[:10]
            )
        except Exception:
            parts.append(published)

    return " \u00b7 ".join(parts)


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

    title = clean(item.get("title"))

    if not title:
        return None

    summary = clean(
        item.get("summary")
        or item.get("description")
    )

    label = clean(
        item.get("label")
    )

    source_text = source_credit(item)
    url = item.get("url")

    summary_part = None

    if summary:

        sentences = split_sentences(summary)

        if sentences:

            budget = max(
                120,
                target
                - telegram_visible_len(title)
                - len(" \u2014 ")
                - telegram_visible_len(label or "")
                - telegram_visible_len(source_text)
                - 2
            )

            summary_part = sentences[0]

            for extra in sentences[1:]:

                if telegram_visible_len(
                    summary_part
                    + " "
                    + extra
                ) <= budget:
                    summary_part = (
                        summary_part + " " + extra
                    )
                else:
                    break

            summary_part = truncate_by_char_limit(
                summary_part,
                budget
            )

    parts = [escape_html(title)]

    if label:
        parts.append(
            "<i>"
            + escape_html(label)
            + "</i>"
        )

    if summary_part:
        parts.append(
            escape_html(summary_part)
        )

    if source_text:
        parts.append(
            escape_html(source_text)
        )

    if url:
        parts.append(
            '<a href="'
            + escape_html(url)
            + '">read more</a>'
        )

    body = "\n".join(parts)
    body = truncate_by_char_limit(body, max_chars)

    return {
        "text": body,
        "parse_mode": "HTML",
    }
