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
