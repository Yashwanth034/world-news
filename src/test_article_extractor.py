"""Unit tests for the article extraction module.

Run with:  .venv/bin/python -m pytest src/test_article_extractor.py -q

All tests are offline: the network fetcher is injected or
mocked, never actually called.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.article_extractor import (
    ArticleCache,
    article_sentences,
    count_useful,
    domain_allowed,
    enrich_thin_stories,
    feed_domain_allowlist,
    non_article_url,
    _public_ip,
)

NOW = datetime(2026, 8, 9, 7, 3, 10, tzinfo=timezone.utc)

SEGMENTS = [
    "video",
    "videos",
    "liveblog",
    "liveblogs",
    "newsfeed",
    "watch",
    "programmes",
]


def make_cfg(**article_overrides):
    cfg = {
        "article_extraction": {
            "enabled": True,
            "max_fetches_per_run": 15,
            "min_domain_interval_seconds": 0,
            "max_article_sentences": 12,
            "cache_ttl_hours_ok": 48,
            "cache_ttl_hours_error": 24,
            "non_article_segments": SEGMENTS,
            "paywall_markers": ["to continue reading"],
        },
        "telegram": {"just_in_freshness_minutes": 15},
        "feeds": [
            {
                "name": "BBC World",
                "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
            }
        ],
    }
    cfg["article_extraction"].update(article_overrides)
    return cfg


def candidate(**overrides):
    base = {
        "story_id": "story-1",
        "id": "story-1",
        "event_id": "event-1",
        "title": (
            "State of emergency declared as fast-moving "
            "Canada wildfire doubles in size"
        ),
        "summary": (
            "The Bald Range wildfire in British Columbia, "
            "still considered out of control, has spread over "
            "more than 36 sq miles."
        ),
        "url": "https://www.bbc.co.uk/news/articles/c1234",
        "source": "BBC World",
        "score": 70,
        "confidence": "medium",
        "priority_level": "HIGH",
        "event_status": "NEW",
        "effective_at": (
            NOW - timedelta(minutes=5)
        ).isoformat(),
    }
    base.update(overrides)
    return base


def fake_fetcher(status="ok", text=None, error=None):
    def _fetch(url, art_cfg, allowlist, robots=None, pace=None):
        if error is not None:
            raise error
        if status == "ok":
            text_value = text or (
                "Officials in British Columbia said the "
                "situation remained extremely dangerous. "
                "More than 20,000 residents have been forced "
                "from their homes so far. Fire crews are "
                "working to protect the town of Ashcroft."
            )
            return ("ok", {"text": text_value, "title": None})
        return (status, {})
    return _fetch


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------

class TestNonArticleUrl:
    def test_plain_article_accepted(self):
        assert not non_article_url(
            "https://www.bbc.co.uk/news/articles/c1234",
            SEGMENTS,
        )

    def test_video_rejected(self):
        assert non_article_url(
            "https://www.aljazeera.com/video/xyz",
            SEGMENTS,
        )

    def test_videos_plural_rejected(self):
        assert non_article_url(
            "https://www.bbc.co.uk/news/videos/c987",
            SEGMENTS,
        )

    def test_liveblog_rejected(self):
        assert non_article_url(
            "https://www.aljazeera.com/news/liveblog/xyz",
            SEGMENTS,
        )

    def test_newsfeed_rejected(self):
        assert non_article_url(
            "https://www.aljazeera.com/newsfeed/xyz",
            SEGMENTS,
        )

    def test_programmes_rejected(self):
        assert non_article_url(
            "https://www.bbc.co.uk/programmes/p0001",
            SEGMENTS,
        )

    def test_watch_rejected(self):
        assert non_article_url(
            "https://www.bbc.co.uk/watch/live",
            SEGMENTS,
        )

    def test_google_news_wrapper_rejected(self):
        assert non_article_url(
            "https://news.google.com/rss/articles/CBMi?hl=en",
            SEGMENTS,
        )

    def test_missing_url_rejected(self):
        assert non_article_url(None, SEGMENTS)


class TestDomainAllowlist:
    def test_feeds_build_allowlist(self):
        allowlist = feed_domain_allowlist(make_cfg()["feeds"])
        assert "bbci.co.uk" in allowlist
        assert "bbc.co.uk" in allowlist

    def test_article_domain_allowed_via_alias(self):
        allowlist = feed_domain_allowlist(make_cfg()["feeds"])
        assert domain_allowed(
            "https://www.bbc.co.uk/news/articles/c1234",
            allowlist,
        )

    def test_foreign_domain_blocked(self):
        allowlist = feed_domain_allowlist(make_cfg()["feeds"])
        assert not domain_allowed(
            "https://www.evil-example.com/news/x",
            allowlist,
        )

    def test_empty_allowlist_blocks_everything(self):
        assert not domain_allowed(
            "https://www.bbc.co.uk/news/articles/c1",
            set(),
        )

    def test_non_http_scheme_blocked(self):
        allowlist = feed_domain_allowlist(make_cfg()["feeds"])
        assert not domain_allowed(
            "file:///etc/passwd",
            allowlist,
        )

    def test_no_feeds_yields_empty_allowlist(self):
        assert feed_domain_allowlist([]) == set()


class TestPublicIp:
    def test_loopback_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "src.article_extractor.socket.getaddrinfo",
            lambda *a, **k: [
                (
                    2,
                    1,
                    6,
                    "",
                    ("127.0.0.1", 443),
                )
            ],
        )
        assert _public_ip("localhost") is None

    def test_private_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "src.article_extractor.socket.getaddrinfo",
            lambda *a, **k: [
                (
                    2,
                    1,
                    6,
                    "",
                    ("10.0.0.5", 443),
                )
            ],
        )
        assert _public_ip("intranet.example") is None

    def test_link_local_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "src.article_extractor.socket.getaddrinfo",
            lambda *a, **k: [
                (
                    2,
                    1,
                    6,
                    "",
                    ("169.254.10.10", 443),
                )
            ],
        )
        assert _public_ip("mystery.example") is None

    def test_public_accepted(self, monkeypatch):
        monkeypatch.setattr(
            "src.article_extractor.socket.getaddrinfo",
            lambda *a, **k: [
                (
                    2,
                    1,
                    6,
                    "",
                    ("151.101.1.111", 443),
                )
            ],
        )
        assert _public_ip("www.bbc.co.uk") == "151.101.1.111"


# ---------------------------------------------------------------------------
# Sentence helpers
# ---------------------------------------------------------------------------

class TestCountUseful:
    def test_rich_summary_counts_two(self):
        assert count_useful(
            "Officials said the fire had grown. "
            "Residents were told to evacuate.",
            "Canada wildfire doubles in size",
        ) == 2

    def test_headline_paraphrase_only_counts_zero(self):
        assert count_useful(
            "A fast-moving Canada wildfire doubles in size.",
            (
                "State of emergency declared as fast-moving "
                "Canada wildfire doubles in size"
            ),
        ) == 0

    def test_empty_summary_counts_zero(self):
        assert count_useful("", "Some headline") == 0

    def test_filler_dropped(self):
        assert count_useful(
            "This is a developing story.",
            "Some headline",
        ) == 0

    def test_duplicates_count_once(self):
        assert count_useful(
            "The fire has spread. The fire has spread.",
            "Some headline",
        ) == 1


class TestArticleSentences:
    def test_extracts_verbatim_sentences(self):
        out = article_sentences(
            "Officials in British Columbia said the situation "
            "remained extremely dangerous. More than 20,000 "
            "residents have been forced from their homes.",
            "Canada wildfire doubles in size",
        )
        assert len(out) == 2
        assert out[0].startswith("Officials in British Columbia")

    def test_headline_paraphrase_dropped(self):
        out = article_sentences(
            "A fast-moving Canada wildfire doubles in size. "
            "Evacuation orders were issued for three towns.",
            (
                "State of emergency declared as fast-moving "
                "Canada wildfire doubles in size"
            ),
        )
        assert len(out) == 1
        assert "Evacuation" in out[0]

    def test_filler_dropped(self):
        out = article_sentences(
            "This is a developing story. Officials spoke "
            "at a press conference.",
            "Some headline",
        )
        assert len(out) == 1

    def test_capped_at_max_sentences(self):
        body = "Sentence number one here. " * 30
        out = article_sentences(body, "Headline", max_sentences=12)
        assert len(out) <= 12

    def test_headline_line_dropped(self):
        out = article_sentences(
            "Helicopter crash kills pilot and crew member "
            "amid Utah wildfire battle\n"
            "Utah wildfire response continues despite loss "
            "of helicopter crew, with containment standing "
            "at 24 percent.",
            "Helicopter crash kills pilot and crew member "
            "amid Utah wildfire battle",
        )
        assert all(
            "amid Utah wildfire battle" not in s
            for s in out
        )

    def test_nav_junk_lines_dropped(self):
        out = article_sentences(
            "Officials confirmed the crash on Saturday.\n"
            "Recommended Stories\n"
            "list of 3 items\n"
            "- list 1 of 3Vance says US destroyed Iran's "
            "nuclear programme\n"
            "Related topics\n"
            "The bodies were recovered on Sunday.",
            "Some headline",
        )
        assert len(out) == 2
        assert all(
            "Recommended" not in s
            and "list of 3" not in s
            and "Related" not in s
            and "Vance" not in s
            for s in out
        )

    def test_section_header_line_dropped(self):
        out = article_sentences(
            "Serbian-made artillery shells\n"
            "The president left Belgrade on Sunday evening.",
            "Some headline",
        )
        assert len(out) == 1
        assert out[0].startswith("The president")

    def test_two_sentences_on_one_line_split(self):
        out = article_sentences(
            "The fire spread quickly. Crews arrived by "
            "dawn.",
            "Some headline",
        )
        assert len(out) == 2


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestArticleCache:
    def test_round_trip(self, tmp_path):
        cache = ArticleCache(tmp_path / "cache.db")
        cache.set(
            "story-1",
            "https://www.bbc.co.uk/news/articles/c1",
            "ok",
            text="body text",
            sentences=["s1", "s2"],
            now=NOW,
        )
        entry = cache.get("story-1", now=NOW)
        assert entry["status"] == "ok"
        assert entry["text"] == "body text"
        assert json.loads(entry["sentences_json"]) == [
            "s1",
            "s2",
        ]
        assert entry["ttl_hours"] == 48
        cache.close()

    def test_positive_ttl_expiry(self, tmp_path):
        cache = ArticleCache(tmp_path / "cache.db")
        cache.set(
            "story-1",
            "https://www.bbc.co.uk/news/articles/c1",
            "ok",
            sentences=["s1", "s2"],
            now=NOW,
        )
        later = NOW + timedelta(hours=49)
        assert cache.get("story-1", now=later) is None
        cache.close()

    def test_negative_ttl_shorter(self, tmp_path):
        cache = ArticleCache(tmp_path / "cache.db")
        cache.set(
            "story-1",
            "https://www.bbc.co.uk/news/articles/c1",
            "http_error",
            now=NOW,
        )
        entry = cache.get("story-1", now=NOW)
        assert entry["status"] == "http_error"
        assert entry["ttl_hours"] == 24
        later = NOW + timedelta(hours=25)
        assert cache.get("story-1", now=later) is None
        cache.close()

    def test_error_result_has_no_text(self, tmp_path):
        cache = ArticleCache(tmp_path / "cache.db")
        cache.set(
            "story-1",
            "https://www.bbc.co.uk/news/articles/c1",
            "blocked",
            text=None,
            sentences=None,
            now=NOW,
        )
        entry = cache.get("story-1", now=NOW)
        assert entry["text"] is None
        assert entry["sentences_json"] is None
        cache.close()

    def test_missing_entry_returns_none(self, tmp_path):
        cache = ArticleCache(tmp_path / "cache.db")
        assert cache.get("nope", now=NOW) is None
        cache.close()


# ---------------------------------------------------------------------------
# Enrichment driver (offline, injected fetcher)
# ---------------------------------------------------------------------------

class TestEnrichThinStories:
    def test_disabled_config_noop(self):
        cfg = make_cfg(enabled=False)
        cands = [candidate()]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert out == cands
        assert stats["enabled"] is False
        assert "article_sentences" not in out[0]

    def test_unimportant_thin_story_not_fetched(self):
        cfg = make_cfg()
        cands = [
            candidate(score=50, priority_level="NORMAL")
        ]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["eligible"] == 0
        assert "article_sentences" not in out[0]

    def test_thin_important_story_fetched(self):
        cfg = make_cfg()
        cands = [candidate()]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["eligible"] == 1
        assert stats["fetched"] == 1
        assert stats["expanded"] == 1
        assert len(out[0]["article_sentences"]) >= 2

    def test_not_thin_story_skipped(self):
        cfg = make_cfg()
        cands = [
            candidate(
                summary=(
                    "Officials said the fire had grown. "
                    "Residents were told to evacuate. "
                    "The town of Ashcroft was threatened."
                )
            )
        ]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["thin"] == 0
        assert stats["fetched"] == 0

    def test_just_in_story_fetched(self):
        cfg = make_cfg()
        cands = [
            candidate(
                score=60,
                confidence="high",
                effective_at=(
                    NOW - timedelta(minutes=5)
                ).isoformat(),
            )
        ]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["eligible"] == 1
        assert stats["fetched"] == 1

    def test_update_story_fetched_at_low_score(self):
        cfg = make_cfg()
        cands = [
            candidate(
                score=55,
                priority_level="NORMAL",
                event_status="UPDATE",
            )
        ]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["eligible"] == 1

    def test_event_dedup_one_fetch_per_event(self):
        cfg = make_cfg()
        cands = [
            candidate(story_id="story-2", score=70),
            candidate(
                story_id="story-3",
                score=60,
                url="https://www.bbc.co.uk/news/articles/c999",
            ),
        ]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["fetched"] == 1
        assert "article_sentences" in out[0]
        assert "article_sentences" not in out[1]

    def test_non_article_url_never_fetched(self):
        cfg = make_cfg()
        cands = [
            candidate(
                url="https://www.aljazeera.com/video/xyz",
            )
        ]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["non_article"] == 1
        assert stats["fetched"] == 0

    def test_foreign_domain_never_fetched(self):
        cfg = make_cfg()
        cands = [
            candidate(
                url="https://www.foreignnews.example/story",
            )
        ]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["domain_blocked"] == 1
        assert stats["fetched"] == 0

    def test_cache_hit_skips_fetch(self, tmp_path):
        cfg = make_cfg()
        cache = ArticleCache(tmp_path / "cache.db")
        cache.set(
            "story-1",
            "https://www.bbc.co.uk/news/articles/c1234",
            "ok",
            sentences=["a", "b"],
            now=NOW,
        )
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=cache,
            fetcher=fake_fetcher(),
        )
        assert stats["cache_hits"] == 1
        assert stats["fetched"] == 0
        assert len(out[0]["article_sentences"]) == 2
        cache.close()

    def test_negative_cache_hit_skips_fetch(self, tmp_path):
        cfg = make_cfg()
        cache = ArticleCache(tmp_path / "cache.db")
        cache.set(
            "story-1",
            "https://www.bbc.co.uk/news/articles/c1234",
            "http_error",
            now=datetime.now(timezone.utc),
        )
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=cache,
            fetcher=fake_fetcher(),
        )
        assert stats["cache_hits"] == 1
        assert stats["fetched"] == 0
        assert "article_sentences" not in out[0]
        cache.close()

    def test_fetch_ok_persists_cache(self, tmp_path):
        cfg = make_cfg()
        cache = ArticleCache(tmp_path / "cache.db")
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=cache,
            fetcher=fake_fetcher(),
        )
        entry = cache.get("story-1", now=NOW)
        assert entry is not None
        assert entry["status"] == "ok"
        assert len(json.loads(entry["sentences_json"])) >= 2
        cache.close()

    def test_fetch_ok_but_too_few_sentences_not_expanded(self):
        cfg = make_cfg()
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=None,
            fetcher=fake_fetcher(
                text=(
                    "Officials in British Columbia confirmed "
                    "the fire had spread across the region."
                )
            ),
        )
        assert stats["expanded"] == 0
        assert stats["not_expanded"] == 1
        assert "article_sentences" not in out[0]

    def test_http_error_not_expanded(self):
        cfg = make_cfg()
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=None,
            fetcher=fake_fetcher(status="http_error"),
        )
        assert stats["http_error"] == 1
        assert "article_sentences" not in out[0]

    def test_paywall_not_expanded(self):
        cfg = make_cfg()
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=None,
            fetcher=fake_fetcher(status="paywall"),
        )
        assert stats["paywall"] == 1
        assert "article_sentences" not in out[0]

    def test_no_text_not_expanded(self):
        cfg = make_cfg()
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=None,
            fetcher=fake_fetcher(status="no_text"),
        )
        assert stats["no_text"] == 1
        assert "article_sentences" not in out[0]

    def test_timeout_not_expanded(self):
        cfg = make_cfg()
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=None,
            fetcher=fake_fetcher(status="timeout"),
        )
        assert stats["timeout"] == 1
        assert "article_sentences" not in out[0]

    def test_budget_cap(self):
        cfg = make_cfg(max_fetches_per_run=2)
        cands = [
            candidate(
                story_id="s%d" % i,
                score=80 - i,
                event_id="event-s%d" % i,
                title="Distinct wildfire story number %d" % i,
                summary=(
                    "The fire continued to spread through "
                    "the night."
                ),
            )
            for i in range(4)
        ]
        out, stats = enrich_thin_stories(
            cands, cfg, NOW, cache=None, fetcher=fake_fetcher()
        )
        assert stats["fetched"] == 2
        assert stats["budget_exhausted"] >= 1

    def test_fetcher_exception_never_fails_pipeline(self):
        cfg = make_cfg()
        out, stats = enrich_thin_stories(
            candidates=[candidate()],
            cfg=cfg,
            now_dt=NOW,
            cache=None,
            fetcher=fake_fetcher(error=RuntimeError("boom")),
        )
        assert stats["network_error"] == 1
        assert "article_sentences" not in out[0]

    def test_mixed_failures_isolated(self):
        cfg = make_cfg()
        good = candidate(
            story_id="story-a",
            title="Evacuations ordered in western Oregon",
            summary=(
                "The fire continued to spread through "
                "the night."
            ),
        )
        bad = candidate(
            story_id="story-b",
            title="Flood warnings issued across Iowa",
            summary=(
                "The river was expected to crest on "
                "Sunday evening."
            ),
            url="https://www.bbc.co.uk/news/articles/c-bad",
        )
        bad["event_id"] = "event-b"

        calls = []

        def fetcher(url, art_cfg, allowlist, robots=None, pace=None):
            calls.append(url)
            if "c-bad" in url:
                return ("paywall", {})
            return (
                "ok",
                {
                    "text": (
                        "Officials in British Columbia said the "
                        "situation remained extremely dangerous. "
                        "More than 20,000 residents have been "
                        "forced from their homes so far."
                    ),
                    "title": None,
                },
            )

        out, stats = enrich_thin_stories(
            [good, bad], cfg, NOW, cache=None, fetcher=fetcher
        )
        assert len(calls) == 2
        assert stats["paywall"] == 1
        assert stats["expanded"] == 1
