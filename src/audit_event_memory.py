"""Automated false-merge audit for cross-run event memory.

Scans the events table of a news database and reports every
event that accumulated 2+ stories, with the reason each story
attached, the shared signals, the semantic score, and the
event's type/location/entities - then flags suspicious clusters
(different countries, near-zero semantic links, unusually broad
canonical state, many stories in one event).

Run with:

    .venv/bin/python -m src.audit_event_memory [db_path] [--json]

db_path defaults to data/news.db.  The audit is read-only; it
never modifies the database and never calls decide().
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict

from src.event_memory import (
    _match_rule,
    _match_score,
    _signals_text,
    _state_from_text,
)

SUSPICIOUS_MAX_STORIES = 6
SUSPICIOUS_MAX_SEMANTIC = 0.12
SUSPICIOUS_MAX_TOKENS = 24


def load_events(conn):
    rows = conn.execute(
        """
        SELECT
            event_id, canonical_title, category, first_seen,
            last_seen, major, queued_count, canonical_summary,
            canonical_state
        FROM events
        ORDER BY first_seen
        """
    ).fetchall()
    return rows


def load_stories(conn):
    rows = conn.execute(
        """
        SELECT id, title, source, category, summary, event_id,
               event_status, first_seen
        FROM stories
        ORDER BY first_seen, rowid
        """
    ).fetchall()
    by_event = defaultdict(list)
    for r in rows:
        by_event[r[5]].append(r)
    return by_event


def story_text(title, summary):
    return (title or "") + " " + (summary or "")


def audit(conn):
    events = load_events(conn)
    stories_by_event = load_stories(conn)

    report = []
    flags = []

    for ev in events:
        (
            event_id,
            canonical_title,
            category,
            first_seen,
            last_seen,
            major,
            queued_count,
            canonical_summary,
            state_raw,
        ) = ev

        state = {}
        if state_raw:
            try:
                state = json.loads(state_raw)
            except (TypeError, ValueError):
                state = {}

        identity = state.get("identity")
        if not isinstance(identity, dict):
            identity = _state_from_text(
                canonical_title,
                canonical_summary,
            ).get("identity") or {}

        stories = stories_by_event.get(event_id, [])
        if len(stories) < 2:
            continue

        entry = {
            "event_id": event_id,
            "canonical_title": canonical_title,
            "category": category or identity.get("category"),
            "major": bool(major),
            "queued_count": queued_count,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "event_type": sorted(
                set(identity.get("core_words") or [])
            ),
            "locations": sorted(set(identity.get("locations") or [])),
            "entities": sorted(set(identity.get("entities") or [])),
            "identity_title": identity.get("title") or canonical_title,
            "stories": [],
            "flags": [],
        }

        identity_locations = set(identity.get("locations") or [])

        for s in stories:
            (
                story_id,
                title,
                source,
                story_cat,
                summary,
                _eid,
                event_status,
                first_seen_story,
            ) = s

            signals = _signals_text(title, summary)
            m = _match_score(signals, identity)
            reason = _match_rule(signals, m)

            shared = {
                k: sorted(v)
                for k, v in (
                    ("entities", m["shared_entities"]),
                    ("locations", m["shared_locations"]),
                    ("actions", m["shared_actions"]),
                    ("core", m["shared_core"]),
                    ("numbers", m["shared_numbers"]),
                    ("impact", m["shared_impact"]),
                    ("distinctive", m["distinctive"]),
                )
            }
            story_locations = signals["locations"]

            entry["stories"].append(
                {
                    "title": title,
                    "source": source,
                    "status": event_status,
                    "reason": reason,
                    "semantic": round(m["semantic"], 3),
                    "score": round(m["score"], 2),
                    "shared": shared,
                    "story_locations": sorted(story_locations),
                    "dev": signals["dev"],
                    "reaction": signals["reaction"],
                }
            )

        # ---- cluster-level suspicion flags ----
        attached = [
            st for st in entry["stories"] if st["status"] != "NEW"
        ]
        if len(stories) > SUSPICIOUS_MAX_STORIES:
            entry["flags"].append(
                f"many_stories({len(stories)})"
            )

        for st in attached:
            # Attached story that names a place the identity never
            # mentioned - is it really the same event?
            if identity_locations and st["story_locations"]:
                novel = (
                    set(st["story_locations"]) - identity_locations
                )
                if novel and not (
                    set(st["story_locations"]) & identity_locations
                ):
                    entry["flags"].append(
                        f"location_mismatch({sorted(novel)})"
                    )
            if (
                st["semantic"] < SUSPICIOUS_MAX_SEMANTIC
                and st["reason"] not in (
                    "dev+link", "reaction",
                )
            ):
                entry["flags"].append(
                    f"low_semantic({st['semantic']})"
                )

        state_tokens = len(
            set(identity.get("content_tokens") or [])
        )
        if state_tokens > SUSPICIOUS_MAX_TOKENS:
            entry["flags"].append(f"broad_identity({state_tokens})")

        # A multi-story event whose attached stories never share a
        # location with the identity is a chain-merge suspect.
        attached_locations = [
            frozenset(st["story_locations"])
            for st in attached
            if st["story_locations"]
        ]
        if (
            attached_locations
            and identity_locations
            and all(
                not (loc & identity_locations)
                for loc in attached_locations
            )
        ):
            entry["flags"].append("no_shared_location")

        report.append(entry)

    return report, flags


def print_report(report):
    for entry in report:
        print("=" * 78)
        print(f"EVENT {entry['event_id']}  [{entry['category']}]"
              f"{'  MAJOR' if entry['major'] else ''}"
              f"  queued={entry['queued_count']}")
        print(f"  CANONICAL TITLE: {entry['canonical_title']}")
        if entry.get("flags"):
            print(f"  FLAGS: {', '.join(entry['flags'])}")
        print(f"  EVENT TYPE: {entry['event_type'] or '-'}  "
              f"LOCATION: {entry['locations'] or '-'}")
        print(f"  ENTITIES: {entry['entities'] or '-'}")
        for i, st in enumerate(entry["stories"]):
            print(f"    {i + 1}. [{st['status']}] {st['title']}")
            print(f"       src={st['source']}  reason={st['reason']}  "
                  f"semantic={st['semantic']}  score={st['score']}")
            if st["reason"]:
                shared = st["shared"]
                parts = [
                    f"entities={shared['distinctive'] or '-'}",
                    f"locs={shared['locations'] or '-'}",
                    f"core={shared['core'] or '-'}",
                    f"actions={shared['actions'] or '-'}",
                    f"impact={shared['impact'] or '-'}",
                ]
                print("       shared: " + "  ".join(parts))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="False-merge audit for event memory"
    )
    parser.add_argument(
        "db",
        nargs="?",
        default="data/news.db",
        help="path to the news database (default: data/news.db)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the full report as JSON instead of text",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    report, _flags = audit(conn)
    conn.close()

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        return 0

    flagged = [e for e in report if e.get("flags")]
    print_report(report)
    print("=" * 78)
    print(
        f"{len(report)} multi-story events; "
        f"{len(flagged)} flagged as suspicious."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
