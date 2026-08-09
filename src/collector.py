import feedparser
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta


def clean(t):
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"<[^>]+>", " ", t or "")
    ).strip()


def parse_entry_time(entry, key):
    value = entry.get(key)

    if value:
        try:
            return datetime(
                *value[:6],
                tzinfo=timezone.utc
            )
        except Exception:
            pass

    return None


def get_entry_times(entry):
    """
    Return both publication and update timestamps.

    Some RSS feeds provide only one of these.
    """
    published_at = parse_entry_time(
        entry,
        "published_parsed"
    )

    updated_at = parse_entry_time(
        entry,
        "updated_parsed"
    )

    return published_at, updated_at


def effective_entry_time(entry):
    """
    Choose the newest trustworthy timestamp available.

    If an article was published yesterday but updated today,
    the recent update keeps it eligible for the pipeline.
    """
    published_at, updated_at = get_entry_times(entry)

    if published_at and updated_at:
        return max(
            published_at,
            updated_at
        )

    return published_at or updated_at


def is_recent(dt, max_age_hours=48):
    if dt is None:
        return False

    age = (
        datetime.now(timezone.utc) - dt
    )

    return (
        timedelta(0)
        <= age
        <= timedelta(hours=max_age_hours)
    )


def fetch_one(
    feed,
    limit=25,
    max_age_hours=48
):
    try:
        parsed = feedparser.parse(
            feed["url"]
        )

        rows = []
        error = None

        if (
            getattr(parsed, "bozo", False)
            and not parsed.entries
        ):
            error = str(
                getattr(
                    parsed,
                    "bozo_exception",
                    "feed parse error"
                )
            )

        for e in parsed.entries[:limit]:

            title = clean(
                e.get("title", "")
            )

            url = e.get(
                "link",
                ""
            )

            summary = clean(
                e.get("summary", "")
                or e.get("description", "")
            )

            published_at, updated_at = (
                get_entry_times(e)
            )

            effective_time = (
                updated_at
                if updated_at
                and (
                    not published_at
                    or updated_at > published_at
                )
                else published_at
            )

            # Only today's or yesterday's material
            # is eligible.
            if not is_recent(
                effective_time,
                max_age_hours
            ):
                continue

            if title and url:

                rows.append({
                    "title": title,
                    "url": url,

                    "source": feed["name"],
                    "source_category": feed["category"],

                    "primary_source": feed.get(
                        "primary",
                        False
                    ),

                    "tier": feed.get(
                        "tier",
                        4
                    ),

                    "region": feed.get(
                        "region"
                    ),

                    "discovery": feed.get(
                        "discovery",
                        False
                    ),

                    "summary": summary[:700],

                    "published_at": (
                        published_at.isoformat()
                        if published_at
                        else None
                    ),

                    "updated_at": (
                        updated_at.isoformat()
                        if updated_at
                        else None
                    ),

                    "effective_at": (
                        effective_time.isoformat()
                        if effective_time
                        else None
                    ),
                })

        return rows, {
            "source": feed["name"],
            "url": feed["url"],
            "status": getattr(
                parsed,
                "status",
                None
            ),
            "entries_seen": len(
                parsed.entries
            ),
            "recent_entries": len(
                rows
            ),
            "error": error,
        }

    except Exception as exc:

        return [], {
            "source": feed["name"],
            "url": feed["url"],
            "status": None,
            "entries_seen": 0,
            "recent_entries": 0,
            "error": str(exc),
        }


def collect(
    feeds,
    limit=25,
    workers=12,
    max_age_hours=48
):
    out = []
    health = []

    with ThreadPoolExecutor(
        max_workers=workers
    ) as pool:

        jobs = [
            pool.submit(
                fetch_one,
                f,
                limit,
                max_age_hours
            )
            for f in feeds
        ]

        for job in as_completed(jobs):
            rows, source_result = (
                job.result()
            )

            out.extend(rows)
            health.append(
                source_result
            )

    return out, health
