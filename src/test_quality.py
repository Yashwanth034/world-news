from src.quality import quality_check, sentence_count
from src.formatter import format_story


item = {
    "title": "Major earthquake strikes region",
    "summary": (
        "Officials are assessing the situation. "
        "Emergency teams are responding to the affected area. "
        "Authorities are monitoring conditions and preparing additional support."
    ),
    "source": "Test Source",
    "url": "https://example.com/story",
    "confidence": "high",
    "score": 90,
    "primary_source": True,
    "corroborating_sources": 2,
    "strong_corroboration": 2,
    "event_status": "NEW",
    "event_id": "test-event",
    "language_status": "ENGLISH",
}


out = format_story(item)
item.update(out)

r = quality_check(item)

assert r["quality_pass"], r


if item["format"] == "single":
    assert len(item["post"]) <= 270
    assert item["post"].strip()
    assert "Source:" in item["post"]

    sentence_total = sentence_count(item["post"])

    assert 3 <= sentence_total <= 4, item["post"]


else:
    assert item["thread"]

    for post in item["thread"]:
        assert post.strip()
        assert len(post) <= 270


print("QUALITY TEST PASSED")
