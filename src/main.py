import json
import re
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from src.intelligence import classify, verify
from src.event_memory import init_events, decide, mark_queued, purge_expired
from src.formatter import format_story
from src.language import check_item
from src.translator import translate_to_english, TranslationError
from src.source_reliability import is_discovery
from src.collector import collect
from src.quality import quality_check
from src.priority import priority


ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads(
    (ROOT / "config.json").read_text()
)

DB = ROOT / CONFIG["database"]
QUEUE = ROOT / CONFIG["queue_file"]
SOURCE_HEALTH = ROOT / "data" / "source_health.json"
TELEGRAM_QUEUE = ROOT / CONFIG.get(
    "telegram",
    {}
).get(
    "telegram_queue_file",
    "data/telegram_queue.json"
)


def clean(t):
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"<[^>]+>", " ", t or "")
    ).strip()


def sid(u, t):
    return hashlib.sha256(
        (
            u.split("?")[0]
            + "|"
            + t.lower()
        ).encode()
    ).hexdigest()


def _is_newer_update(
    incoming_effective,
    stored_first_seen
):
    """
    Whether the incoming article carries a newer
    published/updated timestamp than the stored record.

    A fresher timestamp means the same URL/title may
    now contain a genuine update, so it should be
    re-examined by event memory instead of being
    skipped as already seen.
    """
    if not incoming_effective:
        return False

    try:
        incoming = datetime.fromisoformat(
            incoming_effective.replace(
                "Z",
                "+00:00"
            )
        )

        stored = datetime.fromisoformat(
            stored_first_seen.replace(
                "Z",
                "+00:00"
            )
        )

        if incoming.tzinfo is None:
            incoming = incoming.replace(
                tzinfo=timezone.utc
            )

        if stored.tzinfo is None:
            stored = stored.replace(
                tzinfo=timezone.utc
            )

        return (
            incoming > stored
        )

    except Exception:
        return False


def db():
    DB.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    c = sqlite3.connect(DB)

    c.execute("""
    CREATE TABLE IF NOT EXISTS stories(
        id TEXT PRIMARY KEY,
        title TEXT,
        url TEXT,
        source TEXT,
        category TEXT,
        summary TEXT,
        score INTEGER,
        confidence TEXT,
        event_id TEXT,
        event_status TEXT,
        first_seen TEXT
    )
    """)

    init_events(c)

    return c


def fetch():
    raw, health = collect(
        CONFIG["feeds"],
        CONFIG.get(
            "max_feed_entries_per_source",
            25
        ),
        12,
        max_age_hours=48,
    )

    SOURCE_HEALTH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    SOURCE_HEALTH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "max_age_hours": 48,
                "sources": health,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    out = []

    for item in raw:
        t = item["title"]
        u = item["url"]
        s = item["summary"]

        out.append({
            "id": sid(u, t),
            "title": t,
            "url": u,
            "source": item["source"],
            "source_category": item["source_category"],
            "primary_source": item["primary_source"],
            "tier": item["tier"],
            "region": item.get("region"),
            "discovery": item.get(
                "discovery",
                False
            ),
            "summary": s,
            "published_at": item.get(
                "published_at"
            ),
            "updated_at": item.get(
                "updated_at"
            ),
            "effective_at": item.get(
                "effective_at"
            ),
        })

    return out


def translate_candidate(x):
    result = translate_to_english(
        x["title"] + "\n\n" + x["summary"]
    )

    parts = result["text"].split(
        "\n",
        1
    )

    x["title"] = clean(
        parts[0]
    )

    x["summary"] = clean(
        parts[1]
        if len(parts) > 1
        else ""
    )

    x["translated_from"] = result.get(
        "detected_language"
    )

    x["translation_endpoint"] = result.get(
        "endpoint"
    )

    x["language_status"] = (
        "TRANSLATED_TO_ENGLISH"
    )

    return x


def telegram_ineligible_sources(feeds):
    """Feed names flagged as non-news ("news": false).

    These feeds are still fetched and stored (event memory,
    deduplication and the main queue keep working), but their
    items never become Telegram candidates.
    """
    return {
        feed["name"]
        for feed in feeds
        if not feed.get("news", True)
    }


def build_telegram_stories(
    candidates,
    telegram_cfg,
    now_dt,
):
    """Group telegram candidates into events and enrich each
    event into ONE telegram story (pure, no I/O).

    Same grouping and briefing steps as before; the only
    difference is the empty-message gate: a story whose
    cleaned summary contains no explanatory sentence beyond
    its headline is rejected and never enters the telegram
    queue. No text is ever invented for rejected stories.
    """
    from src.telegram_briefing import (
        build_briefing,
        group_items,
    )

    groups = group_items(
        candidates
    )

    telegram_stories = []

    for group in groups:

        primary = sorted(
            group,
            key=lambda x: (
                x.get("score", 0)
                or x.get("priority_score", 0)
                or 0,
                int(
                    bool(x.get("primary_source"))
                ),
                -int(x.get("tier", 4)),
            ),
            reverse=True,
        )[0]

        # Article-enriched primaries contribute verbatim
        # article sentences through the SAME aggregation
        # filters (filler, headline paraphrase, dedup,
        # conflict, near-duplicate) as a synthetic group
        # member ranked just below the primary.
        article_sentences = primary.get(
            "article_sentences"
        ) or []

        briefing_group = group

        if len(article_sentences) >= 2:

            article_member = dict(primary)

            article_member["id"] = (
                primary.get("id")
                or primary.get("story_id")
            ) + ":article"

            article_member["story_id"] = (
                article_member["id"]
            )

            article_member["summary"] = " ".join(
                article_sentences
            )

            article_member["score"] = max(
                0,
                (
                    primary.get("score")
                    or primary.get("priority_score")
                    or 0
                ) - 1,
            )

            briefing_group = group + [article_member]

        briefing = build_briefing(
            primary,
            briefing_group,
            int(
                telegram_cfg.get(
                    "just_in_freshness_minutes",
                    15
                )
            ),
            now_dt,
            max_sentences=(
                int(
                    telegram_cfg.get(
                        "max_briefing_sentences",
                        10,
                    )
                )
                if len(article_sentences) >= 2
                else None
            ),
        )

        # Empty-message protection: reject stories whose
        # cleaned summary holds no explanatory sentence
        # beyond the headline (headline-only or pure
        # headline-paraphrase summaries collapse to an
        # empty briefing).
        if not briefing["sentences"]:
            continue

        enriched = dict(primary)

        enriched["story_id"] = (
            primary.get("story_id")
            or primary.get("id")
        )

        # Public editorial fields for the Telegram layer.
        enriched["public_label"] = briefing["label"]
        enriched["headline"] = briefing["headline"]
        enriched["briefing"] = {
            "opening": briefing["opening"],
            "body": briefing["body"],
            "bullets": briefing["bullets"],
            "sentences": briefing["sentences"],
            "source": briefing["source"],
            "corroborating": briefing["corroborating"],
            "url": briefing["url"],
        }

        enriched["group_size"] = len(group)
        enriched["label"] = briefing["label"]

        telegram_stories.append(
            enriched
        )

    return telegram_stories


def main():
    c = db()

    # ---------------------------------------------------------
    # Automatic memory cleanup.
    #
    # Runs on every collection cycle and removes only
    # records whose retention period has elapsed.
    # ---------------------------------------------------------

    purged = purge_expired(
        c,
        story_memory_hours=CONFIG.get(
            "story_memory_hours",
            48
        ),
        memory_hours=CONFIG[
            "event_memory_hours"
        ],
        major_memory_hours=CONFIG[
            "major_event_memory_hours"
        ]
    )

    print(
        "MEMORY CLEANUP:",
        json.dumps(
            purged,
            ensure_ascii=False
        )
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    items = fetch()

    q = []
    held = []

    # Stories that fail quality are rejected and skipped.
    # They are NOT placed into the held/publish pipeline.
    quality_rejected = []

    # Diagnostic list for stories rejected by score.
    below_score_stories = []

    # ---------------------------------------------------------
    # Per-run statistics
    # ---------------------------------------------------------

    run_stats = {
        "fetched": len(items),
        "already_seen": 0,
        "update_candidates": 0,
        "new_candidates": 0,
        "duplicates": 0,
        "below_score": 0,
        "discovery_held": 0,
        "low_confidence_rejected": 0,
        "quality_failed": 0,
        "quality_rejected": 0,
        "translation_held": 0,
        "queued_before_limit": 0,
        "non_news_filtered": 0,
    }

    # ---------------------------------------------------------
    # Process stories
    # ---------------------------------------------------------

    for x in items:

        # -----------------------------------------------------
        # Skip stories already stored in database.
        #
        # EXCEPTION:
        # If the incoming article carries a newer
        # published/updated timestamp than the stored
        # record, treat it as an update candidate and
        # let event memory decide whether the update
        # is meaningful.
        # -----------------------------------------------------

        stored = c.execute(
            "SELECT first_seen FROM stories "
            "WHERE id=?",
            (x["id"],)
        ).fetchone()

        if stored:

            if not _is_newer_update(
                x.get(
                    "effective_at"
                ),
                stored[0]
            ):

                run_stats[
                    "already_seen"
                ] += 1

                continue

            run_stats[
                "update_candidates"
            ] += 1

        run_stats[
            "new_candidates"
        ] += 1

        # -----------------------------------------------------
        # Language
        # -----------------------------------------------------

        x["language_status"] = check_item(x)

        # -----------------------------------------------------
        # Classification
        # -----------------------------------------------------

        x.update(
            classify(
                x["title"],
                x["summary"],
                x["source_category"],
                x
            )
        )

        # -----------------------------------------------------
        # Verification
        # -----------------------------------------------------

        x.update(
            verify(
                x,
                items
            )
        )

        # -----------------------------------------------------
        # Priority
        # -----------------------------------------------------

        x.update(
            priority(x)
        )

        # -----------------------------------------------------
        # Translation
        # -----------------------------------------------------

        if (
            CONFIG.get(
                "english_only",
                True
            )
            and x["language_status"] != "ENGLISH"
        ):

            if (
                not CONFIG.get(
                    "translate_non_english",
                    True
                )
                or x["score"]
                < CONFIG.get(
                    "translation_min_score",
                    55
                )
            ):

                x["hold_reason"] = (
                    "Translation required"
                )

                held.append(x)

                run_stats[
                    "translation_held"
                ] += 1

                continue

            try:
                x = translate_candidate(x)

            except TranslationError as exc:

                x["hold_reason"] = (
                    "Translation unavailable"
                )

                x["translation_error"] = str(
                    exc
                )

                held.append(x)

                run_stats[
                    "translation_held"
                ] += 1

                continue

            # Re-classify after translation.

            x.update(
                classify(
                    x["title"],
                    x["summary"],
                    x["source_category"],
                    x
                )
            )

            x.update(
                priority(x)
            )

        # -----------------------------------------------------
        # Event memory
        # -----------------------------------------------------

        status, eid, _ = decide(
            c,
            x,
            CONFIG["event_memory_hours"],
            CONFIG["major_event_memory_hours"]
        )

        x["event_status"] = status
        x["event_id"] = eid

        # -----------------------------------------------------
        # Store story
        #
        # IMPORTANT:
        # Store the story BEFORE quality checking.
        #
        # This means a rejected quality story is remembered
        # and will not be treated as a brand-new story again.
        # -----------------------------------------------------

        c.execute(
            "INSERT OR REPLACE INTO stories VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (
                x["id"],
                x["title"],
                x["url"],
                x["source"],
                x["category"],
                x["summary"],
                x["score"],
                x["confidence"],
                eid,
                status,
                now,
            )
        )

        # -----------------------------------------------------
        # Duplicate
        # -----------------------------------------------------

        if status == "DUPLICATE":

            run_stats[
                "duplicates"
            ] += 1

            continue

        # -----------------------------------------------------
        # Minimum score
        # -----------------------------------------------------

        min_score = (
            CONFIG["discovery_min_score"]
            if is_discovery(x)
            else CONFIG["min_score_to_queue"]
        )

        if x["score"] < min_score:

            run_stats[
                "below_score"
            ] += 1

            below_score_stories.append({
                "title": x.get(
                    "title",
                    ""
                ),
                "score": x.get(
                    "score",
                    0
                ),
                "required_score": min_score,
                "category": x.get(
                    "category",
                    ""
                ),
                "source": x.get(
                    "source",
                    ""
                ),
                "confidence": x.get(
                    "confidence",
                    ""
                ),
                "priority_level": x.get(
                    "priority_level",
                    ""
                ),
            })

            continue

        # -----------------------------------------------------
        # Discovery verification
        # -----------------------------------------------------

        if (
            is_discovery(x)
            and x.get(
                "strong_corroboration",
                0
            ) < 1
            and not x.get(
                "primary_source"
            )
        ):

            x["hold_reason"] = (
                "Discovery lead awaiting "
                "independent confirmation"
            )

            held.append(x)

            run_stats[
                "discovery_held"
            ] += 1

            continue

        # -----------------------------------------------------
        # Low-confidence rejection
        # -----------------------------------------------------

        if (
            x["confidence"] == "low"
            and x["tier"] >= 3
        ):

            run_stats[
                "low_confidence_rejected"
            ] += 1

            continue

        # -----------------------------------------------------
        # Formatting
        # -----------------------------------------------------

        x.update(
            format_story(
                x,
                CONFIG["breaking_min_score"]
            )
        )

        # -----------------------------------------------------
        # Quality
        # -----------------------------------------------------

        x.update(
            quality_check(x)
        )

        # -----------------------------------------------------
        # QUALITY FAILURE
        #
        # IMPORTANT FIX:
        #
        # DO NOT put failed stories into "held".
        #
        # They are rejected and skipped.
        # Good stories continue processing.
        # -----------------------------------------------------

        if not x["quality_pass"]:

            run_stats[
                "quality_failed"
            ] += 1

            run_stats[
                "quality_rejected"
            ] += 1

            quality_rejected.append({
                "id": x.get("id"),
                "title": x.get("title"),
                "source": x.get("source"),
                "quality_errors": x.get(
                    "quality_errors",
                    []
                ),
            })

            print(
                "QUALITY REJECTED:",
                x.get("title", ""),
                "|",
                x.get(
                    "quality_errors",
                    []
                )
            )

            # IMPORTANT:
            # No held.append(x)
            # No q.append(x)
            # No mark_queued()
            #
            # Simply move to the next story.
            continue

        # -----------------------------------------------------
        # Queue only quality-passed stories.
        # -----------------------------------------------------

        q.append(x)

        mark_queued(
            c,
            eid
        )

        run_stats[
            "queued_before_limit"
        ] += 1

    # ---------------------------------------------------------
    # Commit database changes
    # ---------------------------------------------------------

    c.commit()
    c.close()

    # ---------------------------------------------------------
    # Sort queue by priority
    # ---------------------------------------------------------

    q.sort(
        key=lambda x: (
            x.get(
                "priority_level"
            ) == "IMMEDIATE",

            x.get(
                "priority_score",
                0
            ),

            x["event_status"] == "UPDATE",

            x["confidence"] == "high",
        ),
        reverse=True,
    )

    # ---------------------------------------------------------
    # Telegram candidate output
    #
    # ADDITIVE ONLY:
    # Does not alter queue.json, deduplication, event memory,
    # scoring, or any existing pipeline behavior.
    #
    # Captures all quality-passed stories BEFORE the
    # max_stories_per_run slice, applies the Telegram
    # freshness window, and writes a separate bounded
    # candidate file for the Telegram scheduler.
    # ---------------------------------------------------------

    telegram_cfg = CONFIG.get(
        "telegram",
        {}
    )

    freshness_hours = float(
        telegram_cfg.get(
            "freshness_hours",
            6
        )
    )

    max_candidates = int(
        telegram_cfg.get(
            "max_candidates",
            50
        )
    )

    now_dt = datetime.now(
        timezone.utc
    )

    no_news_sources = telegram_ineligible_sources(
        CONFIG["feeds"]
    )

    telegram_candidates = []

    for x in q:

        # Per-feed editorial gate: feeds flagged "news": false
        # (press releases, conference schedules, advisory
        # catalogs) never produce Telegram posts. Items are
        # still stored in the database and keep flowing through
        # the main queue pipeline.
        if (
            x.get("source")
            in no_news_sources
        ):

            run_stats[
                "non_news_filtered"
            ] += 1

            continue

        effective = x.get(
            "effective_at"
        )

        if not effective:
            continue

        try:

            effective_dt = datetime.fromisoformat(
                effective.replace(
                    "Z",
                    "+00:00"
                )
            )

            if effective_dt.tzinfo is None:

                effective_dt = effective_dt.replace(
                    tzinfo=timezone.utc
                )

            age_seconds = (
                now_dt - effective_dt
            ).total_seconds()

        except Exception:

            continue

        # Only fresh stories are Telegram candidates.
        if (
            0
            <= age_seconds
            <= freshness_hours * 3600
        ):

            candidate = dict(x)

            # Normalize the dedup key: the pipeline stores
            # it as "id"; the telegram layer keys off
            # "story_id".
            candidate["story_id"] = (
                candidate.get("story_id")
                or candidate.get("id")
            )

            telegram_candidates.append(
                candidate
            )

    telegram_candidates = (
        telegram_candidates[:max_candidates]
    )

    # ---------------------------------------------------------
    # Article extraction enrichment
    #
    # ADDITIVE ONLY:
    # For important thin stories (fewer than two useful RSS
    # sentences) the original article is fetched and its
    # verbatim sentences flow through the same briefing
    # filters.  Results are cached in the database; every
    # failure degrades to the plain RSS briefing.  The
    # pipeline never fails because of extraction.
    # ---------------------------------------------------------

    article_extraction_stats = {}

    try:

        from src.article_extractor import (
            ArticleCache,
            enrich_thin_stories,
        )

        telegram_candidates, article_extraction_stats = (
            enrich_thin_stories(
                telegram_candidates,
                CONFIG,
                now_dt,
                cache=ArticleCache(DB),
            )
        )

    except Exception as exc:

        article_extraction_stats = {
            "error": str(exc),
        }

    run_stats[
        "article_extraction"
    ] = article_extraction_stats

    # ---------------------------------------------------------
    # Briefing enrichment
    #
    # ADDITIVE ONLY:
    # Groups same-run candidates into events (conservative),
    # builds a provenance-tracked briefing per event, and
    # emits ONE enriched candidate per event. Multiple
    # stories of the same event therefore produce a single
    # Telegram post instead of duplicates.
    #
    # Empty-message gate:
    # Stories whose cleaned summary adds no explanatory
    # sentence beyond the headline are rejected here and
    # never enter the Telegram queue.
    # ---------------------------------------------------------

    telegram_cfg_briefing = dict(telegram_cfg)

    telegram_cfg_briefing[
        "max_briefing_sentences"
    ] = int(
        CONFIG.get(
            "article_extraction",
            {},
        ).get(
            "max_briefing_sentences",
            10,
        )
    )

    telegram_stories = build_telegram_stories(
        telegram_candidates,
        telegram_cfg_briefing,
        now_dt,
    )

    TELEGRAM_QUEUE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    TELEGRAM_QUEUE.write_text(
        json.dumps(
            {
                "generated_at": now,
                "count": len(
                    telegram_stories
                ),
                "stories": telegram_stories,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    # ---------------------------------------------------------
    # Apply maximum stories per run
    # ---------------------------------------------------------

    q = q[
        :CONFIG["max_stories_per_run"]
    ]

    # ---------------------------------------------------------
    # Write queue
    #
    # IMPORTANT:
    # quality-rejected stories are NOT written to "held".
    # ---------------------------------------------------------

    QUEUE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    QUEUE.write_text(
        json.dumps(
            {
                "generated_at": now,
                "count": len(q),
                "held_count": len(held),
                "quality_rejected_count": len(
                    quality_rejected
                ),
                "stories": q,
                "held": held[:30],
                "quality_rejected": quality_rejected[:30],
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    # ---------------------------------------------------------
    # Print queued stories
    # ---------------------------------------------------------

    for x in q:
        print(
            f"[{x['priority_level']} | "
            f"{x['event_status']} | "
            f"{x['format']} | "
            f"{x.get('region')} | "
            f"{x['priority_score']}] "
            f"{x['title']}"
        )

    # ---------------------------------------------------------
    # Basic run results
    # ---------------------------------------------------------

    print(
        "Collected recent stories:",
        len(items)
    )

    print(
        "Queued:",
        len(q)
    )

    print(
        "Held:",
        len(held)
    )

    print(
        "Quality rejected:",
        len(quality_rejected)
    )

    # ---------------------------------------------------------
    # Per-run diagnostics
    # ---------------------------------------------------------

    print("\nRUN STATS")

    print(
        json.dumps(
            run_stats,
            indent=2,
            ensure_ascii=False,
        )
    )

    # ---------------------------------------------------------
    # Quality rejection diagnostics
    # ---------------------------------------------------------

    if quality_rejected:

        print(
            "\nQUALITY REJECTED STORIES"
        )

        for story in quality_rejected:

            print(
                f"[{story.get('source', '')}] "
                f"{story.get('title', '')}"
            )

            print(
                "  Errors:",
                ", ".join(
                    story.get(
                        "quality_errors",
                        []
                    )
                )
            )

    # ---------------------------------------------------------
    # Below-score diagnostics
    # ---------------------------------------------------------

    if below_score_stories:

        print(
            "\nBELOW SCORE STORIES"
        )

        for story in below_score_stories:

            print(
                f"[{story['score']}/"
                f"{story['required_score']}] "
                f"{story['category']} | "
                f"{story['confidence']} | "
                f"{story['source']} | "
                f"{story['title']}"
            )


if __name__ == "__main__":
    main()
