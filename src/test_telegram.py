"""Unit tests for the telegram modules.

Run with:  .venv/bin/python -m pytest src/test_telegram.py -q
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

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
    label="\U0001F4F0 NEWS",
    headline="Canada wildfire doubles in size",
    opening=None,
    body=None,
    bullets=None,
    source="BBC World",
    corroborating=None,
    url="https://www.bbc.co.uk/news/articles/abc123",
    title="State of emergency declared as fast-moving "
    "Canada wildfire doubles in size",
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
    bullets = bullets or [
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
        "\U0001F6A8 BREAKING",
        "\u26A1 JUST IN",
        "\U0001F4F0 NEWS",
        "\U0001F504 UPDATE",
    ):
        msg = build_message(briefing_item(label=label), CFG)
        assert label in msg["text"]
        position = msg["text"].index(label)
        headline_pos = msg["text"].index(
            "<b>"
        )
        assert position < headline_pos


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


def test_bullets_rendered_with_labels():
    msg = build_message(briefing_item(), CFG)
    assert (
        "\u2022 <b>\U0001F4CD Location:</b> "
        "British Columbia"
        in msg["text"]
    )


def test_source_footer_attribution():
    msg = build_message(briefing_item(), CFG)
    assert (
        "\U0001F4F0 <b>Source:</b> BBC World"
        in msg["text"]
    )


def test_corroboration_listed_in_footer():
    msg = build_message(
        briefing_item(
            corroborating=["Al Jazeera"],
        ),
        CFG,
    )
    assert "Corroborated by Al Jazeera" in msg["text"]


def test_read_more_link_clickable():
    msg = build_message(briefing_item(), CFG)
    assert (
        '<a href="https://www.bbc.co.uk/news/articles/'
        'abc123">Read the full report</a>'
        in msg["text"]
    )


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
