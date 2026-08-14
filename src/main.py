import json
import os
import re
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from src.intelligence import classify, verify
from src.event_memory import init_events, decide, mark_queued, purge_expired
from src.language import check_item
from src.translator import translate_to_english, TranslationError
from src.source_reliability import is_discovery
from src.collector import collect
from src.editorial import editorial_eligibility
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


def atomic_write_json(path, data):
    """Write JSON atomically: temp file + fsync + rename.

    A partially written file can never be observed after an
    interrupted run.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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

    # Website-ready schema: creates both tables with the full
    # column set and upgrades older databases in place.
    from src.storage import init_schema

    init_schema(c)
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

    atomic_write_json(
        SOURCE_HEALTH,
        {
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "max_age_hours": 48,
            "sources": health,
        },
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
    summarization_stats=None,
):
    """Group telegram candidates into events and enrich each
    event into ONE telegram story (pure, no I/O).

    Same grouping and briefing steps as before, then the
    source-grounded summarizer composes each story's final
    2-4 sentence summary (article text primary when article
    extraction is available, RSS summary otherwise), verifies
    it against the source, checks headline/body consistency,
    and quality-checks it.  A story whose source material
    holds fewer than two genuinely useful sentences is
    rejected and never enters the telegram queue.  No text is
    ever invented for rejected stories.
    """
    from src.telegram_briefing import (
        build_briefing,
        group_items,
    )
    from src.telegram_summarizer import (
        ARTICLE_ITEM_SUFFIX,
        summarize_rows,
    )

    if summarization_stats is not None:
        summarization_stats.update(
            {
                "stories_considered": 0,
                "summarized": 0,
                "article_source": 0,
                "rss_source": 0,
                "rejected_insufficient": 0,
                "rejected_verification": 0,
                "rejected_quality": 0,
                "rejected_consistency": 0,
                "sentences_composed": 0,
                "sentences_verify_dropped": 0,
                "sentences_quality_dropped": 0,
                "problems": [],
            }
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

        article_item_ids = set()

        briefing_group = group

        if len(article_sentences) >= 2:

            article_member = dict(primary)

            article_member["id"] = (
                primary.get("id")
                or primary.get("story_id")
            ) + ARTICLE_ITEM_SUFFIX

            article_member["story_id"] = (
                article_member["id"]
            )

            article_member["summary"] = " ".join(
                article_sentences
            )

            # The article is the primary source: it outranks
            # the RSS members so its sentences come first and
            # a materially conflicting RSS sentence is dropped
            # in the aggregation step rather than the article
            # sentence.
            article_member["score"] = (
                primary.get("score")
                or primary.get("priority_score")
                or 0
            ) + 1

            briefing_group = group + [article_member]

            article_item_ids.add(article_member["id"])

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
            if summarization_stats is not None:
                summarization_stats[
                    "rejected_insufficient"
                ] += 1
            continue

        # Source material for verification: article text when
        # available, plus every group member's RSS summary.
        source_parts = []

        if len(article_sentences) >= 2:
            source_parts.extend(article_sentences)

        for member in group:
            summary = member.get("summary")
            if summary:
                source_parts.append(summary)

        summarization_cfg = telegram_cfg.get(
            "summarization",
            {},
        )

        if summarization_stats is not None:
            summarization_stats[
                "stories_considered"
            ] += 1

        summary_rows, summary_stats = summarize_rows(
            briefing["sentences"],
            " ".join(source_parts),
            briefing["headline"],
            article_item_ids=article_item_ids,
            cfg=summarization_cfg,
        )

        if summary_rows is None:
            if summarization_stats is not None:
                reason = summary_stats.get(
                    "rejected"
                ) or "insufficient_information"
                if reason == "verification":
                    summarization_stats[
                        "rejected_verification"
                    ] += 1
                elif reason == "quality":
                    summarization_stats[
                        "rejected_quality"
                    ] += 1
                elif reason in (
                    "consistency",
                    "coherence",
                ):
                    summarization_stats[
                        "rejected_consistency"
                    ] += 1
                elif reason in (
                    "question_only",
                    "no_news_content",
                    "unattributed_quote",
                ):
                    summarization_stats[
                        "rejected_quality"
                    ] += 1
                else:
                    summarization_stats[
                        "rejected_insufficient"
                    ] += 1
            continue

        if summarization_stats is not None:
            summarization_stats["summarized"] += 1
            if len(article_sentences) >= 2:
                summarization_stats["article_source"] += 1
            else:
                summarization_stats["rss_source"] += 1
            summarization_stats["sentences_composed"] += len(
                summary_rows
            )
            summarization_stats[
                "sentences_verify_dropped"
            ] += len(summary_stats["verify_problems"])
            summarization_stats[
                "sentences_quality_dropped"
            ] += len(summary_stats["quality_problems"])
            for problem in summary_stats["verify_problems"]:
                summarization_stats["problems"].append(
                    {
                        "story": primary.get("story_id", "?")[:8],
                        "stage": "verify",
                        "text": problem.get("text"),
                        "problems": problem.get("problems"),
                    }
                )
            for problem in summary_stats["quality_problems"]:
                summarization_stats["problems"].append(
                    {
                        "story": primary.get("story_id", "?")[:8],
                        "stage": "quality",
                        "text": problem.get("text"),
                        "problems": problem.get("problems"),
                    }
                )

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
            "sentences": summary_rows,
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

    # Stories that fail the editorial eligibility gate are
    # rejected and skipped.  They are NOT placed into the
    # held/publish pipeline.
    editorial_rejected = []

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
        "editorial_rejected": 0,
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
        # Website-ready metadata (storage/observability only)
        #
        # Sector / region / entities are classified from the
        # article's OWN content and persisted for the future
        # website.  They never gate publishing and never feed
        # event matching, dedup or scoring.
        # -----------------------------------------------------

        from src.sectors import classify_sector
        from src.regions import classify_event_region
        from src.event_memory import story_entities

        sector, subsector = classify_sector(
            x["title"],
            x["summary"],
            x.get("source_category"),
        )
        region, subregion = classify_event_region(
            x["title"],
            x["summary"],
        )

        x["sector"] = sector
        x["subsector"] = subsector
        x["region"] = region
        x["subregion"] = subregion
        x["country"] = None
        x["entities"] = story_entities(
            x["title"],
            x["summary"],
        )

        # -----------------------------------------------------
        # Editorial eligibility
        #
        # Content-type gate: product reviews, buying guides,
        # opinion columns, evergreen how-tos and sponsored
        # material are rejected before any further work.
        # -----------------------------------------------------

        reasons = []

        if not editorial_eligibility(x, reasons):
            run_stats["editorial_rejected"] += 1
            editorial_rejected.append({
                "id": x.get("id"),
                "title": x.get("title"),
                "source": x.get("source"),
                "reasons": reasons,
            })
            continue

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

            # Re-run the editorial gate on the translated
            # text (junk that was not visible in the original
            # language is caught here).
            reasons = []
            if not editorial_eligibility(x, reasons):
                run_stats["editorial_rejected"] += 1
                editorial_rejected.append({
                    "id": x.get("id"),
                    "title": x.get("title"),
                    "source": x.get("source"),
                    "reasons": reasons,
                })
                continue

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
        # Store the story before the duplicate/score gates so a
        # rejected story is remembered and never treated as a
        # brand-new story again.
        # -----------------------------------------------------

        # Website-ready story metadata (sector, region, entities,
        # timestamps, verification) computed from the item and
        # persisted alongside the legacy fields.
        from src.storage import story_meta

        meta = story_meta(x, now)

        c.execute(
            """
            INSERT OR REPLACE INTO stories(
                id, title, url, source, category, summary, score,
                confidence, event_id, event_status, first_seen,
                sector, subsector, region, subregion, country,
                entities, published_at, updated_at, event_time,
                last_seen, verification
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
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
                meta["sector"],
                meta["subsector"],
                meta["region"],
                meta["subregion"],
                meta["country"],
                meta["entities"],
                meta["published_at"],
                meta["updated_at"],
                meta["event_time"],
                meta["last_seen"],
                meta["verification"],
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
        # Queue the story for the Telegram pipeline.
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
    # Captures all pipeline stories BEFORE the
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

    telegram_cfg_briefing[
        "summarization"
    ] = CONFIG.get(
        "summarization",
        {},
    )

    telegram_stories = build_telegram_stories(
        telegram_candidates,
        telegram_cfg_briefing,
        now_dt,
        summarization_stats=run_stats.setdefault(
            "summarization",
            {},
        ),
    )

    atomic_write_json(
        TELEGRAM_QUEUE,
        {
            "generated_at": now,
            "count": len(
                telegram_stories
            ),
            "stories": telegram_stories,
        },
    )

    # ---------------------------------------------------------
    # Apply maximum stories per run
    # ---------------------------------------------------------

    q = q[
        :CONFIG["max_stories_per_run"]
    ]

    # ---------------------------------------------------------
    # Write the internal pipeline queue
    #
    # data/queue.json is the INTERNAL pipeline diagnostics
    # queue (queued stories, held items, editorial rejections).
    # The Telegram scheduler reads only
    # data/telegram_queue.json.  See README.
    # ---------------------------------------------------------

    atomic_write_json(
        QUEUE,
        {
            "generated_at": now,
            "count": len(q),
            "held_count": len(held),
            "editorial_rejected_count": len(
                editorial_rejected
            ),
            "stories": q,
            "held": held[:30],
            "editorial_rejected": editorial_rejected[:30],
        },
    )

    # ---------------------------------------------------------
    # Print queued stories
    # ---------------------------------------------------------

    for x in q:
        print(
            f"[{x['priority_level']} | "
            f"{x['event_status']} | "
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
        "Editorial rejected:",
        len(editorial_rejected)
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
    # Editorial rejection diagnostics
    # ---------------------------------------------------------

    if editorial_rejected:

        print(
            "\nEDITORIAL REJECTED STORIES"
        )

        for story in editorial_rejected:

            print(
                f"[{story.get('source', '')}] "
                f"{story.get('title', '')}"
            )

            print(
                "  Reasons:",
                ", ".join(
                    story.get(
                        "reasons",
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
