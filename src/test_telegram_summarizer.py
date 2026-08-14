"""Regression tests for the source-grounded summarizer.

Covers: 2-8 sentence summaries, source-supported
paraphrasing, preservation of numbers/names/dates,
attribution and uncertainty, conflicting facts, no invented
information, duplicate fact removal, insufficient source
content rejection, truncated/junk removal, and unchanged
final output format.

Run with:  .venv/bin/python -m pytest src/test_telegram_summarizer.py -q
"""
from datetime import datetime, timedelta, timezone

from src.main import build_telegram_stories
from src.telegram_formatter import build_message
from src.telegram_summarizer import (
    front_attribution,
    quality_check_sentence,
    summarize_rows,
    verify_row,
)

NOW = datetime(2026, 8, 10, 8, 0, 0, tzinfo=timezone.utc)

CFG = {
    "just_in_freshness_minutes": 15,
    "max_briefing_sentences": 10,
    "target_message_chars": 1500,
    "max_message_chars": 3000,
    "summarization": {
        "min_sentences": 2,
        "max_sentences": 8,
        "tier1_max": 6,
    },
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


def story(rows):
    """A queue-style story whose briefing carries `rows`."""
    return {
        "story_id": "story-1",
        "title": HEADLINE,
        "headline": HEADLINE,
        "source": "BBC World",
        "url": "https://www.bbc.co.uk/news/articles/c1234",
        "briefing": {"sentences": rows},
    }


def row(text, item_id="story-1"):
    return {"text": text, "source": "BBC World", "item_id": item_id}


def summarize(texts, source=None, headline=HEADLINE):
    """Helper: run the full summarize_rows pipeline."""
    rows = [
        row(t) for t in texts
    ]
    return summarize_rows(
        rows,
        source or " ".join(texts),
        headline,
        cfg=CFG["summarization"],
    )


# ---------------------------------------------------------------------------
# 1. Clear 2-4 sentence summaries
# ---------------------------------------------------------------------------


class TestSummaryLength:
    def test_rich_source_yields_between_two_and_four(self):
        texts = [
            "More than 20,000 residents have been forced from "
            "their homes.",
            "The fire started near the town of Ashcroft on "
            "Sunday.",
            "Winds of up to 40 mph are pushing the flames "
            "northeast.",
            "Temperatures are forecast to stay above 30 "
            "degrees this week.",
            "Evacuation orders now cover eight communities.",
            "Air quality readings in Kamloops reached "
            "hazardous levels.",
            "Police are patrolling evacuated neighbourhoods "
            "to deter looters.",
            "Power lines have been cut to prevent new "
            "ignitions.",
            "Rail services through the valley have been "
            "suspended.",
            "The province has asked the military for "
            "assistance.",
        ]
        kept, stats = summarize(texts)
        assert kept is not None
        assert 2 <= len(kept) <= 4
        assert stats["rejected"] is None

    def test_never_padded_beyond_source(self):
        texts = [
            "More than 20,000 residents have been forced from "
            "their homes.",
            "The fire started near the town of Ashcroft on "
            "Sunday.",
        ]
        kept, _ = summarize(texts)
        assert len(kept) == 2

    def test_pipeline_story_same_limits(self):
        cands = [
            candidate(
                summary=(
                    "The Bald Range wildfire has grown to "
                    "cover 36 square miles. "
                    "Officials said 20,000 residents have been "
                    "evacuated so far. "
                    "Crews are dropping water on the blaze "
                    "from helicopters. "
                    "The fire started near the town of "
                    "Ashcroft on Sunday."
                )
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        assert len(stories) == 1
        rows = stories[0]["briefing"]["sentences"]
        assert 2 <= len(rows) <= 8


# ---------------------------------------------------------------------------
# 2. Source-supported paraphrasing
# ---------------------------------------------------------------------------


class TestParaphrasing:
    def test_trailing_attribution_fronted(self):
        assert front_attribution(
            "Three people were injured, police said."
        ) == "Police said three people were injured."

    def test_according_to_fronted(self):
        assert front_attribution(
            "The road was closed, according to officials."
        ) == "According to officials, the road was closed."

    def test_facts_survive_rewrite_verbatim(self):
        # The source carries both the attributed paragraph form
        # and the independent lead sentence; the composed
        # attribution-fronted sentences must verify against it.
        sentences = [
            "Three people were injured, police said.",
            "The road was closed, according to officials.",
        ]
        source = (
            "Police said three people were injured. "
            "According to officials, the road was closed. "
            + " ".join(sentences)
        )
        kept, _ = summarize(
            sentences,
            source=source,
            headline="Crash closes highway",
        )
        texts = [r["text"] for r in kept]
        assert "Police said three people were injured." in texts
        assert (
            "According to officials, the road was closed."
            in texts
        )

    def test_unsafe_trailing_attribution_untouched(self):
        # Body ends with terminal punctuation: two sentences,
        # never fused.
        assert front_attribution(
            "He was seen fleeing. Police said."
        ) == "He was seen fleeing. Police said."
        # Quoted body is never spliced.
        assert front_attribution(
            '"We are proud", the mayor said.'
        ) == '"We are proud", the mayor said.'


# ---------------------------------------------------------------------------
# 3. Preservation of numbers, names, dates
# ---------------------------------------------------------------------------


class TestFactPreservation:
    def test_numbers_names_dates_preserved(self):
        texts = [
            "More than 20,000 residents have been forced from "
            "their homes.",
            "The fire started near the town of Ashcroft on "
            "Sunday.",
            "Winds of up to 40 mph are pushing the flames "
            "northeast.",
        ]
        kept, _ = summarize(texts)
        joined = " ".join(r["text"] for r in kept)
        assert "20,000" in joined
        assert "Ashcroft" in joined
        assert "Sunday" in joined
        assert "40 mph" in joined

    def test_numbers_never_altered(self):
        # 36 vs 63: the exact figure must survive untouched.
        text = "The fire has spread over more than 36 sq miles."
        kept, _ = summarize(
            [text, "The fire started near Ashcroft on Sunday."],
            source=" ".join(
                [text, "The fire started near Ashcroft on Sunday."]
            ),
            headline="Fire grows in the valley",
        )
        assert any("36 sq miles" in r["text"] for r in kept)


# ---------------------------------------------------------------------------
# 4. Attribution and uncertainty
# ---------------------------------------------------------------------------


class TestAttribution:
    def test_attribution_not_confirmed(self):
        text = "Police said the crash killed two people."
        kept, _ = summarize(
            [text, "The road was closed, according to officials."],
            source=(
                text
                + " The road was closed. "
                "The road was closed, according to officials."
            ),
            headline="Crash reported on motorway",
        )
        joined = " ".join(r["text"] for r in kept)
        assert "said" in joined
        # No confirmation wording is invented.
        assert "confirmed" not in joined

    def test_uncertainty_preserved(self):
        text = "Officials said the storm could strengthen further."
        kept, _ = summarize(
            [text, "Residents were told to stay indoors."],
            source=" ".join(
                [text, "Residents were told to stay indoors."]
            ),
            headline="Officials assess storm damage",
        )
        assert any("could" in r["text"] for r in kept)


# ---------------------------------------------------------------------------
# 5. Conflicting facts
# ---------------------------------------------------------------------------


class TestConflicts:
    def test_conflicting_numbers_never_merged(self):
        # The article is the primary source: its 12,000 figure
        # wins and the conflicting RSS 20,000 is dropped, so
        # the summary never merges the two claims.
        cands = [
            candidate(
                summary=(
                    "A fast-moving Canada wildfire doubles "
                    "in size. "
                    "Officials said 20,000 residents were "
                    "evacuated."
                ),
                article_sentences=[
                    "Officials said 12,000 residents were "
                    "evacuated.",
                    "The fire has grown to cover 36 square "
                    "miles.",
                ],
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        assert len(stories) == 1
        joined = " ".join(
            r["text"]
            for r in stories[0]["briefing"]["sentences"]
        )
        # Only one figure survives; the two are never combined.
        assert ("20,000" in joined) != ("12,000" in joined)
        assert "12,000" in joined

    def test_article_primary_source_wins_conflict(self):
        cands = [
            candidate(
                summary=(
                    "A fast-moving Canada wildfire doubles "
                    "in size. "
                    "Officials said 20,000 residents were "
                    "evacuated."
                ),
                article_sentences=[
                    "Officials said 12,000 residents were "
                    "evacuated.",
                    "The fire has grown to cover 36 square "
                    "miles.",
                ],
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        joined = " ".join(
            r["text"]
            for r in stories[0]["briefing"]["sentences"]
        )
        assert "12,000" in joined
        assert "20,000" not in joined


# ---------------------------------------------------------------------------
# 6. No invented information
# ---------------------------------------------------------------------------


class TestNoInvention:
    def test_verification_blocks_unsupported_numbers(self):
        row = {
            "text": "The fire killed 4,000 people.",
            "item_id": "x",
        }
        ok, problems = verify_row(row, "The fire grew overnight.")
        assert not ok
        assert any("4000" in p for p in problems)

    def test_verification_blocks_unsupported_names(self):
        row = {
            "text": "Mayor Kamloops declared the emergency.",
            "item_id": "x",
        }
        ok, problems = verify_row(row, "The fire grew overnight.")
        assert not ok
        assert any("kamloops" in p for p in problems)

    def test_composed_summary_never_adds_facts(self):
        texts = [
            "The fire has grown to cover 36 square miles.",
            "Officials said 20,000 residents have been "
            "evacuated.",
        ]
        kept, _ = summarize(texts)
        from src.telegram_summarizer import extract_facts
        source_numbers = {"36", "20000"}
        for r in kept:
            facts = extract_facts(r["text"])
            assert facts["numbers"] <= source_numbers


# ---------------------------------------------------------------------------
# 7. Duplicate fact removal
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_identical_sentences_count_once(self):
        text = "More than 20,000 residents have been evacuated."
        kept, _ = summarize(
            [text, text, "Winds of up to 40 mph are pushing "
             "the flames northeast."]
        )
        assert len(kept) == 2
        joined = " ".join(r["text"] for r in kept)
        assert joined.count("20,000") == 1

    def test_cross_source_same_consequence_kept_once(self):
        # RSS and the article report the same evacuation
        # consequence; the article is the primary source and
        # also carries a distinct fact.  The consequence
        # appears exactly once in the final briefing.
        cands = [
            candidate(
                summary=(
                    "Officials said 20,000 residents were "
                    "evacuated."
                ),
                article_sentences=[
                    "More than 20,000 residents were evacuated.",
                    "The fire has grown to cover 36 square "
                    "miles.",
                ],
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        assert len(stories) == 1
        joined = " ".join(
            r["text"]
            for r in stories[0]["briefing"]["sentences"]
        )
        assert joined.count("20,000") == 1
        assert "36 square miles" in joined

    def test_distinct_facts_both_kept(self):
        texts = [
            "20,000 residents were evacuated.",
            "40 mph winds are pushing the flames northeast.",
        ]
        kept, _ = summarize(texts)
        joined = " ".join(r["text"] for r in kept)
        assert "20,000" in joined
        assert "40 mph" in joined


# ---------------------------------------------------------------------------
# 8. Insufficient source content -> rejected
# ---------------------------------------------------------------------------


class TestInsufficientContent:
    def test_single_sentence_rejected(self):
        kept, stats = summarize(
            ["The fire is growing near the town."]
        )
        assert kept is None
        assert stats["rejected"] == "insufficient_information"

    def test_headline_only_rejected(self):
        kept, stats = summarize(
            ["A fast-moving Canada wildfire doubles in size."]
        )
        assert kept is None

    def test_pipeline_never_queues_below_two(self):
        cands = [
            candidate(
                summary=(
                    "A fast-moving Canada wildfire doubles "
                    "in size. "
                    "Officials said the fire remains "
                    "dangerous."
                )
            )
        ]
        stats = {}
        stories = build_telegram_stories(
            cands, CFG, NOW, summarization_stats=stats
        )
        assert stories == []
        assert stats["rejected_insufficient"] == 1


# ---------------------------------------------------------------------------
# 9. Truncated / junk article content -> removed
# ---------------------------------------------------------------------------


class TestJunkRemoval:
    def test_truncated_tail_dropped(self):
        # The source says "their healthy 14-year-old son";
        # a summary sentence ending "...their healthy." is a
        # feed truncation and must be dropped by verification.
        source = (
            "The families were left in shock after the sudden "
            "death of their healthy 14-year-old son."
        )
        text = (
            "The families were left in shock after the sudden "
            "death of their healthy."
        )
        kept, stats = summarize(
            [text],
            source=source,
            headline="Families demand answers",
        )
        if kept is not None:
            assert "their healthy." not in " ".join(
                r["text"] for r in kept
            )
        else:
            assert stats["rejected"] in (
                "insufficient_information",
                "verification",
            )

    def test_nav_junk_line_rejected(self):
        kept, stats = summarize(
            ["Recommended stories for you."]
        )
        assert kept is None
        assert stats["rejected"] == "insufficient_information"

    def test_boilerplate_sentence_dropped(self):
        kept, stats = summarize(
            [
                "Sign up for our newsletter to get more "
                "stories like this.",
                "Officials said the fire remains dangerous.",
                "Evacuation orders now cover eight communities.",
            ]
        )
        assert kept is not None
        assert not any(
            "newsletter" in r["text"] for r in kept
        )

    def test_quality_gate_flags_incomplete_sentence(self):
        problems = quality_check_sentence(
            "The fire grew rapidly",
            HEADLINE,
        )
        assert any("complete" in p for p in problems)


# ---------------------------------------------------------------------------
# 10. Final output format unchanged
# ---------------------------------------------------------------------------


class TestFinalFormat:
    def test_message_structure_unchanged(self):
        cands = [
            candidate(
                effective_at=(
                    NOW - timedelta(minutes=60)
                ).isoformat(),
                summary=(
                    "The Bald Range wildfire has grown to "
                    "cover 36 square miles. "
                    "Officials said 20,000 residents have been "
                    "evacuated so far. "
                    "Crews are dropping water on the blaze "
                    "from helicopters."
                ),
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        msg = build_message(stories[0], CFG, NOW)
        assert msg is not None
        assert msg["parse_mode"] == "HTML"
        text = msg["text"]
        lines = text.split("\n\n")
        # label, headline, summary, source, read-more
        assert len(lines) == 5
        assert lines[0] == "\U0001F4F0 NEWS"
        assert lines[1] == "<b>" + HEADLINE + "</b>"
        assert lines[3] == "\U0001F4F0 Source: BBC World"
        assert lines[4].startswith("\U0001F517 ")
        assert "Read the full report" in lines[4]
        assert "\u2b50" not in text
        assert "\U0001F4CD" not in text

    def test_two_sentence_message_renders(self):
        cands = [
            candidate(
                summary=(
                    "The Bald Range wildfire has grown to "
                    "cover 36 square miles. "
                    "Officials said 20,000 residents have been "
                    "evacuated so far."
                )
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        msg = build_message(stories[0], CFG, NOW)
        assert msg is not None

    def test_summary_body_matches_briefing_rows(self):
        cands = [
            candidate(
                summary=(
                    "The Bald Range wildfire has grown to "
                    "cover 36 square miles. "
                    "Officials said 20,000 residents have been "
                    "evacuated so far."
                )
            )
        ]
        stories = build_telegram_stories(cands, CFG, NOW)
        rows = stories[0]["briefing"]["sentences"]
        msg = build_message(stories[0], CFG, NOW)
        assert msg is not None
        for r in rows:
            assert r["text"] in msg["text"]


# ---------------------------------------------------------------------------
# 11. Quote-boundary fact preservation
# ---------------------------------------------------------------------------


class TestQuoteBoundaryFactPreservation:
    """A sentence following a closing quote must be recognized as a
    separate sentence, and its facts must survive the whole pipeline.

    The formatter's split_sentences cannot see a period trapped
    inside a closing quote (". " The IMF said ...), so without this
    fix the IMF sentence fuses with the quoted fragment. When that
    fused row is the only sentence the story is rejected for having
    fewer than two sentences and the statistic is lost."""

    def test_post_quote_sentence_recognized_as_separate(self):
        rows = [
            {
                "text": (
                    '"We should reduce food." The International '
                    "Monetary Fund said today that subsidies must "
                    "fall by 15 percent."
                ),
                "source": "BBC",
                "item_id": "i",
            }
        ]
        kept, stats = summarize_rows(
            rows,
            '"We should reduce food." The International Monetary '
            "Fund said today that subsidies must fall by 15 "
            "percent.",
            "IMF warns on food subsidies",
            cfg=CFG["summarization"],
        )
        assert stats["rejected"] is None
        texts = [r["text"] for r in kept]
        # The post-quote sentence is its own fact-bearing row.
        assert any(
            t.startswith("The International Monetary Fund")
            for t in texts
        )
        # The statistic survives.
        assert any("15 percent" in t for t in texts)

    def test_single_fused_quote_row_no_longer_rejected(self):
        # The story's only row fuses the quote with the IMF sentence.
        # Before the fix this collapsed to a single sentence and the
        # story was rejected as <2 sentences, losing the statistic.
        text = (
            '"We should reduce food." The International Monetary '
            "Fund said today that subsidies must fall by 15 percent."
        )
        kept, stats = summarize_rows(
            [{"text": text, "source": "BBC", "item_id": "i"}],
            text,
            "IMF warns on food subsidies",
            cfg=CFG["summarization"],
        )
        assert stats["rejected"] is None
        joined = " ".join(r["text"] for r in kept)
        assert "15 percent" in joined
        assert "International Monetary Fund" in joined

    def test_curly_quote_boundary_split(self):
        # A curly closing quote works exactly like a straight one.
        text = (
            "\u201cWe should reduce food.\u201d The International "
            "Monetary Fund said today that subsidies must fall by "
            "15 percent."
        )
        kept, stats = summarize_rows(
            [{"text": text, "source": "BBC", "item_id": "i"}],
            text,
            "IMF warns on food subsidies",
            cfg=CFG["summarization"],
        )
        assert stats["rejected"] is None
        texts = [r["text"] for r in kept]
        assert any(
            t.startswith("The International Monetary Fund")
            for t in texts
        )
        assert any("15 percent" in t for t in texts)

    def test_continuous_quoted_sentence_not_split(self):
        # Two sentences inside ONE quote must stay a single sentence;
        # only a boundary after the closing quote is split.
        from src.telegram_summarizer import _split_quote_boundaries
        text = (
            '"I honestly can\u2019t remember the last time I bought '
            'red meat. We replaced it with chicken," she told DW.'
        )
        rows = _split_quote_boundaries(
            [{"text": text, "source": "BBC", "item_id": "i"}],
            "Iranians feel the cost of war",
        )
        assert len(rows) == 1
        assert "red meat. We replaced it with chicken" in rows[0]["text"]
        assert rows[0]["text"].endswith('she told DW.')

    def test_quote_split_only_at_closing_quote(self):
        # A comma-quote (no trapped period) is never split.
        from src.telegram_summarizer import _split_quote_boundaries
        text = '"We should reduce food," the IMF said.'
        rows = _split_quote_boundaries(
            [{"text": text, "source": "BBC", "item_id": "i"}],
            "IMF food advice",
        )
        assert len(rows) == 1
        assert rows[0]["text"].startswith('"We should reduce food,"')


# ---------------------------------------------------------------------------
# 12. Live-blog relevance filtering
# ---------------------------------------------------------------------------


class TestLiveBlogRelevance:
    """Unrelated stories appended by a live-blog or multi-topic
    source must not enter the summary; same-event context survives."""

    HEADLINE = "Canada wildfire doubles in size"

    LIVE_BLOG = (
        "The fire has grown to cover 36 square miles. "
        "An international effort is under way, with crews from "
        "the US, Canada and Mexico. "
        "Meanwhile, the International Monetary Fund warned that "
        "the country must cut spending by 15 percent. "
        "Separately, a total solar eclipse will cross the sky "
        "tonight, astronomers said."
    )

    def _summary(self, texts, headline=HEADLINE):
        from src.formatter import split_sentences
        rows = [
            {"text": s, "source": "BBC", "item_id": "i"}
            for s in split_sentences(self.LIVE_BLOG)
        ]
        return summarize_rows(
            rows,
            self.LIVE_BLOG,
            headline,
            cfg=CFG["summarization"],
        )

    def test_unrelated_liveblog_items_dropped(self):
        kept, stats = self._summary([self.LIVE_BLOG])
        joined = " ".join(r["text"] for r in (kept or []))
        assert stats["rejected"] is None
        assert "International Monetary Fund" not in joined
        assert "15 percent" not in joined
        assert "solar eclipse" not in joined

    def test_headline_event_context_kept(self):
        kept, _ = self._summary([self.LIVE_BLOG])
        joined = " ".join(r["text"] for r in (kept or []))
        assert "36 square miles" in joined
        # Crews from the US, Canada and Mexico are same-event context.
        assert "Canada" in joined

    def test_same_event_meanwhile_kept(self):
        from src.formatter import split_sentences
        text = (
            "At least four people, including a child, were killed "
            "in Russian drone and missile strikes on Kyiv and "
            "surrounding regions. "
            "Meanwhile, Ukraine struck another oil refinery in "
            "Russia, as Kyiv continues its campaign of long-range "
            "attacks on Russian energy facilities."
        )
        kept, stats = summarize_rows(
            [
                {"text": s, "source": "F24", "item_id": "i"}
                for s in split_sentences(text)
            ],
            text,
            "Russian attacks kill several in Ukraine",
            cfg=CFG["summarization"],
        )
        joined = " ".join(r["text"] for r in (kept or []))
        assert stats["rejected"] is None
        assert "oil refinery" in joined
        assert "Ukraine" in joined

    def test_relevant_unquoted_headline_link_kept(self):
        # Same-event context survives the relevance filter.
        kept, _ = self._summary([])
        joined = " ".join(r["text"] for r in (kept or []))
        assert "36 square miles" in joined
        assert "Canada" in joined


# ---------------------------------------------------------------------------
# 13. Final content-quality pass (dry-run audit fixes)
# ---------------------------------------------------------------------------


class TestHeadlineBodyCoherence:
    """The composed body must actually explain the headline's main
    event.  A live-blog headline whose body belongs to a different
    story in the blog is rejected, never repaired."""

    def test_headline_topic_a_body_topic_b_rejected(self):
        # Audit case: "... business live" headline about US bond
        # yields, body about Brent crude only.
        kept, stats = summarize_rows(
            [
                row(
                    "Brent crude oil prices rose by 2 percent on "
                    "Tuesday."
                ),
                row(
                    "Traders are watching the next Opec meeting "
                    "for supply signals."
                ),
            ],
            (
                "Brent crude oil prices rose by 2 percent on "
                "Tuesday. Traders are watching the next Opec "
                "meeting for supply signals."
            ),
            (
                "US long-term borrowing costs rise to 25-year "
                "high, as inflation fears hit bond sale - "
                "business live"
            ),
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "coherence"

    def test_liveblog_headline_unrelated_body_rejected(self):
        # A multi-topic live-blog headline with a body from a
        # different story in the same blog.
        kept, stats = summarize_rows(
            [
                row(
                    "Brent crude fell below 70 dollars a barrel "
                    "on Tuesday."
                ),
                row(
                    "The yen weakened to a 38-year low against "
                    "the dollar."
                ),
            ],
            (
                "Brent crude fell below 70 dollars a barrel on "
                "Tuesday. The yen weakened to a 38-year low "
                "against the dollar."
            ),
            "Markets live: bond yields surge as inflation fears grow",
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "coherence"

    def test_headline_body_same_event_accepted(self):
        kept, stats = summarize_rows(
            [
                row(
                    "The wildfire has grown to cover 36 square "
                    "miles."
                ),
                row(
                    "Officials said 20,000 residents have been "
                    "evacuated so far."
                ),
            ],
            (
                "The wildfire has grown to cover 36 square miles. "
                "Officials said 20,000 residents have been "
                "evacuated so far."
            ),
            (
                "State of emergency declared as fast-moving "
                "Canada wildfire doubles in size"
            ),
            cfg=CFG["summarization"],
        )
        assert kept is not None
        assert stats["rejected"] is None


class TestLiveBlogBoilerplate:
    """Live-blog navigation prompts and follow-me text must never
    enter a summary."""

    def test_liveblog_follow_prompts_stripped(self):
        from src.telegram_briefing import clean_sentence_text
        assert clean_sentence_text(
            "Follow our liveblog for the latest updates."
        ) == ""
        assert clean_sentence_text(
            "Follow live updates for the latest developments."
        ) == ""
        assert clean_sentence_text(
            "Get the latest updates."
        ) == ""
        assert clean_sentence_text(
            "Liveblog navigation"
        ) == ""

    def test_liveblog_follow_prompt_dropped_from_summary(self):
        # Audit case: the USS Lincoln story ended with "Follow our
        # liveblog for the latest updates." - the prompt must never
        # appear in the final body.
        texts = [
            "Reports of declining mental health have been "
            "flagged by US sailors aboard the USS Lincoln.",
            "The ship has so far spent a record 260 "
            "uninterrupted days at sea.",
            "Follow our liveblog for the latest updates.",
        ]
        kept, stats = summarize_rows(
            [
                row(t) for t in texts
            ],
            " ".join(texts),
            "US sailors report dire conditions aboard aircraft carrier",
            cfg=CFG["summarization"],
        )
        assert kept is not None
        assert not any(
            "liveblog" in r["text"] for r in kept
        )


class TestListMarkerChrome:
    """Page/list markers ("- list 3 of 3") and the recommended-
    story lines they prefix must be stripped before summarization."""

    def test_list_marker_fused_with_next_item_stripped(self):
        from src.telegram_briefing import clean_sentence_text
        assert clean_sentence_text(
            "- list 3 of 3Why has North Korea's new satellite "
            "alarmed Seoul?"
        ) == "Why has North Korea's new satellite alarmed Seoul?"
        result = clean_sentence_text(
            "- list 1 of 3North Korea's hackers using AI for "
            "attacks, cybersecurity firm says"
        )
        assert "list 1 of 3" not in result
        assert clean_sentence_text(
            "Recommended Stories"
        ) == ""
        assert clean_sentence_text(
            "list of 3 items"
        ) == ""

    def test_list_marker_leak_never_queued(self):
        kept, stats = summarize_rows(
            [
                row(
                    "- list 3 of 3Why has North Korea's new "
                    "satellite alarmed Seoul?"
                ),
                row(
                    "- list 2 of 3What will Pyongyang do next?"
                ),
            ],
            (
                "- list 3 of 3Why has North Korea's new satellite "
                "alarmed Seoul? - list 2 of 3What will Pyongyang "
                "do next?"
            ),
            "North Korea launches new satellite",
            cfg=CFG["summarization"],
        )
        assert kept is None


class TestFusedSentenceRepair:
    """HTML/line-break extraction damage ("...centre Orangutans
    rescued...") must be reconstructed when safe and rejected
    otherwise - never published corrupted."""

    def test_fused_sentence_with_common_word_evidence_split(self):
        # "orangutans" is used as a common word in the source, so
        # the boundary is a real sentence start and the row is
        # safely reconstructed - never published fused.
        text = (
            "Blaze comes within metres of the rehabilitation "
            "centre Orangutans rescued from traffickers face a "
            "new threat."
        )
        kept, stats = summarize_rows(
            [
                row(text),
                row(
                    "Flames have come within metres of a "
                    "rehabilitation centre in West Kalimantan."
                ),
            ],
            text + " Flames have come within metres of a "
            "rehabilitation centre in West Kalimantan.",
            "Wildfires threaten orangutans rescued from traffickers",
            cfg=CFG["summarization"],
        )
        assert stats["rejected"] is None
        joined = " ".join(r["text"] for r in kept)
        assert "centre Orangutans" not in joined
        assert "face a new threat" in joined

    def test_fused_sentence_without_evidence_dropped(self):
        # "Ponds" is never used as a common word in the source, so
        # the corrupted row is dropped rather than published.
        text = (
            "Farmers are finding new ways to help crops survive "
            "Ponds and collection tanks are being built."
        )
        kept, stats = summarize_rows(
            [
                row(text),
                row(
                    "The drought has brought a series of "
                    "challenges this summer."
                ),
            ],
            text + " The drought has brought a series of challenges "
            "this summer.",
            "UK farmers struggle through drought",
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "insufficient_information"

    def test_fused_after_closing_quote_split(self):
        # Audit case: a standfirst fuses into the lede after a
        # closing quote: "...'digital lifeline' In Taipei, the
        # streets swiftly empty...".
        text = (
            "Taiwan practised cutting off internet for the first "
            "time amid concerns about the safety of the undersea "
            "cables that form its 'digital lifeline' In Taipei, "
            "the streets swiftly empty as air raid sirens ring "
            "out."
        )
        kept, stats = summarize_rows(
            [
                row(text),
                row(
                    "Police officers usher stragglers into "
                    "shelters."
                ),
            ],
            text + " Police officers usher stragglers into "
            "shelters.",
            "Taiwan simulates an internet blackout",
            cfg=CFG["summarization"],
        )
        assert stats["rejected"] is None
        joined = " ".join(r["text"] for r in kept)
        assert "lifeline' In" not in joined

    def test_fused_after_apostrophe_quote_split(self):
        # Audit case: "...taxing gas exports' Cross-benchers have
        # slammed..." - a closing quote ends the fragment and a
        # new sentence starts after it.  The boundary is
        # unambiguous (the quote is the signal) and is split.
        text = (
            "Greens say Labor's reform is set to 'rip supports "
            "away' Cross-benchers have slammed the overhaul."
        )
        kept, stats = summarize_rows(
            [
                row(text),
                row(
                    "The report on the NDIS overhaul was released "
                    "on Friday afternoon."
                ),
            ],
            (
                text + " The report on the NDIS overhaul was "
                "released on Friday afternoon."
            ),
            "Senators slam NDIS overhaul",
            cfg=CFG["summarization"],
        )
        assert stats["rejected"] is None
        joined = " ".join(r["text"] for r in kept)
        assert "away' Cross-benchers" not in joined
        assert "slammed the overhaul" in joined

    def test_fused_reporting_verb_lowercased_not_split(self):
        # Audit case: "...Rightmove says Searches for homes..."
        # fuses after a reporting verb.  The capitalised word is
        # a common noun verified in the source, so the clean
        # reconstruction lowercases it (every word preserved);
        # splitting would orphan a "Rightmove says." fragment.
        text = (
            "Rightmove says Searches for homes for sale jumped "
            "11% in the first week of the year, as buyers "
            "returned to the market."
        )
        kept, stats = summarize_rows(
            [
                row(text),
                row(
                    "The portal said demand had recovered after "
                    "the festive lull."
                ),
            ],
            text + " The portal said demand had recovered after "
            "the festive lull.",
            "Rightmove reports jump in home searches",
            cfg=CFG["summarization"],
        )
        assert stats["rejected"] is None
        joined = " ".join(r["text"] for r in kept)
        assert "Searches" not in joined
        assert "says searches for homes" in joined

    def test_fused_ellipsis_boundary_split(self):
        # Audit case: a live-blog subheading ends with "…" and
        # the next item starts fresh: "...rates?…Professor Costas
        # Milas...".  The ellipsis is the boundary signal and the
        # row is split, never fused.
        text = (
            "But why are 10-year Treasury yields still near the "
            "key 5% level, when the US central bank has begun "
            "cutting rates?…Professor Costas Milas, of the "
            "University of Liverpool, says the yen story is at "
            "least as important as the dollar story"
        )
        kept, stats = summarize_rows(
            [
                row(text),
                row(
                    "US long-term borrowing costs have risen to a "
                    "25-year high."
                ),
            ],
            text + " US long-term borrowing costs have risen to a "
            "25-year high.",
            "US long-term borrowing costs rise to 25-year high",
            cfg=CFG["summarization"],
        )
        # The yen fragment carries no fact tied to the bond-sale
        # headline, so the story is rejected rather than published
        # with a mismatch (fix-1 behaviour).
        assert kept is None
        assert stats["rejected"] == "quality"


class TestQuestionOnlyRejection:
    """A body that only asks questions explains nothing and is
    rejected; the answer is never invented."""

    def test_question_only_summary_rejected(self):
        kept, stats = summarize_rows(
            [
                row(
                    "Why is the Sun's corona millions of degrees "
                    "hotter than its surface?"
                ),
                row(
                    "What does this mean for future solar "
                    "missions?"
                ),
            ],
            (
                "Why is the Sun's corona millions of degrees "
                "hotter than its surface? What does this mean "
                "for future solar missions?"
            ),
            (
                "Indian solar mission's new findings throw light "
                "on enduring Sun mysteries"
            ),
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "question_only"

    def test_fact_free_background_rejected(self):
        # Pure article-introduction text carries no news fact.
        kept, stats = summarize_rows(
            [
                row(
                    "The corona is the outermost layer of the "
                    "sun's atmosphere."
                ),
                row(
                    "The sun is the star at the center of the "
                    "solar system."
                ),
            ],
            (
                "The corona is the outermost layer of the sun's "
                "atmosphere. The sun is the star at the center "
                "of the solar system."
            ),
            "Solar mission reports new findings",
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "no_news_content"


class TestUnattributedQuotes:
    """A quoted body must identify who said it; an unattributed
    quote is rejected, never published with an invented speaker."""

    def test_unattributed_leading_quote_rejected(self):
        # The quote row must survive to the composition stage, so
        # the headline links to it; the quote gate then rejects.
        kept, stats = summarize_rows(
            [
                row('"I feel sick."'),
                row("The journey home took 9 hours."),
            ],
            '"I feel sick." The journey home took 9 hours.',
            "Residents feel sick as the journey home takes 9 hours",
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "unattributed_quote"

    def test_pronoun_only_attribution_rejected(self):
        # "he said" does not identify the speaker.
        kept, stats = summarize_rows(
            [
                row(
                    '"Look, we are working constructively," he said.'
                ),
                row("The talks resume on Monday."),
            ],
            (
                '"Look, we are working constructively," he said. '
                "The talks resume on Monday."
            ),
            (
                "Minister says parties are working constructively "
                "as talks resume"
            ),
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "unattributed_quote"

    def test_unattributed_first_person_fragment_rejected(self):
        # Audit case: a body opening with "I feel sick." - direct
        # speech with no speaker named anywhere.
        kept, stats = summarize_rows(
            [
                row("I feel sick."),
                row(
                    "The widow has said she has been left feeling "
                    "sick with worry."
                ),
            ],
            (
                "I feel sick. The widow has said she has been "
                "left feeling sick with worry."
            ),
            "British widow faces removal from Sweden",
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "unattributed_quote"

    def test_unattributed_spoken_opening_rejected(self):
        # "Look, we're working constructively..." with no speaker.
        kept, stats = summarize_rows(
            [
                row(
                    "Albanese said gambling inducements are 'over "
                    "the top'."
                ),
                row(
                    "Look, we're working constructively across "
                    "the parliament to put in place these reforms."
                ),
            ],
            (
                "Albanese said gambling inducements are 'over the "
                "top'. Look, we're working constructively across "
                "the parliament to put in place these reforms."
            ),
            "Bet365 apologises after children wear gambling logos",
            cfg=CFG["summarization"],
        )
        assert kept is None
        assert stats["rejected"] == "unattributed_quote"

    def test_attributed_first_person_sentence_accepted(self):
        # The source carries both the quoted form and the fronted
        # "The minister said ..." form so the composed sentence
        # verifies; the spoken row names its speaker and survives.
        kept, stats = summarize_rows(
            [
                row(
                    "We are confident the talks will succeed, the "
                    "minister said."
                ),
                row("The talks will resume on Monday."),
            ],
            (
                "We are confident the talks will succeed, the "
                "minister said. The minister said we are confident "
                "the talks will succeed. The talks will resume on "
                "Monday."
            ),
            "Minister says talks will resume",
            cfg=CFG["summarization"],
        )
        assert kept is not None
        assert stats["rejected"] is None

    def test_attributed_quote_accepted(self):
        kept, stats = summarize_rows(
            [
                row(
                    '"We are working constructively", the minister '
                    "said."
                ),
                row("The talks will resume on Monday."),
            ],
            (
                '"We are working constructively", the minister said. '
                "The talks will resume on Monday."
            ),
            (
                "Minister says parties are working constructively "
                "as talks resume"
            ),
            cfg=CFG["summarization"],
        )
        assert kept is not None
        assert stats["rejected"] is None
