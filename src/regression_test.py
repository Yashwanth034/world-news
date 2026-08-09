import re

from src.formatter import format_story
from src.quality import quality_check
from src.priority import priority


def test_normal():
    item = {
        "title": "Central bank announces new policy",
        "summary": (
            "Officials said the policy will take effect next month "
            "and explained the expected changes. "
            "The central bank said the measure is intended to support "
            "economic stability."
        ),
        "source": "Test",
        "url": "https://example.com/a",
        "confidence": "medium",
        "score": 60,
        "primary_source": True,
        "strong_corroboration": 1,
        "corroborating_sources": 1,
        "event_status": "NEW",
        "event_id": "a",
        "language_status": "ENGLISH",
    }

    item.update(
        format_story(item)
    )

    r = quality_check(item)

    assert r["quality_pass"], r
    assert item["format"] == "single"


def test_urgent():
    item = {
        "title": "Major earthquake triggers tsunami warning",
        "summary": (
            "Emergency authorities are assessing the situation."
        ),
        "source": "Test",
        "url": "https://example.com/b",
        "confidence": "high",
        "score": 70,
        "primary_source": True,
        "strong_corroboration": 1,
        "corroborating_sources": 1,
        "event_status": "NEW",
        "event_id": "b",
        "language_status": "ENGLISH",
    }

    p = priority(item)

    assert p["priority_level"] == "IMMEDIATE", p


def test_no_confirmed_low_confidence():
    """
    Low-confidence stories may use the label
    UNCONFIRMED, but must never claim that something
    is confirmed.
    """

    item = {
        "format": "single",
        "post": (
            "⚠️ UNCONFIRMED: A major event may have happened. "
            "The report is not independently verified. "
            "Officials are assessing the available information. "
            "Source: Test."
        ),
        "url": "https://example.com",
        "confidence": "low",
        "language_status": "ENGLISH",
        "event_status": "NEW",
        "event_id": "c",
    }

    r = quality_check(item)

    assert r["quality_pass"], r

    # UNCONFIRMED is allowed.
    assert "UNCONFIRMED" in item["post"]

    # But the standalone word "confirmed" is not allowed.
    assert not re.search(
        r"\bconfirmed\b",
        item["post"].lower()
    )


def main():
    test_normal()
    test_urgent()
    test_no_confirmed_low_confidence()

    print("REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
