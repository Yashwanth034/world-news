"""Tests for the persistent source-health history and the Phase C
coverage-audit additions (concentration, sector/regional source
coverage, underrepresented sources).

Run with:  .venv/bin/python -m pytest src/test_source_health.py -q
"""
import json
import sqlite3

from src.audit_source_coverage import (
    compute_audit,
    hhi,
    load_feeds,
    top_n_shares,
)
from src.storage import (
    _error_class,
    init_schema,
    record_source_health,
    source_health_rows,
)

NOW1 = "2026-08-14T10:00:00+00:00"
NOW2 = "2026-08-14T11:00:00+00:00"

FEEDS = {
    "BBC World": {"name": "BBC World", "type": "publisher", "tier": 2,
                  "region": "Global", "sector": "general"},
    "SCMP": {"name": "SCMP", "type": "publisher", "tier": 2,
             "region": "Asia", "sector": "general"},
    "USGS": {"name": "USGS", "type": "primary", "tier": 1,
             "region": "Global", "sector": "disasters"},
}


def db():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return conn


def run_metrics(failed=False, error=None, fetched=0, accepted=0,
                duplicates=0, editorial_rejected=0, summarized=0):
    return {
        "attempted": True,
        "failed": failed,
        "error": error,
        "fetched": fetched,
        "accepted": accepted,
        "duplicates": duplicates,
        "editorial_rejected": editorial_rejected,
        "summarized": summarized,
    }


# ---------------------------------------------------------------------------
# source_health table
# ---------------------------------------------------------------------------


class TestSourceHealthTable:
    def test_table_created_by_init_schema(self):
        conn = db()
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(source_health)")}
        assert {"attempt_count", "success_count", "failure_count",
                "articles_fetched", "last_success", "last_failure",
                "last_error", "summarized_count"} <= cols

    def test_legacy_db_migrates(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE stories(id TEXT)")
        conn.execute("CREATE TABLE events(event_id TEXT)")
        init_schema(conn)  # must not raise
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(source_health)")}
        assert "attempt_count" in cols

    def test_migration_idempotent(self):
        conn = db()
        init_schema(conn)
        n1 = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        init_schema(conn)
        n2 = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        assert n1 == n2


# ---------------------------------------------------------------------------
# persistence / history
# ---------------------------------------------------------------------------


class TestSourceHealthPersistence:
    def test_failed_fetch_recorded(self):
        conn = db()
        record_source_health(
            conn,
            {"BBC World": run_metrics(
                failed=True, error="HTTP Error 403: Forbidden")},
            NOW1,
            feeds=FEEDS,
        )
        r = source_health_rows(conn)[0]
        assert r["attempt_count"] == 1
        assert r["failure_count"] == 1
        assert r["success_count"] == 0
        assert r["last_failure"] == NOW1
        assert r["last_error"] == "HTTP_403"
        assert r["last_success"] is None

    def test_successful_fetch_recorded(self):
        conn = db()
        record_source_health(
            conn,
            {"BBC World": run_metrics(
                fetched=25, accepted=8, duplicates=3,
                editorial_rejected=2, summarized=1)},
            NOW2,
            feeds=FEEDS,
        )
        r = source_health_rows(conn)[0]
        assert r["attempt_count"] == 1 and r["success_count"] == 1
        assert r["articles_fetched"] == 25
        assert r["articles_accepted"] == 8
        assert r["duplicates_generated"] == 3
        assert r["editorial_rejected_count"] == 2
        assert r["summarized_count"] == 1
        assert r["last_success"] == NOW2

    def test_history_accumulates_across_runs(self):
        # Run 1 fails, run 2 succeeds: history must remember both.
        conn = db()
        record_source_health(
            conn,
            {"BBC World": run_metrics(
                failed=True, error="HTTP Error 403: Forbidden")},
            NOW1,
            feeds=FEEDS,
        )
        record_source_health(
            conn,
            {"BBC World": run_metrics(
                fetched=25, accepted=8, duplicates=3,
                editorial_rejected=2, summarized=1)},
            NOW2,
            feeds=FEEDS,
        )
        r = source_health_rows(conn)[0]
        assert r["attempt_count"] == 2
        assert r["success_count"] == 1
        assert r["failure_count"] == 1
        assert r["last_success"] == NOW2
        assert r["last_failure"] == NOW1
        assert r["articles_fetched"] == 25
        # counters accumulate: a third successful run adds
        record_source_health(
            conn,
            {"BBC World": run_metrics(fetched=10, accepted=3)},
            "2026-08-14T12:00:00+00:00",
            feeds=FEEDS,
        )
        r = source_health_rows(conn)[0]
        assert r["attempt_count"] == 3
        assert r["success_count"] == 2
        assert r["articles_fetched"] == 35
        assert r["articles_accepted"] == 11

    def test_multiple_sources_independent(self):
        conn = db()
        record_source_health(
            conn,
            {"BBC World": run_metrics(fetched=10),
             "SCMP": run_metrics(failed=True, error="timeout")},
            NOW1,
            feeds=FEEDS,
        )
        rows = {r["source_id"]: r for r in source_health_rows(conn)}
        assert rows["BBC World"]["success_count"] == 1
        assert rows["SCMP"]["failure_count"] == 1
        assert rows["SCMP"]["last_error"] == "TIMEOUT"


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_known_classes(self):
        assert _error_class("HTTP Error 403: Forbidden") == "HTTP_403"
        assert _error_class("404 Not Found") == "HTTP_404"
        assert _error_class("timed out") == "TIMEOUT"
        assert _error_class("getaddrinfo failed: Name or service not known") == "DNS_ERROR"
        assert _error_class("XMLSyntaxError: mismatched tag") == "PARSE_ERROR"
        assert _error_class("Connection refused") == "CONNECTION_ERROR"

    def test_unknown_falls_back_to_other(self):
        assert _error_class("something weird happened") == "OTHER"
        assert _error_class("") is None
        assert _error_class(None) is None


# ---------------------------------------------------------------------------
# rates (zero-division safe)
# ---------------------------------------------------------------------------


class TestRates:
    def test_rates_computed_from_history(self):
        conn = db()
        record_source_health(
            conn,
            {"BBC World": run_metrics(fetched=25, accepted=8,
                                      duplicates=3)},
            NOW1,
            feeds=FEEDS,
        )
        r = source_health_rows(conn)[0]
        assert r["success_count"] == 1 and r["attempt_count"] == 1

    def test_no_division_by_zero(self):
        conn = db()
        record_source_health(
            conn,
            {"BBC World": run_metrics(failed=True, error="404")},
            NOW1,
            feeds=FEEDS,
        )
        r = source_health_rows(conn)[0]
        assert r["articles_fetched"] == 0
        # rates are computed by the audit layer, which guards /0
        report = compute_audit(
            [dict(FEEDS["BBC World"], feed_url="https://x/feed")],
            health=[{"source": "BBC World", "error": "404"}],
            cfg={"min_score_to_queue": 55, "discovery_min_score": 70},
            health_rows=[r],
        )
        src = report["source_coverage"][0]
        assert src["success_rate"] == 0.0
        assert src["failure_rate"] == 1.0
        assert src["useful_news_rate"] is None  # fetched == 0


# ---------------------------------------------------------------------------
# concentration
# ---------------------------------------------------------------------------


class TestConcentration:
    def test_top_n_shares(self):
        counts = {"a": 50, "b": 30, "c": 20}
        assert top_n_shares(counts, 1) == 0.5
        assert top_n_shares(counts, 2) == 0.8
        assert top_n_shares(counts, 3) == 1.0
        assert top_n_shares({}, 1) is None

    def test_hhi(self):
        # two equal sources -> 0.5
        assert abs(hhi({"a": 1, "b": 1}) - 0.5) < 1e-9
        # one dominant source -> 1.0
        assert abs(hhi({"a": 10}) - 1.0) < 1e-9
        # ten equal sources -> 0.1
        assert abs(hhi({f"s{i}": 1 for i in range(10)}) - 0.1) < 1e-9
        assert hhi({}) is None

    def test_concentration_in_report(self):
        feeds = [
            {"name": "A", "type": "publisher", "tier": 2, "region": "Global",
             "sector": "general", "feed_url": "https://a/feed", "news": True},
            {"name": "B", "type": "publisher", "tier": 2, "region": "Global",
             "sector": "general", "feed_url": "https://b/feed", "news": True},
            {"name": "C", "type": "publisher", "tier": 2, "region": "Global",
             "sector": "general", "feed_url": "https://c/feed", "news": True},
        ]
        rows = [
            {"title": f"event {i} alpha", "summary": "s", "source": "A",
             "url": f"https://a/{i}", "source_category": "world",
             "tier": 2, "primary_source": False, "discovery": False}
            for i in range(10)
        ] + [
            {"title": f"other {i} beta", "summary": "s", "source": "B",
             "url": f"https://b/{i}", "source_category": "world",
             "tier": 2, "primary_source": False, "discovery": False}
            for i in range(5)
        ] + [
            {"title": f"misc {i} gamma", "summary": "s", "source": "C",
             "url": f"https://c/{i}", "source_category": "world",
             "tier": 2, "primary_source": False, "discovery": False}
            for i in range(5)
        ]
        report = compute_audit(
            feeds, rows=rows,
            cfg={"min_score_to_queue": 55, "discovery_min_score": 70},
        )
        conc = report["source_concentration"]
        assert conc["top_1_share"] == 0.5   # A: 10 of 20 useful events
        assert abs(conc["top_3_share"] - 1.0) < 1e-9
        assert conc["hhi"] is not None and conc["hhi"] > 0.3


# ---------------------------------------------------------------------------
# underrepresented / sector / region coverage
# ---------------------------------------------------------------------------


class TestAuditAdditions:
    def _report(self, rows, health=None):
        feeds = [
            {"name": "A", "type": "publisher", "tier": 2, "region": "Global",
             "sector": "general", "feed_url": "https://a/feed", "news": True},
            {"name": "B", "type": "publisher", "tier": 2, "region": "Africa",
             "sector": "general", "feed_url": "https://b/feed", "news": True},
        ]
        return compute_audit(
            feeds, rows=rows, health=health,
            cfg={"min_score_to_queue": 55, "discovery_min_score": 70},
        )

    def test_underrepresented_source_detected(self):
        rows = [
            {"title": f"quake alpha {i} hits city", "summary": "s",
             "source": "A", "url": f"https://a/{i}",
             "source_category": "world", "tier": 2,
             "primary_source": False, "discovery": False}
            for i in range(10)
        ] + [
            # B's articles carry no urgency signal and come from a
            # tier-4 source, so they score below the queue
            # threshold and produce 0 useful events despite being
            # fetched.
            {"title": f"meeting agenda item {i}", "summary": "s",
             "source": "B", "url": f"https://b/{i}",
             "source_category": "world", "tier": 4,
             "primary_source": False, "discovery": False}
            for i in range(5)
        ]
        report = self._report(rows)
        under = report["underrepresented_sources"]
        names = [u["name"] for u in under]
        # B fetched 5 articles but produced 0 useful events -> listed
        assert "B" in names
        assert "A" not in names

    def test_sector_and_region_source_coverage(self):
        rows = [
            {"title": "quake strikes city", "summary": "s", "source": "A",
             "url": f"https://a/{i}", "source_category": "world",
             "tier": 2, "primary_source": False, "discovery": False}
            for i in range(5)
        ]
        report = self._report(rows)
        sec = report["sector_coverage_by_source"]
        assert "general" in sec
        assert sec["general"]["configured"] == 2
        assert sec["general"]["articles"] == 5
        reg = report["region_coverage_by_source"]
        assert "Global" in reg and "Africa" in reg
        assert reg["Global"]["articles"] == 5

    def test_unique_event_contribution_ranking(self):
        rows = [
            {"title": f"event {i} alpha", "summary": "s", "source": "A",
             "url": f"https://a/{i}", "source_category": "world",
             "tier": 2, "primary_source": False, "discovery": False}
            for i in range(3)
        ]
        report = self._report(rows)
        contrib = report["source_unique_event_contribution"]
        assert contrib[0]["name"] == "A"
        assert contrib[0]["useful"] == 3
        assert contrib[0]["useful_news_rate"] == 1.0


# ---------------------------------------------------------------------------
# JSON output / CLI
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_json_mode_prints_structured_report(self, capsys):
        import sys
        from src.audit_source_coverage import main
        rc = main([
            "--config", "config.json", "--json",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        # stable field names
        assert "source_concentration" in report
        assert "underrepresented_sources" in report
        assert "sector_coverage_by_source" in report
        assert "region_coverage_by_source" in report
        assert "source_unique_event_contribution" in report
        assert "source_health_history" in report
        assert "totals" in report
        assert report["totals"]["sources"] == 51

    def test_load_feeds_roundtrip(self):
        feeds, cfg = load_feeds("config.json")
        assert len(feeds) == 51
        assert all(f.get("name") for f in feeds)
