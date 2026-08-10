"""Pipeline-level tests: article-enriched candidates through
build_telegram_stories and the rendered Telegram message.

Run with:  .venv/bin/python -m pytest src/test_article_enrichment.py -q
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.main import build_telegram_stories
from src.telegram_formatter import build_message

NOW = datetime(2026, 8, 9, 7, 3, 10, tzinfo=timezone.utc)

CFG = {
    "just_in_freshness_minutes": 15,
    "max_briefing_sentences": 10,
    "target_message_chars": 1500,
    "max_message_chars": 3000,
}

HEADLINE = (
    "State of emergency declared as fast-moving Canada "
    "wildfire doubles in size"
)


def candidate(**overrides):
    base = {
        "story_id": "story-1",
        "id": "story-1",
        "event_id": "event-1",
        "title": HEADLINE,
        "summary": (
            "The Bald Range wildfire in British Columbia, "
            "still considered out of control, has spread over "
            "more than 36 sq miles."
        ),
        "url": "https://www.bbc.co.uk/news/articles/c1234",
        "source": "BBC World",
        "category": "disaster",
        "score": 70,
        "confidence": "medium",
        "priority_level": "HIGH",
        "event_status": "NEW",
        "primary_source": False,
        "tier": 2,
        "urgency_terms": ["wildfire", "state of emergency"],
        "effective_at": (
            NOW - timedelta(minutes=5)
        ).isoformat(),
    }
    base.update(overrides)
    return base


ARTICLE = [
    "Officials in British Columbia said the situation "
    "remained extremely dangerous.",
    "More than 20,000 residents have been forced from "
    "their homes so far.",
    "Fire crews are working to protect the town of "
    "Ashcroft from the advancing flames.",
    "The provincial government has opened three "
    "emergency shelters for displaced families.",
]


def build_one(cands):
    stories = build_telegram_stories(cands, CFG, NOW)
    return stories[0]


class TestArticleEnrichmentPipeline:
    def test_article_sentences_enter_briefing(self):
        story = build_one(
            [candidate(article_sentences=list(ARTICLE))]
        )
        texts = {
            r["text"] for r in story["briefing"]["sentences"]
        }
        assert any(
            s.startswith("More than 20,000 residents")
            for s in texts
        )
        assert any(
            s.startswith("Fire crews are working")
            for s in texts
        )

    def test_article_sentences_capped_at_ten(self):
        body = list(ARTICLE) + [
            "Additional sentence number %d here." % i
            for i in range(1, 30)
        ]
        story = build_one(
            [candidate(article_sentences=body)]
        )
        assert len(story["briefing"]["sentences"]) <= 10

    def test_no_article_sentences_briefing_unchanged(self):
        plain = build_one([candidate()])
        with_article = build_one(
            [candidate(article_sentences=list(ARTICLE))]
        )
        assert plain["briefing"]["source"] == "BBC World"
        assert with_article["briefing"]["source"] == "BBC World"
        assert len(plain["briefing"]["sentences"]) < len(
            with_article["briefing"]["sentences"]
        )

    def test_headline_paraphrase_article_sentences_dropped(self):
        story = build_one(
            [
                candidate(
                    article_sentences=[
                        "The fast-moving Canada wildfire "
                        "doubles in size.",
                        "Officials confirmed the fire had "
                        "spread across the province.",
                    ]
                )
            ]
        )
        texts = [
            r["text"]
            for r in story["briefing"]["sentences"]
        ]
        assert not any(
            "doubles in size" in t for t in texts
        )
        assert any(
            "spread across the province" in t for t in texts
        )

    def test_filler_article_sentences_dropped(self):
        story = build_one(
            [
                candidate(
                    article_sentences=[
                        "This is a developing story.",
                        "Officials said the fire was still "
                        "growing this morning.",
                    ]
                )
            ]
        )
        texts = [
            r["text"]
            for r in story["briefing"]["sentences"]
        ]
        assert not any(
            "developing story" in t for t in texts
        )

    def test_duplicate_article_sentence_dropped(self):
        story = build_one(
            [
                candidate(
                    article_sentences=[
                        ARTICLE[0],
                        ARTICLE[0],
                        ARTICLE[1],
                    ]
                )
            ]
        )
        texts = [
            r["text"]
            for r in story["briefing"]["sentences"]
        ]
        assert texts.count(ARTICLE[0]) == 1

    def test_headline_only_story_still_rejected(self):
        cands = [
            candidate(
                summary="A fast-moving Canada wildfire "
                "doubles in size.",
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        assert stories == []

    def test_article_sentences_rescue_headline_only_story(self):
        cands = [
            candidate(
                summary="A fast-moving Canada wildfire "
                "doubles in size.",
                article_sentences=list(ARTICLE),
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        assert len(stories) == 1

    def test_provenance_attributed_to_primary_source(self):
        story = build_one(
            [candidate(article_sentences=list(ARTICLE))]
        )
        sources = {
            r["source"] for r in story["briefing"]["sentences"]
        }
        assert sources == {"BBC World"}

    def test_no_user_visible_expansion_marker(self):
        story = build_one(
            [candidate(article_sentences=list(ARTICLE))]
        )
        msg = build_message(story, CFG, NOW)
        lowered = (msg.get("text") or "").lower()
        assert "expanded" not in lowered
        assert "supplemented" not in lowered
        assert "article text" not in lowered

    def test_default_cap_unchanged_without_article(self):
        cfg = dict(CFG)
        cfg["max_briefing_sentences"] = 10
        cands = [
            candidate(
                summary=(
                    "Officials said the fire had grown. "
                    "Residents were told to evacuate. "
                    "The town of Ashcroft was threatened. "
                    "Three shelters have opened. "
                    "Crews arrived overnight. "
                    "Power lines were cut. "
                    "Roads were closed. "
                    "Drones surveyed the area. "
                    "Tankers dropped water. "
                    "Helicopters joined the effort. "
                    "Schools were closed on Friday. "
                    "A curfew was imposed."
                )
            )
        ]
        story = build_telegram_stories(cands, cfg, NOW)[0]
        assert len(story["briefing"]["sentences"]) == 12
