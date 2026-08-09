import hashlib
import re
from datetime import datetime, timezone, timedelta


def init_events(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS events(
        event_id TEXT PRIMARY KEY,
        canonical_title TEXT NOT NULL,
        category TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        major INTEGER DEFAULT 0,
        queued_count INTEGER DEFAULT 0,
        canonical_summary TEXT DEFAULT ''
    )
    """)

    # Upgrade existing databases created before
    # canonical_summary was added.
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(events)"
        ).fetchall()
    }

    if "canonical_summary" not in columns:
        conn.execute(
            """
            ALTER TABLE events
            ADD COLUMN canonical_summary TEXT DEFAULT ''
            """
        )

    conn.commit()


def _tokens(text):
    return set(
        re.findall(
            r"[a-z0-9][a-z0-9'-]*",
            (text or "").lower()
        )
    )


def _sim(a, b):
    aa = _tokens(a)
    bb = _tokens(b)

    if not aa or not bb:
        return 0.0

    return len(aa & bb) / max(
        1,
        len(aa | bb)
    )


def _new_id(title):
    return hashlib.sha256(
        title.strip().lower().encode()
    ).hexdigest()[:24]


def _same_event_source(
    conn,
    event_id,
    source
):
    """
    Check whether this source has already produced
    a story belonging to this event.
    """
    if not source:
        return False

    row = conn.execute(
        """
        SELECT 1
        FROM stories
        WHERE event_id=? AND source=?
        LIMIT 1
        """,
        (
            event_id,
            source
        )
    ).fetchone()

    return row is not None


def _meaningful_update(
    item,
    canonical_title,
    canonical_summary
):
    """
    Determine whether the incoming story contains
    meaningful new information compared with the
    existing event coverage.

    This intentionally uses a conservative rule.
    """
    new_title = item.get(
        "title",
        ""
    )

    new_summary = item.get(
        "summary",
        ""
    )

    title_similarity = _sim(
        new_title,
        canonical_title
    )

    summary_similarity = _sim(
        new_summary,
        canonical_summary
    )

    # A substantially different title is potentially
    # a meaningful development.
    if title_similarity < 0.55:
        return True

    # If there is useful summary information and the
    # summary differs substantially, treat it as an update.
    if (
        new_summary
        and canonical_summary
        and summary_similarity < 0.55
    ):
        return True

    return False


def decide(
    conn,
    item,
    memory_hours=48,
    major_memory_hours=168
):
    now = datetime.now(
        timezone.utc
    )

    rows = conn.execute(
        """
        SELECT
            event_id,
            canonical_title,
            first_seen,
            last_seen,
            major,
            queued_count,
            canonical_summary
        FROM events
        """
    ).fetchall()

    best = None
    best_sim = 0.0

    for row in rows:
        (
            event_id,
            canonical,
            first_seen,
            last_seen,
            major,
            queued_count,
            canonical_summary,
        ) = row

        try:
            last = datetime.fromisoformat(
                last_seen.replace(
                    "Z",
                    "+00:00"
                )
            )
        except Exception:
            continue

        hours = (
            major_memory_hours
            if major
            else memory_hours
        )

        if (
            now - last
        ).total_seconds() > hours * 3600:
            continue

        sim = _sim(
            item.get("title", ""),
            canonical
        )

        if sim > best_sim:
            best_sim = sim
            best = row

    # ---------------------------------------------------------
    # No sufficiently similar recent event.
    # ---------------------------------------------------------
    if (
        not best
        or best_sim < 0.42
    ):
        event_id = _new_id(
            item.get("title", "")
            + "|"
            + item.get("source", "")
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO events(
                event_id,
                canonical_title,
                category,
                first_seen,
                last_seen,
                major,
                queued_count,
                canonical_summary
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                item.get(
                    "title",
                    ""
                ),
                item.get(
                    "category",
                    "world"
                ),
                now.isoformat(),
                now.isoformat(),
                int(
                    item.get(
                        "priority_score",
                        item.get(
                            "score",
                            0
                        )
                    ) >= 85
                ),
                0,
                item.get(
                    "summary",
                    ""
                ),
            )
        )

        return (
            "NEW",
            event_id,
            1.0
        )

    (
        event_id,
        canonical,
        first_seen,
        last_seen,
        major,
        queued_count,
        canonical_summary,
    ) = best

    source = item.get(
        "source",
        ""
    )

    # ---------------------------------------------------------
    # Same source + same event.
    # ---------------------------------------------------------
    if _same_event_source(
        conn,
        event_id,
        source
    ):
        conn.execute(
            """
            UPDATE events
            SET last_seen=?,
                major=MAX(major,?)
            WHERE event_id=?
            """,
            (
                now.isoformat(),
                int(
                    item.get(
                        "priority_score",
                        item.get(
                            "score",
                            0
                        )
                    ) >= 85
                ),
                event_id,
            )
        )

        return (
            "DUPLICATE",
            event_id,
            best_sim
        )

    # ---------------------------------------------------------
    # Different source covering the same event.
    #
    # Only call it UPDATE if the incoming report contains
    # meaningfully different information.
    # ---------------------------------------------------------
    if _meaningful_update(
        item,
        canonical,
        canonical_summary
    ):
        conn.execute(
            """
            UPDATE events
            SET
                last_seen=?,
                major=MAX(major,?),
                canonical_title=?,
                canonical_summary=?
            WHERE event_id=?
            """,
            (
                now.isoformat(),
                int(
                    item.get(
                        "priority_score",
                        item.get(
                            "score",
                            0
                        )
                    ) >= 85
                ),
                item.get(
                    "title",
                    canonical
                ),
                item.get(
                    "summary",
                    canonical_summary
                ),
                event_id,
            )
        )

        return (
            "UPDATE",
            event_id,
            best_sim
        )

    # ---------------------------------------------------------
    # Same event, different source, but no meaningful
    # new information.
    #
    # Do not repost it.
    # ---------------------------------------------------------
    conn.execute(
        """
        UPDATE events
        SET
            last_seen=?,
            major=MAX(major,?)
        WHERE event_id=?
        """,
        (
            now.isoformat(),
            int(
                item.get(
                    "priority_score",
                    item.get(
                        "score",
                        0
                    )
                ) >= 85
            ),
            event_id,
        )
    )

    return (
        "DUPLICATE",
        event_id,
        best_sim
    )


def mark_queued(
    conn,
    event_id
):
    conn.execute(
        """
        UPDATE events
        SET queued_count=queued_count+1
        WHERE event_id=?
        """,
        (event_id,)
    )


def purge_expired(
    conn,
    story_memory_hours=48,
    memory_hours=48,
    major_memory_hours=168
):
    """
    Delete only records whose retention period has elapsed.

    - Individual stories expire after story_memory_hours.
    - Normal events expire after memory_hours.
    - Major events expire after major_memory_hours.

    Timestamp-based, idempotent, and safe to run on
    every collection cycle (including every 5 minutes
    in GitHub Actions). Active records are never touched.
    """
    now = datetime.now(
        timezone.utc
    )

    story_cutoff = (
        now - timedelta(
            hours=story_memory_hours
        )
    ).isoformat()

    event_cutoff = (
        now - timedelta(
            hours=memory_hours
        )
    ).isoformat()

    major_cutoff = (
        now - timedelta(
            hours=major_memory_hours
        )
    ).isoformat()

    stories_expired = conn.execute(
        """
        DELETE FROM stories
        WHERE first_seen < ?
        """,
        (story_cutoff,)
    ).rowcount

    normal_events_expired = conn.execute(
        """
        DELETE FROM events
        WHERE major=0 AND last_seen < ?
        """,
        (event_cutoff,)
    ).rowcount

    major_events_expired = conn.execute(
        """
        DELETE FROM events
        WHERE major=1 AND last_seen < ?
        """,
        (major_cutoff,)
    ).rowcount

    conn.commit()

    return {
        "stories_expired": stories_expired,
        "normal_events_expired": normal_events_expired,
        "major_events_expired": major_events_expired,
    }
