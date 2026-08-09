"""Unit tests for the telegram modules.

Run with:  .venv/bin/python -m pytest src/test_telegram.py -q
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.telegram_briefing import (
    BREAKING,
    JUST_IN,
    NEWS,
    UPDATE,
)
from src.telegram_formatter import (
    build_message,
    telegram_visible_len,
    truncate_by_char_limit,
)
from src.telegram_scheduler import (
    filter_candidates,
    is_fresh,
    maybe_important,
    now_utc,
    publish_due,
    story_age_minutes,
)

CFG = {
    "target_message_chars": 1500,
    "max_message_chars": 3000,
}

NOW = datetime.now(timezone.utc)


def item(**overrides):
    base = {
        "story_id": "s" + str(
            abs(
                hash(
                    json.dumps(overrides)
                )
            ) % 100000
        ),
        "item_id": "i1",
        "event_id": "e1",
        "title": "Test story",
        "summary": "A summary sentence. Second sentence.",
        "source": "Example News",
        "url": "https://example.com/story",
        "label": "analysis",
        "priority_level": "NORMAL",
        "priority_score": 40,
        "effective_at": now_utc().isoformat(),
        "published_at": "2026-08-01T10:00:00Z",
    }
    base.update(overrides)
    return base


def briefing_item(
    label=NEWS,
    headline="Canada wildfire doubles in size",
    opening=None,
    body=None,
    bullets=None,
    source="BBC World",
    corroborating=None,
    url="https://www.bbc.co.uk/news/articles/abc123",
    title="State of emergency declared as fast-moving "
    "Canada wildfire doubles in size",
    region=None,
):
    opening = opening or [
        "The Bald Range wildfire in British Columbia, "
        "still considered out of control, has spread "
        "over more than 36 sq miles."
    ]
    body = body or [
        "A fast-moving wildfire has forced more than "
        "20,000 people to evacuate.",
    ]
    if bullets is None:
        bullets = [
            {
                "icon": "\U0001F4CD",
                "label": "Location",
                "text": "British Columbia",
            },
        ]
    return item(
        label=label,
        public_label=label,
        headline=headline,
        title=title,
        summary=opening[0],
        region=region,
        briefing={
            "opening": opening,
            "body": body,
            "bullets": bullets,
            "sentences": [
                {"text": t, "source": source}
                for t in opening + body
            ],
            "source": source,
            "corroborating": corroborating or [],
            "url": url,
        },
    )


def fresh_item(minutes_ago=5, **overrides):
    effective = (
        now_utc() - timedelta(minutes=minutes_ago)
    ).isoformat()
    return item(
        effective_at=effective,
        **overrides
    )


def make_state(**overrides):
    state = {
        "posted": [],
        "scheduled": [],
        "failures": [],
        "last_posted_at": None,
    }
    state.update(overrides)
    return state


def fake_publisher(dry_run_default=False):
    class FakePublisher:
        def __init__(self):
            self.sent = []
            self.fail_next = 0
            self.rate_limit = None

        def send_message(
            self,
            chat_id,
            message,
            dry_run=False,
        ):
            if dry_run or dry_run_default:
                return {
                    "dry_run": True,
                    "chat_id": chat_id,
                }

            if self.fail_next > 0:
                self.fail_next -= 1
                from src.telegram_publisher import (
                    TelegramPublisherError,
                )
                raise TelegramPublisherError(
                    "simulated failure"
                )

            if self.rate_limit:
                from src.telegram_publisher import (
                    TelegramRateLimited,
                )
                raise TelegramRateLimited(
                    self.rate_limit,
                    "simulated rate limit",
                )

            self.sent.append(chat_id)
            return {
                "message_id": len(self.sent),
                "chat_id": chat_id,
            }

    return FakePublisher()


def dt_to_iso(dt):
    return dt.isoformat()


def scheduled_entry(
    story_id,
    scheduled_at,
    item_id="i1",
    event_id="e1",
    label="analysis",
):
    return {
        "story_id": story_id,
        "item_id": item_id,
        "event_id": event_id,
        "label": label,
        "scheduled_at": dt_to_iso(scheduled_at),
        "attempts": 0,
    }


# ---------------------------------------------------------
# story_age_minutes / is_fresh
# ---------------------------------------------------------


def test_story_age_minutes_fresh():
    x = fresh_item(minutes_ago=10)
    assert story_age_minutes(x) == pytest.approx(
        10.0,
        abs=1,
    )


def test_story_age_minutes_missing():
    assert story_age_minutes(item(effective_at=None)) is None


def test_story_age_minutes_future_skew():
    x = fresh_item(minutes_ago=-5)
    assert story_age_minutes(x) == 0.0


def test_story_age_minutes_huge_future():
    x = fresh_item(minutes_ago=-60)
    assert story_age_minutes(x) is None


def test_is_fresh_within_window():
    x = fresh_item(minutes_ago=30)
    assert is_fresh(x, 6)
    assert not is_fresh(x, 0.25)


# ---------------------------------------------------------
# maybe_important
# ---------------------------------------------------------


def test_breaking_immediate_is_important():
    x = fresh_item(
        priority_level="IMMEDIATE",
        priority_score=95,
    )
    assert maybe_important(x, 15, True)
    assert not maybe_important(x, 15, False)


def test_high_priority_is_important():
    x = fresh_item(priority_level="HIGH")
    assert maybe_important(x, 15, True)


def test_just_in_is_important():
    x = fresh_item(minutes_ago=5)
    assert maybe_important(x, 15, True)
    assert not maybe_important(x, 2, True)


# ---------------------------------------------------------
# filter_candidates
# ---------------------------------------------------------


def test_filter_candidates_skips_posted_and_scheduled():
    x1 = fresh_item(
        story_id="post-me",
        title="Posted story",
    )
    x2 = fresh_item(
        story_id="sched-me",
        title="Scheduled story",
    )
    x3 = fresh_item(
        story_id="fresh-me",
        title="Fresh story",
    )
    state = make_state(
        posted=[
            {"story_id": "post-me"},
        ],
        scheduled=[
            {"story_id": "sched-me"},
        ],
    )
    got = filter_candidates(
        [x1, x2, x3],
        state,
        6,
        50,
        15,
        True,
    )
    ids = [x["story_id"] for x in got]
    assert "post-me" not in ids
    assert "sched-me" not in ids
    assert "fresh-me" in ids


def test_filter_candidates_skips_old_stories():
    x = fresh_item(minutes_ago=420)
    got = filter_candidates(
        [x],
        make_state(),
        6,
        50,
        15,
        True,
    )
    assert got == []


def test_filter_candidates_sorts_important_first():
    normal = fresh_item(
        story_id="normal",
        priority_level="NORMAL",
        priority_score=30,
        minutes_ago=30,
    )
    breaking = fresh_item(
        story_id="breaking",
        priority_level="IMMEDIATE",
        priority_score=95,
        minutes_ago=10,
    )
    got = filter_candidates(
        [normal, breaking],
        make_state(),
        6,
        50,
        15,
        True,
    )
    assert got[0]["story_id"] == "breaking"


def test_filter_candidates_respects_max():
    items = [
        fresh_item(
            story_id="s%d" % i,
            minutes_ago=1,
        )
        for i in range(10)
    ]
    got = filter_candidates(
        items,
        make_state(),
        6,
        3,
        15,
        True,
    )
    assert len(got) == 3


def test_filter_candidates_blocks_failed_item_id():
    x = fresh_item(
        story_id="story-a",
        item_id="same-item",
    )
    state = make_state(
        failures=[
            {
                "story_id": "story-b",
                "item_id": "same-item",
            }
        ]
    )
    got = filter_candidates(
        [x],
        state,
        6,
        50,
        15,
        True,
    )
    assert got == []


# ---------------------------------------------------------
# publish_due
# ---------------------------------------------------------


def test_publish_due_success():
    x = fresh_item(
        story_id="due-story",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[
            scheduled_entry(
                "due-story",
                now_utc() - timedelta(minutes=5),
            )
        ]
    )
    publisher = fake_publisher()
    report = publish_due(
        publisher,
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert len(report["published"]) == 1
    assert report["published"][0]["story_id"] == "due-story"
    assert state["scheduled"] == []
    assert len(state["posted"]) == 1
    assert state["last_posted_at"]


def test_publish_due_future_not_published():
    x = fresh_item(
        story_id="future-story",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[
            scheduled_entry(
                "future-story",
                now_utc() + timedelta(hours=1),
            )
        ]
    )
    publisher = fake_publisher()
    report = publish_due(
        publisher,
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report["due"] == 0
    assert report["published"] == []
    assert len(state["scheduled"]) == 1


def test_publish_due_no_chat():
    x = fresh_item(
        story_id="nochat-story",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[
            scheduled_entry(
                "nochat-story",
                now_utc() - timedelta(minutes=5),
            )
        ]
    )
    report = publish_due(
        fake_publisher(),
        "",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report["published"] == []
    assert len(state["scheduled"]) == 1


def test_publish_due_hourly_cap():
    x = fresh_item(
        story_id="capped-story",
        minutes_ago=10,
    )
    posted = [
        {
            "story_id": "p%d" % i,
            "posted_at": (
                now_utc() - timedelta(minutes=i)
            ).isoformat(),
        }
        for i in range(20)
    ]
    state = make_state(
        scheduled=[
            scheduled_entry(
                "capped-story",
                now_utc() - timedelta(minutes=5),
            )
        ],
        posted=posted,
    )
    report = publish_due(
        fake_publisher(),
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report["published"] == []
    assert len(report["skipped_cap"]) == 1
    assert len(state["scheduled"]) == 1


def test_publish_due_expired_story():
    x = fresh_item(
        story_id="expired-story",
        minutes_ago=420,
    )
    state = make_state(
        scheduled=[
            scheduled_entry(
                "expired-story",
                now_utc() - timedelta(minutes=5),
            )
        ]
    )
    report = publish_due(
        fake_publisher(),
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report["published"] == []
    assert len(report["expired"]) == 1
    assert state["scheduled"] == []


def test_publish_due_retry_then_fail():
    x = fresh_item(
        story_id="flaky-story",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[
            scheduled_entry(
                "flaky-story",
                now_utc() - timedelta(minutes=5),
            )
        ]
    )
    publisher = fake_publisher()
    publisher.fail_next = 2

    report = publish_due(
        publisher,
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report["published"] == []
    assert len(report["failed"]) == 1
    assert state["scheduled"][0]["attempts"] == 1

    report2 = publish_due(
        publisher,
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report2["published"] == []
    assert len(report2["failed"]) == 1
    assert state["scheduled"] == []
    assert len(state["failures"]) == 1
    assert state["failures"][0]["story_id"] == "flaky-story"


def test_publish_due_dry_run_keeps_schedule():
    x = fresh_item(
        story_id="dry-story",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[
            scheduled_entry(
                "dry-story",
                now_utc() - timedelta(minutes=5),
            )
        ]
    )
    report = publish_due(
        fake_publisher(),
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
        dry_run=True,
    )
    assert report["published"]
    assert report["published"][0]["dry_run"]
    assert len(state["scheduled"]) == 1
    assert state["posted"] == []


def test_publish_due_min_gap():
    x = fresh_item(
        story_id="gap-story",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[
            scheduled_entry(
                "gap-story",
                now_utc() - timedelta(minutes=5),
            )
        ],
        last_posted_at=(
            now_utc() - timedelta(seconds=10)
        ).isoformat(),
        posted=[
            {
                "story_id": "recent",
                "posted_at": (
                    now_utc() - timedelta(seconds=10)
                ).isoformat(),
            }
        ],
    )
    report = publish_due(
        fake_publisher(),
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report["published"] == []
    assert len(report["skipped_gap"]) == 1
    assert len(state["scheduled"]) == 1


# ---------------------------------------------------------
# telegram_visible_len / truncate
# ---------------------------------------------------------


def test_visible_len_ascii():
    assert telegram_visible_len("hello world") == 11


def test_visible_len_cjk():
    assert telegram_visible_len("\u4e16\u754c") == 4


def test_truncate_cjk_keeps_whole_glyphs():
    text = "\u4e16" * 500
    cut = truncate_by_char_limit(text, 200)
    assert telegram_visible_len(cut) <= 200
    assert cut.rstrip("\u2026") == "\u4e16" * 99


def test_truncate_short_text_unchanged():
    text = "short"
    assert truncate_by_char_limit(text, 100) == text


def test_truncate_appends_ellipsis():
    text = "a" * 100
    cut = truncate_by_char_limit(text, 50)
    assert cut.endswith("\u2026")
    assert telegram_visible_len(cut) <= 50


# ---------------------------------------------------------
# build_message - briefing rendering
# ---------------------------------------------------------


def test_briefing_label_rendered_above_headline():
    for label in (
        BREAKING,
        JUST_IN,
        NEWS,
        UPDATE,
    ):
        msg = build_message(briefing_item(label=label), CFG)
        assert label in msg["text"]
        assert msg["text"].startswith(label + "\n")
        position = msg["text"].index(label)
        headline_pos = msg["text"].index(
            "<b>"
        )
        assert position < headline_pos


def test_breaking_visual_label():
    assert BREAKING == "\U0001F534 BREAKING"
    msg = build_message(
        briefing_item(label=BREAKING),
        CFG,
    )
    assert msg["text"].startswith(
        "\U0001F534 BREAKING\n"
    )


def test_just_in_visual_label():
    assert JUST_IN == "\U0001F7E1 JUST IN"
    msg = build_message(
        briefing_item(label=JUST_IN),
        CFG,
    )
    assert msg["text"].startswith(
        "\U0001F7E1 JUST IN\n"
    )


def test_news_visual_label():
    assert NEWS == "\U0001F535 NEWS"
    msg = build_message(
        briefing_item(label=NEWS),
        CFG,
    )
    assert msg["text"].startswith(
        "\U0001F535 NEWS\n"
    )


def test_update_visual_label():
    assert UPDATE == "\U0001F7E0 UPDATE"
    msg = build_message(
        briefing_item(label=UPDATE),
        CFG,
    )
    assert msg["text"].startswith(
        "\U0001F7E0 UPDATE\n"
    )


def test_region_line_rendered_when_evidence_exists():
    msg = build_message(
        briefing_item(
            region="East Asia",
        ),
        CFG,
    )
    assert "\U0001F30F EAST ASIA" in msg["text"]
    assert (
        msg["text"].index("\U0001F30F EAST ASIA")
        > msg["text"].index(NEWS)
    )
    assert (
        msg["text"].index("\U0001F30F EAST ASIA")
        < msg["text"].index("<b>")
    )


def test_region_line_omitted_without_evidence():
    msg = build_message(
        briefing_item(region=None),
        CFG,
    )
    for region in (
        "CHINA",
        "UNITED STATES",
        "ASIA",
        "\U0001F30D",
        "\U0001F30E",
        "\U0001F30F",
    ):
        assert region not in msg["text"]


def test_unknown_region_never_guessed():
    msg = build_message(
        briefing_item(region="Atlantis"),
        CFG,
    )
    assert "Atlantis" not in msg["text"]


def test_headline_is_bold():
    msg = build_message(briefing_item(), CFG)
    assert (
        "<b>Canada wildfire doubles in size</b>"
        in msg["text"]
    )


def test_opening_rendered_as_paragraph():
    msg = build_message(
        briefing_item(
            opening=[
                "First opening sentence here.",
                "Second opening sentence here.",
            ]
        ),
        CFG,
    )
    assert (
        "First opening sentence here. "
        "Second opening sentence here."
        in msg["text"]
    )


def test_paragraph_spacing_uses_blank_lines():
    msg = build_message(
        briefing_item(
            region="Africa",
            opening=[
                "The opening paragraph sentence.",
            ],
            body=[
                "The body paragraph sentence.",
            ],
        ),
        CFG,
    )
    text = msg["text"]
    assert NEWS + "\n\n" in text
    assert "\U0001F30D AFRICA\n\n" in text
    assert "</b>\n\n" in text
    assert "The opening paragraph sentence.\n\n" in text
    assert "The body paragraph sentence.\n\n" in text


def test_impact_section_rendered():
    msg = build_message(
        briefing_item(
            bullets=[
                {
                    "icon": "\U0001F465",
                    "label": "Impact",
                    "text": "nearly 100,000 people",
                },
            ]
        ),
        CFG,
    )
    assert "\U0001F465 **IMPACT**" in msg["text"]
    assert (
        "**IMPACT**\nNearly 100,000 people"
        in msg["text"]
    )


def test_status_section_rendered():
    msg = build_message(
        briefing_item(
            bullets=[
                {
                    "icon": "\u26A0\uFE0F",
                    "label": "Status",
                    "text": "out of control",
                },
            ]
        ),
        CFG,
    )
    assert "\u26A0\uFE0F **STATUS**" in msg["text"]
    assert (
        "**STATUS**\nOut of control"
        in msg["text"]
    )


def test_next_section_rendered():
    msg = build_message(
        briefing_item(
            bullets=[
                {
                    "icon": "\u27A1\uFE0F",
                    "label": "Next",
                    "text": "The storm is expected to "
                    "weaken by morning.",
                },
            ]
        ),
        CFG,
    )
    assert "\u27A1\uFE0F **NEXT**" in msg["text"]
    assert (
        "**NEXT**\nThe storm is expected to "
        "weaken by morning."
        in msg["text"]
    )


def test_location_section_rendered():
    msg = build_message(briefing_item(), CFG)
    assert "\U0001F4CD **LOCATION**" in msg["text"]
    assert (
        "**LOCATION**\nBritish Columbia"
        in msg["text"]
    )


def test_only_supported_sections_rendered():
    msg = build_message(
        briefing_item(
            bullets=[
                {
                    "icon": "\u2022",
                    "label": "Mystery",
                    "text": "unsupported section",
                },
            ]
        ),
        CFG,
    )
    assert "Mystery" not in msg["text"]
    assert "unsupported section" not in msg["text"]


def test_divider_placement_with_sections():
    msg = build_message(
        briefing_item(
            bullets=[
                {
                    "icon": "\U0001F465",
                    "label": "Impact",
                    "text": "nearly 100,000 people",
                },
                {
                    "icon": "\U0001F4CD",
                    "label": "Location",
                    "text": "British Columbia",
                },
            ]
        ),
        CFG,
    )
    text = msg["text"]
    dividers = text.count("\u2501" * 14)
    assert dividers == 2
    assert dividers <= 2
    assert (
        text.index("</b>\n\n") < text.index(
            "\u2501" * 14
        )
    )
    assert (
        text.index("\u2501" * 14)
        < text.index("\U0001F465 **IMPACT**")
    )
    assert (
        text.index("\U0001F465 **IMPACT**")
        < text.index("\u2501" * 14, text.index(
            "\U0001F465 **IMPACT**"
        ))
    )
    assert (
        text.rindex("\u2501" * 14)
        < text.index("\U0001F4F0 **SOURCE:**")
    )


def test_single_divider_without_sections():
    msg = build_message(
        briefing_item(bullets=[]),
        CFG,
    )
    assert msg["text"].count("\u2501" * 14) == 1
    text = msg["text"]
    assert (
        text.index("</b>\n\n") < text.index(
            "\u2501" * 14
        )
    )
    assert (
        text.index("\u2501" * 14)
        < text.index("\U0001F4F0 **SOURCE:**")
    )


def test_source_footer_attribution():
    msg = build_message(briefing_item(), CFG)
    assert (
        "\U0001F4F0 **SOURCE:** BBC World"
        in msg["text"]
    )


def test_corroboration_listed_in_footer():
    msg = build_message(
        briefing_item(
            corroborating=["Al Jazeera"],
        ),
        CFG,
    )
    assert (
        "\U0001F4F0 **SOURCE:** BBC World"
        in msg["text"]
    )
    assert "Corroborated by: Al Jazeera" in msg["text"]


def test_read_more_link_clickable():
    msg = build_message(briefing_item(), CFG)
    assert (
        '<a href="https://www.bbc.co.uk/news/articles/'
        'abc123">Read the full report</a>'
        in msg["text"]
    )
    assert "\U0001F517" in msg["text"]


def test_no_unsupported_html_or_color():
    msg = build_message(briefing_item(), CFG)
    text = msg["text"].lower()
    for forbidden in (
        "<font",
        "color=",
        "style=",
        "css",
        "<div",
        "<span",
        "<script",
        "class=",
        "markdown",
    ):
        assert forbidden not in text


def test_five_plus_sentences_when_information_exists():
    opening = [
        "Sentence one describes the event.",
        "Sentence two adds location details.",
    ]
    body = [
        "Sentence three reports the official response.",
        "Sentence four describes the current situation.",
        "Sentence five reports the impact on residents.",
        "Sentence six explains what happens next.",
    ]
    msg = build_message(
        briefing_item(opening=opening, body=body),
        CFG,
    )
    count = len(
        [
            s
            for s in opening + body
            if s in msg["text"]
        ]
    )
    assert count >= 5


def test_short_source_fallback_no_filler():
    x = item(
        summary="Only one useful sentence.",
    )
    msg = build_message(x, CFG)
    assert "Only one useful sentence." in msg["text"]
    assert "Sentence two" not in msg["text"]


def test_no_invented_facts():
    opening = ["The river rose one metre overnight."]
    body = ["Residents were told to move to higher ground."]
    source_sentences = opening + body
    msg = build_message(
        briefing_item(opening=opening, body=body),
        CFG,
    )
    stripped = msg["text"].replace("\u2026", "")
    for sentence in source_sentences:
        assert sentence in stripped
    assert "two metre" not in stripped


def test_3000_character_maximum():
    body = [
        "This is a complete sentence number %d in the "
        "briefing body paragraph." % i
        for i in range(300)
    ]
    msg = build_message(
        briefing_item(opening=body[:1], body=body[1:]),
        CFG,
    )
    assert telegram_visible_len(msg["text"]) <= 3000


def test_sentence_safe_truncation():
    body = [
        "First complete sentence of the long briefing.",
        "Second complete sentence of the long briefing.",
        "Third complete sentence of the long briefing.",
    ]
    msg = build_message(
        briefing_item(opening=body[:1], body=body[1:]),
        CFG,
    )
    for sentence in body:
        if sentence in msg["text"]:
            assert msg["text"].index(sentence) >= 0


def test_duplicate_sentence_protection_in_rendered_message():
    from src.telegram_briefing import build_briefing

    next_sentence = (
        "The storm is expected to make landfall "
        "late Sunday or early Monday."
    )
    x = item(
        title="Storm approaches the coast",
        summary=(
            "The storm strengthened overnight. "
            "Flights were cancelled across the region. "
            + next_sentence
        ),
    )
    briefing = build_briefing(x, [x], 15, NOW)
    en = dict(
        x,
        public_label=briefing["label"],
        headline=briefing["headline"],
        briefing=briefing,
    )
    msg = build_message(en, CFG)
    assert "\u27A1\uFE0F **NEXT**" in msg["text"]
    assert msg["text"].count(next_sentence) == 1


def test_no_internal_values_in_output():
    x = briefing_item(
        title="Internal fields story",
        headline="Internal fields story",
        opening=["The summary sentence is clean."],
    )
    x["score"] = 95
    x["confidence"] = "high"
    x["priority_level"] = "IMMEDIATE"
    x["priority_score"] = 100
    x["tier"] = 1
    x["quality_pass"] = True
    x["max_delay_minutes"] = 30
    x["urgency_score"] = 0.9
    msg = build_message(x, CFG)
    for forbidden in (
        "score",
        "confidence",
        "priority_level",
        "priority_score",
        "IMMEDIATE",
        "tier",
        "quality",
        "max_delay",
        "urgency",
        "HIGH",
        "MEDIUM",
        "LOW",
        "95",
        "100",
    ):
        assert forbidden not in msg["text"]


def test_build_message_escapes_html():
    x = item(
        title='A <b>bold</b> claim & "quotes"',
        headline='A <b>bold</b> claim & "quotes"',
    )
    msg = build_message(x, CFG)
    assert "<b>bold</b>" not in msg["text"]
    assert "&lt;b&gt;" in msg["text"]
    assert "&amp;" in msg["text"]


def test_build_message_without_summary():
    x = item(summary="")
    msg = build_message(x, CFG)
    assert msg is not None
    assert "Test story" in msg["text"]


def test_build_message_empty_item():
    assert build_message({}, CFG) is None


def test_build_message_parse_mode():
    msg = build_message(briefing_item(), CFG)
    assert msg["parse_mode"] == "HTML"


def _workflow_path(name):
    return (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "workflows"
        / name
    )


def test_workflow_uses_main_branch():
    text = _workflow_path("telegram.yml").read_text()
    assert "master" not in text
    assert "ref: main" in text
    assert "python -m src.main" in text
    assert "python -m src.telegram_run --yes" in text
    assert "git push origin main" in text
    assert "git checkout origin/main" not in text


def test_workflow_write_permission():
    text = _workflow_path("telegram.yml").read_text()
    assert "permissions:" in text
    assert "contents: write" in text


def test_workflow_shared_concurrency_group():
    for name in (
        "telegram.yml",
        "telegram-test-one.yml",
    ):
        text = _workflow_path(name).read_text()
        assert "group: telegram-publish" in text
        assert "cancel-in-progress: false" in text


def test_posted_history_retained_at_48h_boundary():
    state = make_state(
        scheduled=[],
        posted=[
            {
                "story_id": "kept-48h",
                "posted_at": (
                    now_utc()
                    - timedelta(hours=48)
                ).isoformat(),
            },
        ],
    )
    publish_due(
        fake_publisher(),
        "@channel",
        state,
        [],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert len(state["posted"]) == 1
    assert state["posted"][0]["story_id"] == "kept-48h"


def test_posted_history_retained_50h_still_blocks():
    story = fresh_item(
        story_id="dup-story",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[],
        posted=[
            {
                "story_id": "dup-story",
                "posted_at": (
                    now_utc()
                    - timedelta(hours=50)
                ).isoformat(),
            },
        ],
    )
    publish_due(
        fake_publisher(),
        "@channel",
        state,
        [story],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    chosen = filter_candidates(
        [story],
        state,
        6,
        50,
        15,
        True,
    )
    assert chosen == []
    assert state["posted"][0]["story_id"] == "dup-story"


def test_posted_history_pruned_only_when_safely_older():
    state = make_state(
        scheduled=[],
        posted=[
            {
                "story_id": "kept-50h",
                "posted_at": (
                    now_utc()
                    - timedelta(hours=50)
                ).isoformat(),
            },
            {
                "story_id": "pruned-70h",
                "posted_at": (
                    now_utc()
                    - timedelta(hours=70)
                ).isoformat(),
            },
        ],
    )
    publish_due(
        fake_publisher(),
        "@channel",
        state,
        [],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    ids = [
        e["story_id"] for e in state["posted"]
    ]
    assert ids == ["kept-50h"]


def test_posted_history_unknown_timestamp_kept():
    state = make_state(
        scheduled=[],
        posted=[
            {"story_id": "no-timestamp"},
            {
                "story_id": "bad-timestamp",
                "posted_at": "not-a-date",
            },
            {
                "story_id": "pruned-old",
                "posted_at": (
                    now_utc()
                    - timedelta(hours=200)
                ).isoformat(),
            },
        ],
    )
    publish_due(
        fake_publisher(),
        "@channel",
        state,
        [],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    ids = {
        e["story_id"] for e in state["posted"]
    }
    assert "no-timestamp" in ids
    assert "bad-timestamp" in ids
    assert "pruned-old" not in ids


def test_duplicate_protection_after_48h_memory_boundary():
    story = fresh_item(
        story_id="dup-boundary",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[],
        posted=[
            {
                "story_id": "dup-boundary",
                "posted_at": (
                    now_utc()
                    - timedelta(hours=48)
                ).isoformat(),
            },
        ],
    )
    chosen = filter_candidates(
        [story],
        state,
        6,
        50,
        15,
        True,
    )
    assert chosen == []


def test_publish_due_daily_cap():
    x = fresh_item(
        story_id="daily-capped",
        minutes_ago=10,
    )
    posted = [
        {
            "story_id": "d%d" % i,
            "posted_at": (
                now_utc()
                - timedelta(minutes=61 + i)
            ).isoformat(),
        }
        for i in range(150)
    ]
    state = make_state(
        scheduled=[
            scheduled_entry(
                "daily-capped",
                now_utc()
                - timedelta(minutes=5),
            )
        ],
        posted=posted,
    )
    report = publish_due(
        fake_publisher(),
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report["published"] == []
    assert len(report["skipped_cap"]) == 1
    assert (
        report["skipped_cap"][0]["reason"]
        == "daily cap reached"
    )
    assert (
        state["scheduled"][0]["story_id"]
        == "daily-capped"
    )


def test_publish_due_rate_limited_deferral():
    x = fresh_item(
        story_id="rate-story",
        minutes_ago=10,
    )
    state = make_state(
        scheduled=[
            scheduled_entry(
                "rate-story",
                now_utc()
                - timedelta(minutes=5),
            )
        ]
    )
    publisher = fake_publisher()
    publisher.rate_limit = 30

    report = publish_due(
        publisher,
        "@channel",
        state,
        [x],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
    )
    assert report["published"] == []
    assert report["rate_limited"] == 30
    assert len(report["failed"]) == 1
    assert report["failed"][0]["retry_after"] == 30
    assert (
        state["scheduled"][0]["story_id"]
        == "rate-story"
    )
    assert state["scheduled"][0]["attempts"] == 0


def test_rate_limit_wait_bounded():
    from src.telegram_run import (
        RATE_LIMIT_BUDGET_SECONDS,
        RATE_LIMIT_MAX_WAIT_SECONDS,
        rate_limit_wait_seconds,
    )

    assert rate_limit_wait_seconds(30) == 30
    assert (
        rate_limit_wait_seconds(10 ** 6)
        == RATE_LIMIT_MAX_WAIT_SECONDS
    )
    assert (
        rate_limit_wait_seconds(30)
        <= RATE_LIMIT_BUDGET_SECONDS
    )
    assert rate_limit_wait_seconds(-5) == 0
    assert rate_limit_wait_seconds(None) == 0
    assert rate_limit_wait_seconds("nope") == 0
    assert (
        rate_limit_wait_seconds(500, budget=120)
        == RATE_LIMIT_MAX_WAIT_SECONDS
    )
    assert (
        rate_limit_wait_seconds(
            500,
            max_wait=1000,
            budget=120,
        )
        == 120
    )
    assert (
        rate_limit_wait_seconds(5, budget=2) == 2
    )


def test_state_save_load_roundtrip_and_atomic(
    tmp_path,
):
    from src.telegram_publisher import (
        load_state,
        save_state,
    )

    state_file = tmp_path / "telegram_state.json"
    posted_at = now_utc().isoformat()
    data = {
        "posted": [
            {
                "story_id": "s1",
                "posted_at": posted_at,
            }
        ],
        "scheduled": [],
        "failures": [],
    }

    save_state(str(state_file), data)

    assert state_file.exists()
    assert not (
        tmp_path / "telegram_state.json.tmp"
    ).exists()

    loaded = load_state(str(state_file))
    assert loaded["posted"][0]["story_id"] == "s1"
    assert (
        loaded["posted"][0]["posted_at"]
        == posted_at
    )


def test_publish_due_same_call_two_entries_enforce_gap():
    a = fresh_item(
        story_id="same-call-a",
        minutes_ago=10,
    )
    b = fresh_item(
        story_id="same-call-b",
        minutes_ago=10,
    )
    now = now_utc()
    state = make_state(
        scheduled=[
            scheduled_entry(
                "same-call-a",
                now - timedelta(minutes=5),
            ),
            scheduled_entry(
                "same-call-b",
                now - timedelta(minutes=4),
            ),
        ]
    )
    publisher = fake_publisher()

    report = publish_due(
        publisher,
        "@channel",
        state,
        [a, b],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
        now=now,
    )

    assert len(report["published"]) == 1
    assert (
        report["published"][0]["story_id"]
        == "same-call-a"
    )
    assert len(report["skipped_gap"]) == 1
    assert (
        report["skipped_gap"][0]["story_id"]
        == "same-call-b"
    )
    assert len(publisher.sent) == 1
    assert len(state["posted"]) == 1
    assert {
        e["story_id"]
        for e in state["scheduled"]
    } == {"same-call-b"}

    later = now + timedelta(minutes=15)
    report2 = publish_due(
        fake_publisher(),
        "@channel",
        state,
        [a, b],
        6,
        20,
        150,
        60,
        2,
        cfg=CFG,
        now=later,
    )
    assert len(report2["published"]) == 1
    assert (
        report2["published"][0]["story_id"]
        == "same-call-b"
    )
    assert state["scheduled"] == []
