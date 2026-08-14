"""Tests for the source-coverage audit machinery.

Covers: source metadata parsing, unknown-sector handling, event
region classification, duplicate-source detection, source failure
handling, source disable/enable behaviour, primary-source
classification, sector coverage audit and regional coverage audit.

Run with:  .venv/bin/python -m pytest src/test_source_coverage.py -q
"""

import json
import os
import tempfile

from src.audit_source_coverage import (
    compute_audit,
    load_feeds,
    normalize_feed,
    source_region,
    source_sector,
    source_type,
)
from src.regions import classify_event_region, all_regions, top_regions
from src.sectors import (
    SECTOR_TREE,
    classify_sector,
    sub_sectors,
    top_sectors,
)


def feed(**overrides):
    base = {
        "name": "Test Feed",
        "url": "https://example.com/rss.xml",
        "category": "world",
        "tier": 2,
    }
    base.update(overrides)
    return base


def article(title, summary="", source="Test Feed", category="world",
            discovery=False, tier=2, url=None):
    return {
        "title": title,
        "summary": summary,
        "source": source,
        "url": url or "https://example.com/" + str(abs(hash(title))),
        "source_category": category,
        "discovery": discovery,
        "tier": tier,
        "primary_source": False,
    }


class TestSourceMetadata:
    def test_normalize_feed_fills_all_fields(self):
        f = normalize_feed(feed())
        for key in (
            "source_id", "name", "type", "tier", "reliability",
            "region", "sector", "language", "feed_url", "news",
            "primary_source", "discovery", "breaking_capability",
            "freshness_expectation",
        ):
            assert key in f, key
        assert f["news"] is True
        assert f["discovery"] is False
        assert f["language"] == "en"

    def test_primary_source_type_and_breaking(self):
        f = normalize_feed(feed(primary=True, tier=1))
        assert f["type"] == "primary_source"
        assert f["breaking_capability"] is True
        assert f["reliability"] == "high"

    def test_discovery_type(self):
        f = normalize_feed(feed(discovery=True, tier=4))
        assert f["type"] == "discovery_aggregator"
        assert f["reliability"] == "unknown"

    def test_agency_type(self):
        f = normalize_feed(feed(url="https://reuters.com/rss"))
        assert f["type"] == "news_agency"

    def test_region_normalisation(self):
        assert source_region(feed(category="india")) == "Asia|South Asia"
        assert source_region(
            feed(category="middle-east")
        ) == "Asia|West Asia / Middle East"
        assert source_region(feed(category="world")) == "Global"
        assert source_region(feed(region="Africa|West Africa")) == "Africa|West Africa"

    def test_sector_normalisation(self):
        assert source_sector(feed(category="finance")) == "finance"
        assert source_sector(feed(category="health")) == "health"
        assert source_sector(feed(category="world")) == "general"

    def test_load_feeds(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as fh:
            json.dump({"feeds": [feed()]}, fh)
            path = fh.name
        try:
            feeds, cfg = load_feeds(path)
            assert len(feeds) == 1
            assert feeds[0]["name"] == "Test Feed"
        finally:
            os.unlink(path)


class TestSectorTaxonomy:
    def test_top_and_sub_sectors(self):
        assert "climate" in top_sectors()
        assert "earthquakes" in sub_sectors("climate")
        assert len(top_sectors()) >= 10

    def test_known_classifications(self):
        assert classify_sector(
            "Powerful earthquake strikes Japan", "Magnitude 7.6 quake"
        )[0] == "climate"
        assert classify_sector(
            "US Federal Reserve holds rates", "The Fed kept its rate"
        )[0] == "economy"
        assert classify_sector(
            "China unveils chip export curbs", "Semiconductor controls"
        )[0] == "technology"
        assert classify_sector(
            "Swiatek defeats Rybakina", "Wins the Canadian Open"
        )[0] == "sports"

    def test_plural_matching(self):
        # "tariffs" must match the "tariff" term.
        top, sub = classify_sector(
            "US slaps tariffs on steel imports", ""
        )
        assert top == "economy"
        assert sub == "trade"

    def test_unknown_sector_handling(self):
        assert classify_sector(
            "A completely unrelated whimsical phrase", "no terms here"
        ) == ("other", "general")

    def test_source_category_fallback(self):
        # No article terms: the source's own topic label is used.
        top, sub = classify_sector(
            "A local story", "Nothing recognizable here", "health"
        )
        assert (top, sub) == ("health", "health")


class TestRegionClassification:
    def test_us_and_pronoun_safety(self):
        assert classify_event_region(
            "US Federal Reserve holds rates", "The Fed acted"
        ) == ("North America", "United States")
        # lowercase "us" is a pronoun and must never match.
        assert classify_event_region(
            "us and them", "Let us consider the options"
        ) == (None, None)

    def test_first_mention_wins(self):
        # The subject of the headline names the region before a
        # secondary mention.
        assert classify_event_region(
            "Canada pushes back on US tariffs", "Ottawa responded"
        ) == ("North America", "Canada")
        assert classify_event_region(
            "Mexico and US agree border deal", "Officials met"
        ) == ("North America", "Mexico")

    def test_major_regions(self):
        assert classify_event_region(
            "Earthquake hits Japan", "A 7.6 quake struck"
        )[0] == "Asia"
        assert classify_event_region(
            "Coup in Niger", "The junta took power"
        )[0] == "Africa"
        assert classify_event_region(
            "Flooding in Brazil", "Rivers overflowed"
        )[0] == "South America"

    def test_all_regions_well_formed(self):
        tops = top_regions()
        pairs = all_regions()
        assert len(pairs) >= 20
        for top, sub in pairs:
            assert top in tops
            assert sub


class TestDuplicateSourceDetection:
    def test_same_domain_flagged(self):
        f1 = normalize_feed(feed(name="Feed A", url="https://x.com/a.xml"))
        f2 = normalize_feed(feed(name="Feed B", url="https://x.com/b.xml"))
        report = compute_audit([f1, f2], rows=[])
        assert "x.com" in report["source_redundancy"]
        assert set(report["source_redundancy"]["x.com"]) == {"Feed A", "Feed B"}

    def test_distinct_domains_not_flagged(self):
        f1 = normalize_feed(feed(name="Feed A", url="https://a.com/rss"))
        f2 = normalize_feed(feed(name="Feed B", url="https://b.com/rss"))
        report = compute_audit([f1, f2], rows=[])
        assert report["source_redundancy"] == {}


class TestSourceFailureHandling:
    def test_failed_source_reported(self):
        f = normalize_feed(feed())
        health = [{
            "source": "Test Feed", "url": f["feed_url"],
            "status": 403, "entries_seen": 0,
            "recent_entries": 0, "error": "Forbidden",
        }]
        report = compute_audit([f], rows=[], health=health)
        assert report["source_failure_rate"] == 1.0
        assert report["failed_sources"][0]["name"] == "Test Feed"
        assert report["failed_sources"][0]["error"] == "Forbidden"

    def test_ok_source_not_failed(self):
        f = normalize_feed(feed())
        health = [{
            "source": "Test Feed", "url": f["feed_url"],
            "status": 200, "entries_seen": 10,
            "recent_entries": 5, "error": None,
        }]
        report = compute_audit([f], rows=[], health=health)
        assert report["source_failure_rate"] == 0.0
        assert report["failed_sources"] == []


class TestSourceEnableDisable:
    def test_news_false_marked(self):
        f = normalize_feed(feed(news=False))
        assert f["news"] is False
        report = compute_audit([f], rows=[])
        assert report["source_coverage"][0]["news"] is False

    def test_absent_feed_excluded(self):
        # "Disabling" a source = removing it from the config: it
        # then never appears in the audit.
        f = normalize_feed(feed())
        report = compute_audit([f], rows=[])
        assert len(report["source_coverage"]) == 1
        report2 = compute_audit([], rows=[])
        assert report2["source_coverage"] == []


class TestCoverageAudit:
    def test_sector_coverage_distribution(self):
        f = normalize_feed(feed())
        rows = [
            article("Earthquake hits Japan", "7.6 quake", category="world"),
            article("Stock market rally", "Wall Street up", category="finance"),
            article("Stock market rally copy", "Wall Street up", category="finance"),
        ]
        report = compute_audit([f], rows=rows)
        dist = report["sector_coverage"]["distribution"]
        assert dist.get("climate", 0) >= 1
        assert dist.get("economy", 0) >= 2

    def test_weak_and_missing_sectors(self):
        f = normalize_feed(feed())
        rows = [
            article("Earthquake hits Japan", "7.6 quake"),
            article("War in Ukraine", "Airstrikes continue"),
        ]
        report = compute_audit([f], rows=rows)
        # Most sectors were never covered: they must be missing.
        assert len(report["missing_sectors"]) >= 5
        # Weak = present but below the volume threshold.
        assert report["weak_sectors"]

    def test_regional_coverage_distribution(self):
        f = normalize_feed(feed())
        rows = [
            article("Coup in Niger", "Junta takes power"),
            article("Earthquake in Japan", "Quake strikes"),
            article("Flooding in Brazil", "Rivers overflow"),
        ]
        report = compute_audit([f], rows=rows)
        dist = report["regional_coverage"]["distribution"]
        assert dist.get("Africa", 0) >= 1
        assert dist.get("Asia", 0) >= 1
        assert dist.get("South America", 0) >= 1

    def test_missing_regions_reported(self):
        f = normalize_feed(feed())
        report = compute_audit([f], rows=[])
        # With no articles, every top region is missing.
        assert set(report["missing_regions"]) == set(top_regions())

    def test_duplicate_articles_counted(self):
        f1 = normalize_feed(feed(name="A", url="https://a.com/rss"))
        f2 = normalize_feed(feed(name="B", url="https://b.com/rss"))
        rows = [
            article("Same event everywhere", "Identical wording",
                    source="A", url="https://a.com/s1"),
            article("Same event everywhere", "Identical wording",
                    source="B", url="https://b.com/s2"),
        ]
        report = compute_audit([f1, f2], rows=rows)
        assert report["totals"]["articles_duplicates"] == 1
        assert report["totals"]["articles_accepted"] == 1

    def test_editorial_rejections_counted(self):
        f = normalize_feed(feed())
        rows = [
            article("Best vacuum cleaners of 2026", "A buying guide"),
            article("Earthquake hits Japan", "A 7.6 quake struck"),
        ]
        report = compute_audit([f], rows=rows)
        assert report["totals"]["articles_rejected"] >= 1
