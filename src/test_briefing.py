"""Tests for the conservative briefing builder.

Run with:  .venv/bin/python -m pytest src/test_briefing.py -q
"""
from datetime import datetime, timedelta, timezone

from src.telegram_briefing import (
    BREAKING,
    JUST_IN,
    NEWS,
    UPDATE,
    aggregate_sentences,
    build_briefing,
    clean_headline,
    group_items,
    public_label,
    same_event,
)
from src.telegram_formatter import build_message

NOW = datetime.now(timezone.utc)


def item(
    title,
    summary,
    source="BBC World",
    event_id="e1",
    score=76,
    confidence="medium",
    urgency=None,
    category="world",
    primary=False,
    strong=0,
    status="NEW",
    minutes_ago=30,
    tier=2,
    url="https://example.com/a",
    **extra,
):
    base = {
        "id": "id-" + str(abs(hash(title + source))),
        "story_id": "story-" + str(abs(hash(title + source))),
        "item_id": "item-" + str(abs(hash(title + source))),
        "event_id": event_id,
        "title": title,
        "summary": summary,
        "source": source,
        "category": category,
        "score": score,
        "confidence": confidence,
        "urgency_terms": urgency or [],
        "primary_source": primary,
        "strong_corroboration": strong,
        "event_status": status,
        "tier": tier,
        "url": url,
        "effective_at": (
            NOW - timedelta(minutes=minutes_ago)
        ).isoformat(),
    }
    base.update(extra)
    return base


def bbc():
    return item(
        title="State of emergency declared as fast-moving "
        "Canada wildfire doubles in size",
        summary=(
            'The Bald Range wildfire in British Columbia, still '
            'considered "out of control", has spread over more '
            "than 36 sq miles (95 sq km)."
        ),
        event_id="e-wildfire-1",
        score=76,
        confidence="medium",
        urgency=["wildfire", "state of emergency"],
    )


def aljazeera():
    return item(
        title="Wildfire forces thousands of evacuations "
        "in Western Canada",
        summary=(
            "A fast-moving wildfire has forced more than 20,000 "
            "people to evacuate parts of British Columbia's "
            "Okanagan region."
        ),
        source="Al Jazeera",
        event_id="e-wildfire-2",
        score=72,
        confidence="medium",
        urgency=["wildfire", "evacuations"],
    )


# ---------------------------------------------------------
# Conservative grouping
# ---------------------------------------------------------


def test_no_merge_on_geography_alone():
    a = item(
        title="Canada wildfire spreads",
        summary="A wildfire in Canada.",
        source="BBC World",
        event_id="ea",
        urgency=["wildfire"],
        region="Canada",
    )
    b = item(
        title="Canada economy grows",
        summary="The Canadian economy grew.",
        source="Reuters",
        event_id="eb",
        urgency=[],
        region="Canada",
    )
    assert not same_event(a, b)
    assert len(group_items([a, b])) == 2


def test_no_merge_on_urgency_alone():
    a = item(
        title="Wildfire forces evacuations in Greece",
        summary="Fires near Athens.",
        source="BBC World",
        event_id="ea",
        urgency=["wildfire"],
    )
    b = item(
        title="Wildfire threatens suburbs of Sydney",
        summary="Fires near Sydney.",
        source="Reuters",
        event_id="eb",
        urgency=["wildfire"],
    )
    assert not same_event(a, b)


def test_merge_on_multiple_signals():
    assert same_event(bbc(), aljazeera())
    assert len(group_items([bbc(), aljazeera()])) == 1


def test_merge_with_different_urgent_categories():
    a = item(
        "State of emergency declared as fast-moving "
        "Canada wildfire doubles in size",
        "The Bald Range wildfire spread over 36 sq miles.",
        event_id="e1",
        category="world",
        urgency=["wildfire", "state of emergency"],
    )
    b = item(
        "Wildfire forces thousands of evacuations "
        "in Western Canada",
        "More than 20,000 people evacuated.",
        source="Al Jazeera",
        event_id="e2",
        category="disaster",
        urgency=["wildfire", "evacuation"],
    )
    assert same_event(a, b)


def test_non_urgent_categories_never_merge():
    a = item(
        "Swimmer wins gold in final race",
        "A swimmer won a race.",
        category="sports",
        urgency=[],
        event_id="e1",
    )
    b = item(
        "Swimmer wins silver in another race",
        "A swimmer placed second.",
        source="Reuters",
        category="sports",
        urgency=[],
        event_id="e2",
    )
    assert not same_event(a, b)


def test_merge_on_same_event_id():
    a = item("Alpha event", "Alpha happened.", event_id="shared")
    b = item("Beta event", "Beta happened.", event_id="shared")
    assert same_event(a, b)


def test_grouping_avoids_chain_drift():
    a = item(
        "Iran strike on Tehran oil refinery",
        "Refinery hit.",
        event_id="ea",
        urgency=["strike"],
    )
    b = item(
        "Tehran oil refinery strike kills workers",
        "Workers killed.",
        event_id="eb",
        urgency=["strike"],
    )
    c = item(
        "Workers strike over wages in Germany",
        "Wage strike.",
        event_id="ec",
        urgency=[],
    )
    groups = group_items([a, b, c])
    assert len(groups) == 2


# ---------------------------------------------------------
# Public labels
# ---------------------------------------------------------


def test_breaking_requires_all_signals():
    x = item(
        "Major earthquake hits coastal city",
        "A strong earthquake struck the coast.",
        score=90,
        confidence="high",
        urgency=["earthquake"],
        primary=True,
        minutes_ago=5,
    )
    assert public_label(x, 15, NOW) == BREAKING


def test_high_priority_alone_is_not_breaking():
    x = item(
        "Company announces new product",
        "A company made an announcement.",
        score=95,
        confidence="high",
        urgency=[],
        primary=True,
        category="business",
        minutes_ago=5,
    )
    assert public_label(x, 15, NOW) != BREAKING


def test_breaking_requires_verification():
    x = item(
        "Major earthquake hits coastal city",
        "A strong earthquake struck the coast.",
        score=90,
        confidence="high",
        urgency=["earthquake"],
        primary=False,
        strong=0,
        minutes_ago=30,
    )
    assert public_label(x, 15, NOW) == NEWS


def test_low_confidence_never_breaking():
    x = item(
        "Major earthquake hits coastal city",
        "A strong earthquake struck the coast.",
        score=90,
        confidence="low",
        urgency=["earthquake"],
        primary=True,
        minutes_ago=5,
    )
    assert public_label(x, 15, NOW) == NEWS


def test_just_in_requires_freshness_and_importance():
    fresh_but_low = item(
        "Minor sports result",
        "A sports team won.",
        score=50,
        confidence="high",
        minutes_ago=2,
        category="sports",
    )
    important_but_old = item(
        "Government announces new policy",
        "A policy was announced.",
        score=80,
        confidence="high",
        minutes_ago=60,
    )
    just_in = item(
        "Government announces new policy",
        "A policy was announced.",
        score=80,
        confidence="high",
        minutes_ago=2,
    )
    assert public_label(fresh_but_low, 15, NOW) == NEWS
    assert public_label(important_but_old, 15, NOW) == NEWS
    assert public_label(just_in, 15, NOW) == JUST_IN


def test_update_label_for_update_status():
    x = item(
        "Wildfire update: crews gain control",
        "Firefighters have made progress.",
        status="UPDATE",
        score=70,
    )
    assert public_label(x, 15, NOW) == UPDATE


# ---------------------------------------------------------
# Aggregation provenance + conflicts
# ---------------------------------------------------------


def test_aggregate_keeps_provenance():
    rows = aggregate_sentences(
        [bbc(), aljazeera()],
        bbc(),
    )
    assert len(rows) == 2
    assert rows[0]["source"] == "BBC World"
    assert rows[1]["source"] == "Al Jazeera"
    assert rows[0]["text"].startswith("The Bald Range")


def test_aggregate_primary_first():
    rows = aggregate_sentences(
        [aljazeera(), bbc()],
        bbc(),
    )
    assert rows[0]["source"] == "BBC World"


def test_aggregate_dedupes_identical_sentences():
    a = item("X event", "Same sentence here.", source="BBC World")
    b = item(
        "X event details",
        "Same sentence here.",
        source="Reuters",
    )
    rows = aggregate_sentences([a, b], a)
    assert len(rows) == 1


def test_conflicting_numbers_are_dropped():
    primary = item(
        "Protest turnout in capital",
        "Officials estimate 20,000 people joined the protest.",
        source="BBC World",
        score=80,
    )
    rival = item(
        "Protest turnout in capital city",
        "Organizers claim 30,000 people joined the protest.",
        source="Reuters",
        score=70,
    )
    rows = aggregate_sentences([primary, rival], primary)
    texts = [r["text"] for r in rows]
    assert len(texts) == 1
    assert "20,000" in texts[0]
    assert "30,000" not in texts[0]


def test_different_units_do_not_conflict():
    primary = item(
        "Fire expands",
        "The fire has spread over 36 sq miles.",
        source="BBC World",
    )
    rival = item(
        "Fire forces evacuations",
        "More than 20,000 people have been evacuated.",
        source="Al Jazeera",
    )
    rows = aggregate_sentences([primary, rival], primary)
    assert len(rows) == 2


def test_no_invented_facts():
    briefing = build_briefing(
        bbc(),
        [bbc(), aljazeera()],
        15,
        NOW,
    )
    all_text = " ".join(
        briefing["opening"]
        + briefing["body"]
    )
    source_texts = (
        bbc()["summary"] + " " + aljazeera()["summary"]
    )
    for sentence in (
        briefing["opening"] + briefing["body"]
    ):
        assert sentence in source_texts


def test_briefing_corroborating_attribution():
    briefing = build_briefing(
        bbc(),
        [bbc(), aljazeera()],
        15,
        NOW,
    )
    assert briefing["source"] == "BBC World"
    assert "Al Jazeera" in briefing["corroborating"]


def test_briefing_bullets_are_literal():
    briefing = build_briefing(
        bbc(),
        [bbc(), aljazeera()],
        15,
        NOW,
    )
    bullets = briefing["bullets"]
    bullet_text = " ".join(
        b["text"] for b in bullets
    )
    combined = (
        bbc()["summary"] + " " + aljazeera()["summary"]
    )
    for b in bullets:
        assert b["text"].lower() in combined.lower()


def test_short_source_stays_short():
    single = item(
        "Small local story",
        summary="A single fact sentence.",
    )
    briefing = build_briefing(
        single,
        [single],
        15,
        NOW,
    )
    assert len(briefing["opening"]) == 1
    assert briefing["body"] == []
    assert briefing["corroborating"] == []


def test_location_bullet_stops_at_place_name():
    # Regression: the Location bullet must not swallow
    # following lowercase words ("Port Vila overn").
    storm = item(
        title="Tropical Storm Kei makes landfall near Port Vila",
        summary=(
            "Tropical Storm Kei made landfall near Port Vila "
            "overnight, bringing torrential rain and gusts of "
            "140 km/h."
        ),
        source="Pacific Times",
        minutes_ago=8,
    )
    briefing = build_briefing(
        storm,
        [storm],
        15,
        NOW,
    )
    location = [
        b for b in briefing["bullets"]
        if b["label"] == "Location"
    ]
    assert len(location) == 1
    assert location[0]["text"] == "Port Vila"


def test_bullet_sentence_not_repeated_in_body():
    # Regression: France 24 Typhoon Dolphin — the
    # "expected to make landfall" sentence must appear
    # exactly once (as the ➡️ Next bullet), never again
    # as a body paragraph.
    typhoon = item(
        title="China cancels flights, issues evacuation orders "
        "as Typhoon Dolphin looms",
        summary=(
            "China's National Meteorological Centre on Sunday "
            "issued a red typhoon alert, its most severe warning, "
            "as Typhoon Dolphin barrelled towards the country's "
            "densely populated eastern coast. Flights in and out "
            "of the region were cancelled and nearly 100,000 "
            "people were relocated to less risky areas, according "
            "to authorities. Typhoon Dolphin is expected to make "
            "landfall late Sunday or early Monday."
        ),
        source="France 24",
        score=73,
        urgency=["evacuation"],
    )
    briefing = build_briefing(
        typhoon,
        [typhoon],
        15,
        NOW,
    )
    next_bullets = [
        b for b in briefing["bullets"]
        if b["label"] == "Next"
    ]
    assert len(next_bullets) == 1
    landfall = next_bullets[0]["text"]
    assert "expected to make landfall" in landfall
    assert landfall not in briefing["body"]

    msg = build_message(
        dict(
            typhoon,
            public_label=briefing["label"],
            headline=briefing["headline"],
            briefing=briefing,
        ),
        {
            "target_message_chars": 1500,
            "max_message_chars": 3000,
        },
    )["text"]
    assert msg.count("expected to make landfall") == 1
    assert (
        msg.find("red typhoon alert")
        < msg.find("expected to make landfall")
    )


# ---------------------------------------------------------
# Headline cleaning
# ---------------------------------------------------------


def test_headline_strips_source_suffix():
    assert clean_headline(
        "Storm hits coast - BBC World"
    ) == "Storm hits coast"


def test_headline_strips_live_prefix():
    assert clean_headline(
        "Live: Storm hits coast"
    ) == "Storm hits coast"


def test_headline_never_paraphrases():
    assert clean_headline(
        "Canada wildfire doubles in size"
    ) == "Canada wildfire doubles in size"
