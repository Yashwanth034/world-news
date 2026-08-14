"""Source-network coverage audit for the WorldNews pipeline.

Reports source, sector and event-region coverage, source failure
rates, source redundancy, article/rejection/duplicate volume, and
identifies missing/weak sectors and regions, overrepresented sources
and low-value (feature-heavy) sources.

Usage:
    python -m src.audit_source_coverage --config config.json [--json]
    python -m src.audit_source_coverage --config config.json --live
        [--json] [--out audit.json]

Modes:
    default  metadata-only audit of the configured source network
             (source inventory, tier/reliability, redundancy by
             publisher domain, configured sector/region coverage).
    --live   additionally fetch every feed now and audit the actual
             articles: per-source volume, editorial accept/reject,
             score gate, duplicates, sector distribution and event
             region distribution.  Never publishes anything.

--health PATH and --db PATH may be given to merge a real run's
source-health JSON and SQLite DB into the report.

The audit is measurement only: it never gates publishing and applies
no sector or regional quotas.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

from src import sectors as sector_mod
from src import regions as region_mod
from src.editorial import editorial_eligibility
from src.source_reliability import get_tier, TIER_SCORE


# ---------------------------------------------------------------------------
# Source metadata normalisation
# ---------------------------------------------------------------------------

# Publisher-domain keywords that identify government/institutional
# primary sources when no explicit "type" is present.
_PRIMARY_DOMAIN_HINTS = (
    "gov", "int", "europa", "who.int", "esa.int", "un.org", "sec.gov",
    "cisa.gov", "usgs.gov", "noaa.gov", "nasa.gov", "jpl.nasa.gov",
)

_AGENCY_HINTS = ("reuters", "ap.org", "apnews", "afp", "efe", "xinhua",
                 "kyodo", "tass", "anadolu", "dpa", "ansa", "upi")

_REGION_HINTS = {
    "india": "Asia|South Asia",
    "japan": "Asia|East Asia",
    "china": "Asia|East Asia",
    "south-korea": "Asia|East Asia",
    "southeast-asia": "Asia|Southeast Asia",
    "middle-east": "Asia|West Asia / Middle East",
    "europe": "Europe|Europe",
    "africa": "Africa",
    "latin-america": "South America|South America",
    "canada": "North America|Canada",
    "australia": "Oceania|Australia",
    "pacific": "Oceania|Pacific Islands",
    "south-asia": "Asia|South Asia",
    "east-asia": "Asia|East Asia",
    "oceania": "Oceania",
    "north-america": "North America",
    "us": "North America|United States",
    "usa": "North America|United States",
}


def _domain(url):
    from urllib.parse import urlparse
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def source_type(feed):
    """Classify a feed's source type."""
    if feed.get("discovery"):
        return "discovery_aggregator"
    if feed.get("type"):
        return feed["type"]
    if feed.get("primary"):
        return "primary_source"
    dom = _domain(feed.get("url"))
    if any(h in dom for h in _PRIMARY_DOMAIN_HINTS):
        return "primary_source"
    if any(h in dom for h in _AGENCY_HINTS):
        return "news_agency"
    return "publisher"


def source_region(feed):
    """Normalise a feed's SOURCE region (publisher geography)."""
    explicit = feed.get("region")
    if explicit and "|" in str(explicit):
        return str(explicit)
    if explicit and str(explicit) in _REGION_HINTS:
        return _REGION_HINTS[str(explicit)]
    cat = str(feed.get("category", "")).lower()
    if cat == "world":
        return "Global"
    if cat in _REGION_HINTS:
        return _REGION_HINTS[cat]
    return "unknown"


# Geographic/source labels that are never a publisher beat.
_GEO_SECTOR_LABELS = {
    "world", "africa", "india", "japan", "china", "south-korea",
    "southeast-asia", "europe", "middle-east", "latin-america",
    "canada", "australia", "pacific", "south-asia", "east-asia",
    "oceania", "north-america", "south-america", "central-asia",
}


def source_sector(feed):
    """Sector label for a feed (publisher's beat)."""
    cat = str(feed.get("category", "")).lower()
    if cat in sector_mod.SECTOR_TREE:
        return cat
    if cat in sector_mod.SECTOR_TERMS and cat not in _GEO_SECTOR_LABELS:
        return cat
    if cat in _GEO_SECTOR_LABELS:
        return "general"
    return cat or "general"


def reliability_label(tier):
    return {
        1: "high",
        2: "medium",
        3: "low",
        4: "unknown",
    }.get(tier, "unknown")


def normalize_feed(feed):
    """Expand a config feed entry into the audit metadata schema."""
    tier = get_tier(feed)
    return {
        "source_id": re.sub(
            r"[^a-z0-9]+", "-",
            str(feed.get("name", "")).lower().strip("-"),
        ) or _domain(feed.get("url")),
        "name": feed.get("name"),
        "type": source_type(feed),
        "tier": tier,
        "reliability": reliability_label(tier),
        "region": source_region(feed),
        "sector": source_sector(feed),
        "language": feed.get("language", "en"),
        "feed_url": feed.get("url"),
        "news": bool(feed.get("news", True)),
        "primary_source": bool(feed.get("primary", False)),
        "discovery": bool(feed.get("discovery", False)),
        "breaking_capability": bool(
            feed.get("primary", False)
            or source_type(feed) in ("news_agency", "primary_source")
        ),
        "freshness_expectation": feed.get(
            "freshness", "high"
            if tier <= 1 or source_type(feed) in (
                "news_agency", "primary_source",
            )
            else "medium",
        ),
    }


def load_feeds(config_path):
    with open(config_path) as fh:
        cfg = json.load(fh)
    return [normalize_feed(f) for f in cfg["feeds"]], cfg


# ---------------------------------------------------------------------------
# Article-level classification used by --live
# ---------------------------------------------------------------------------

def _norm_title(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _norm_url(url):
    from urllib.parse import urlparse
    u = urlparse(url or "")
    path = re.sub(r"/+$", "", u.path or "")
    return (u.netloc + path).lower()


def classify_article(row, min_score, discovery_min_score):
    """sector, region, score, editorial_pass, discovery for one row."""
    item = {
        "title": row.get("title"),
        "summary": row.get("summary"),
        "source": row.get("source"),
        "url": row.get("url"),
        "tier": row.get("tier", 4),
        "primary_source": row.get("primary_source", False),
    }
    top, sub = sector_mod.classify_sector(
        row.get("title"), row.get("summary"),
        row.get("source_category"),
    )
    region = region_mod.classify_event_region(
        row.get("title"), row.get("summary"),
    )
    from src.intelligence import classify
    info = classify(
        row.get("title"), row.get("summary"),
        row.get("source_category"), item,
    )
    score = info["score"]
    editorial_pass = editorial_eligibility(item)
    is_disc = bool(row.get("discovery"))
    threshold = discovery_min_score if is_disc else min_score
    return {
        "sector": top,
        "sub_sector": sub,
        "region": region[0],
        "sub_region": region[1],
        "score": score,
        "editorial_pass": editorial_pass,
        "below_score": score < threshold,
        "discovery": is_disc,
        "title": row.get("title"),
        "url": row.get("url"),
        "source": row.get("source"),
    }


# ---------------------------------------------------------------------------
# Concentration / balance helpers
# ---------------------------------------------------------------------------

def top_n_shares(counts, n):
    """Share (0..1) of the total held by the top-N items.

    `counts` maps name -> non-negative number.  Returns None when
    there is nothing to measure (empty or all-zero).
    """
    total = sum(counts.values())
    if total <= 0:
        return None
    ranked = sorted(
        counts.values(),
        reverse=True,
    )
    return sum(ranked[:n]) / total


def hhi(counts):
    """Herfindahl-Hirschman Index over a distribution.

    Sum of squared shares (0..1 scale).  Interpreting a source
    distribution: < 0.15 low concentration, 0.15-0.25 moderate,
    > 0.25 high (one or two sources dominate useful output).
    Returns None when there is nothing to measure.
    """
    total = sum(counts.values())
    if total <= 0:
        return None
    return sum(
        (v / total) ** 2
        for v in counts.values()
    )


def _weak_threshold(total, n):
    """A bucket with fewer than ~1% of articles (min 3) is weak."""
    if total <= 0:
        return True
    return n < max(3, total * 0.01)


def compute_audit(feeds, rows=None, health=None, cfg=None, health_rows=None):
    """Build the full audit report dict.

    `feeds` is the normalised feed metadata list; `rows` (optional)
    is the list of collected article dicts from a --live run; `health`
    is the source-health list from the collector.
    """
    cfg = cfg or {}
    min_score = int(cfg.get("min_score_to_queue", 55))
    disc_min = int(cfg.get("discovery_min_score", 70))

    by_name = {f["name"]: f for f in feeds}

    # ---- article-level aggregation (--live) ----
    per_source = defaultdict(lambda: {
        "fetched": 0, "accepted": 0, "editorial_rejected": 0,
        "below_score": 0, "duplicates": 0, "useful": 0,
        "sectors": Counter(), "regions": Counter(),
    })
    sector_counts = Counter()
    region_counts = Counter()
    subregion_counts = Counter()
    total = 0
    dup_keys = {}

    for row in (rows or []):
        src = row.get("source")
        info = classify_article(row, min_score, disc_min)
        total += 1
        bucket = per_source[src]
        bucket["fetched"] += 1
        bucket["sectors"][info["sector"]] += 1
        if info["region"]:
            bucket["regions"][info["region"]] += 1
        if not info["editorial_pass"]:
            bucket["editorial_rejected"] += 1
            continue
        sector_counts[info["sector"]] += 1
        if info["region"]:
            region_counts[info["region"]] += 1
            if info["sub_region"]:
                subregion_counts[
                    info["region"] + "|" + info["sub_region"]
                ] += 1
        if info["below_score"]:
            bucket["below_score"] += 1
            continue
        # Same-story detection for the coverage audit: an article
        # whose normalized title was already contributed by a
        # different source is the same event (source redundancy),
        # while the pipeline's own url+title sid() dedup stays
        # unchanged and is reported separately by the run stats.
        key = _norm_title(info["title"])
        if key in dup_keys and dup_keys[key] != src:
            bucket["duplicates"] += 1
            dup_keys[key] = None  # duplicate consumed
            continue
        dup_keys[key] = src
        bucket["accepted"] += 1
        bucket["useful"] += 1

    # ---- health aggregation ----
    health_by_source = {}
    for h in (health or []):
        health_by_source[h.get("source")] = h

    # ---- persistent source-health history (--db) ----
    health_history = {}
    for row in (health_rows or []):
        health_history[row.get("source_id")] = row

    source_rows = []
    for f in feeds:
        bucket = per_source.get(f["name"]) or defaultdict(Counter)
        h = health_by_source.get(f["name"]) or {}
        status = h.get("status")
        failed = (
            h.get("error") is not None
            or (status is not None and status >= 400)
        )
        entries_seen = int(h.get("entries_seen") or 0)
        recent = int(h.get("recent_entries") or 0)
        fetched = bucket.get("fetched", 0) or recent
        hist = health_history.get(f["name"]) or {}
        attempts = int(hist.get("attempt_count") or 0)
        successes = int(hist.get("success_count") or 0)
        failures = int(hist.get("failure_count") or 0)
        useful = bucket.get("useful", 0)
        source_rows.append({
            **f,
            "entries_seen": entries_seen,
            "recent_entries": recent,
            "failed": bool(failed),
            "error": h.get("error"),
            "status": status,
            "fetched": fetched,
            "accepted": bucket.get("accepted", 0),
            "editorial_rejected": bucket.get("editorial_rejected", 0),
            "below_score": bucket.get("below_score", 0),
            "duplicates": bucket.get("duplicates", 0),
            "useful": useful,
            "sector_counts": dict(bucket.get("sectors", Counter())),
            "region_counts": dict(bucket.get("regions", Counter())),
            # persistent historical metrics (across all runs)
            "attempt_count": attempts,
            "success_count": successes,
            "failure_count": failures,
            "last_success": hist.get("last_success"),
            "last_failure": hist.get("last_failure"),
            "last_error": hist.get("last_error"),
            "articles_fetched_total": int(
                hist.get("articles_fetched") or 0
            ),
            "articles_accepted_total": int(
                hist.get("articles_accepted") or 0
            ),
            "duplicates_total": int(
                hist.get("duplicates_generated") or 0
            ),
            "editorial_rejected_total": int(
                hist.get("editorial_rejected_count") or 0
            ),
            "summarized_total": int(
                hist.get("summarized_count") or 0
            ),
            "success_rate": round(
                successes / attempts, 3,
            ) if attempts else None,
            "failure_rate": round(
                failures / attempts, 3,
            ) if attempts else None,
            "useful_news_rate": round(
                useful / fetched, 3,
            ) if fetched else None,
            "duplicate_rate": round(
                bucket.get("duplicates", 0) / fetched, 3,
            ) if fetched else None,
        })

    fetched_total = sum(r["fetched"] for r in source_rows)
    useful_total = sum(r["useful"] for r in source_rows)
    accepted_total = sum(r["accepted"] for r in source_rows)
    reject_total = sum(r["editorial_rejected"] + r["below_score"]
                       for r in source_rows)
    dup_total = sum(r["duplicates"] for r in source_rows)
    failed_sources = [r for r in source_rows if r["failed"]]

    # ---- redundancy (publisher-domain overlap) ----
    dom_groups = defaultdict(list)
    for r in source_rows:
        dom_groups[_domain(r["feed_url"])].append(r["name"])
    redundant = {
        dom: names for dom, names in dom_groups.items()
        if len(names) > 1
    }

    # ---- weak / missing sectors and regions ----
    top_sectors = sector_mod.top_sectors()
    sector_articles = {
        s: sector_counts.get(s, 0) for s in top_sectors
    }
    missing_sectors = [
        s for s in top_sectors if sector_articles[s] == 0
    ]
    weak_sectors = [
        s for s in top_sectors
        if sector_articles[s] > 0
        and _weak_threshold(total, sector_articles[s])
    ]

    top_regions = region_mod.top_regions()
    region_articles = {
        r: region_counts.get(r, 0) for r in top_regions
    }
    missing_regions = [
        r for r in top_regions if region_articles[r] == 0
    ]
    weak_regions = [
        r for r in top_regions
        if region_articles[r] > 0
        and _weak_threshold(total, region_articles[r])
    ]

    # ---- overrepresented / low-value sources ----
    expected_share = 1.0 / max(1, len(source_rows))
    overrepresented = [
        r["name"] for r in source_rows
        if fetched_total > 0
        and (r["fetched"] / fetched_total) > 2 * expected_share
    ]
    low_value = [
        {
            "name": r["name"],
            "editorial_rejection_rate": round(
                (r["editorial_rejected"] / r["fetched"])
                if r["fetched"] else 0.0, 2,
            ),
            "useful_share": round(
                (r["useful"] / r["fetched"])
                if r["fetched"] else 0.0, 2,
            ),
        }
        for r in source_rows
        if r["fetched"] >= 5
        and (r["editorial_rejected"] / r["fetched"]) > 0.5
    ]
    low_value.sort(key=lambda x: -x["editorial_rejection_rate"])

    # ---- publisher concentration (unique-event distribution) ----
    useful_by_source = {
        r["name"]: r["useful"] for r in source_rows
    }
    concentration = {
        "top_1_share": top_n_shares(useful_by_source, 1),
        "top_3_share": top_n_shares(useful_by_source, 3),
        "top_5_share": top_n_shares(useful_by_source, 5),
        "top_10_share": top_n_shares(useful_by_source, 10),
        "hhi": hhi(useful_by_source),
        "denominator": "unique useful events per source "
                       "(accepted after editorial+score+different-source dedup)",
    }
    raw_by_source = {
        r["name"]: r["fetched"] for r in source_rows
    }
    concentration["raw_fetched"] = {
        "top_1_share": top_n_shares(raw_by_source, 1),
        "top_3_share": top_n_shares(raw_by_source, 3),
        "top_5_share": top_n_shares(raw_by_source, 5),
        "top_10_share": top_n_shares(raw_by_source, 10),
        "hhi": hhi(raw_by_source),
        "denominator": "raw articles fetched per source",
    }

    # ---- underrepresented sources ----
    # A source is *underrepresented* when its share of useful
    # events is far below its expected share, it fetched real
    # articles this run, and its configured capability is not
    # already recognised in the report.  Low volume alone never
    # qualifies: a specialized source with 5 excellent events is
    # valuable, not underrepresented.
    n_sources = max(1, len(source_rows))
    expected = 1.0 / n_sources
    underrepresented = [
        {
            "name": r["name"],
            "sector": r.get("sector"),
            "region": r.get("region"),
            "fetched": r["fetched"],
            "useful": r["useful"],
            "useful_share": round(
                r["useful"] / useful_total, 3,
            ) if useful_total else 0.0,
            "expected_share": round(expected, 3),
        }
        for r in source_rows
        if r["fetched"] >= 5
        and r["failed"] is False
        and r["useful"] == 0
    ]
    underrepresented.sort(key=lambda x: -x["fetched"])

    # ---- sector-level source coverage ----
    sector_sources = defaultdict(lambda: {
        "configured": 0, "successful": 0, "failed": 0,
        "articles": 0, "useful": 0,
    })
    for r in source_rows:
        sec = r.get("sector") or "other"
        b = sector_sources[sec]
        b["configured"] += 1
        if r["failed"]:
            b["failed"] += 1
        else:
            b["successful"] += 1
        b["articles"] += r["fetched"]
        b["useful"] += r["useful"]
    sector_coverage_by_source = {
        sec: dict(b) for sec, b in sorted(
            sector_sources.items(),
            key=lambda kv: -kv[1]["articles"],
        )
    }

    # ---- regional source coverage ----
    region_sources = defaultdict(lambda: {
        "configured": 0, "successful": 0, "failed": 0,
        "articles": 0, "useful": 0,
    })
    for r in source_rows:
        reg = r.get("region") or "global"
        b = region_sources[reg]
        b["configured"] += 1
        if r["failed"]:
            b["failed"] += 1
        else:
            b["successful"] += 1
        b["articles"] += r["fetched"]
        b["useful"] += r["useful"]
    region_coverage_by_source = {
        reg: dict(b) for reg, b in sorted(
            region_sources.items(),
            key=lambda kv: -kv[1]["articles"],
        )
    }

    report = {
        "generated_at": __import__(
            "datetime"
        ).datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "totals": {
            "sources": len(source_rows),
            "articles_fetched": fetched_total,
            "articles_unique": accepted_total + dup_total,
            "articles_accepted": accepted_total,
            "articles_useful": useful_total,
            "articles_duplicates": dup_total,
            "articles_rejected": reject_total,
            "sources_failed": len(failed_sources),
        },
        "source_coverage": source_rows,
        "sector_coverage": {
            "articles": dict(sector_articles),
            "distribution": {
                s: sector_counts.get(s, 0)
                for s in sorted(
                    sector_counts, key=lambda x: -sector_counts[x]
                )
            },
        },
        "regional_coverage": {
            "articles": dict(region_articles),
            "distribution": {
                r: region_counts.get(r, 0)
                for r in sorted(
                    region_counts, key=lambda x: -region_counts[x]
                )
            },
            "sub_regions": {
                r: subregion_counts.get(r, 0)
                for r in sorted(
                    subregion_counts, key=lambda x: -subregion_counts[x]
                )
            },
        },
        "source_failure_rate": (
            len(failed_sources) / len(source_rows)
            if source_rows else 0.0
        ),
        "failed_sources": failed_sources,
        "source_redundancy": redundant,
        "missing_sectors": missing_sectors,
        "weak_sectors": weak_sectors,
        "missing_regions": missing_regions,
        "weak_regions": weak_regions,
        "overrepresented_sources": overrepresented,
        "underrepresented_sources": underrepresented,
        "low_value_sources": low_value,
        "source_concentration": concentration,
        "sector_coverage_by_source": sector_coverage_by_source,
        "region_coverage_by_source": region_coverage_by_source,
        "source_unique_event_contribution": [
            {
                "name": r["name"],
                "sector": r.get("sector"),
                "region": r.get("region"),
                "fetched": r["fetched"],
                "useful": r["useful"],
                "duplicates": r["duplicates"],
                "useful_news_rate": r["useful_news_rate"],
                "success_rate": r["success_rate"],
            }
            for r in sorted(
                source_rows,
                key=lambda x: -x["useful"],
            )
        ],
        "source_health_history": [
            {
                "name": r["name"],
                "attempt_count": r["attempt_count"],
                "success_count": r["success_count"],
                "failure_count": r["failure_count"],
                "success_rate": r["success_rate"],
                "failure_rate": r["failure_rate"],
                "articles_fetched_total": r["articles_fetched_total"],
                "articles_accepted_total": r["articles_accepted_total"],
                "duplicates_total": r["duplicates_total"],
                "editorial_rejected_total": r["editorial_rejected_total"],
                "summarized_total": r["summarized_total"],
                "last_success": r["last_success"],
                "last_failure": r["last_failure"],
                "last_error": r["last_error"],
            }
            for r in source_rows
        ],
    }
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_text(report):
    lines = []
    t = report["totals"]
    lines.append("=" * 78)
    lines.append("SOURCE COVERAGE AUDIT")
    lines.append("=" * 78)
    lines.append(
        "sources=%d fetched=%d unique=%d accepted=%d useful=%d "
        "duplicates=%d rejected=%d failed_sources=%d" % (
            t["sources"], t["articles_fetched"], t["articles_unique"],
            t["articles_accepted"], t["articles_useful"],
            t["articles_duplicates"], t["articles_rejected"],
            t["sources_failed"],
        )
    )
    lines.append("")
    lines.append("SOURCE COVERAGE")
    lines.append("-" * 78)
    lines.append(
        "%-30s %-9s %-4s %-14s %-8s %5s %5s %5s %5s %5s" % (
            "source", "type", "tier", "region", "sector", "fetch",
            "acc", "rej", "dup", "useful",
        )
    )
    for r in sorted(
        report["source_coverage"],
        key=lambda x: -x["fetched"],
    ):
        lines.append(
            "%-30s %-9s %-4s %-14s %-8s %5d %5d %5d %5d %5d" % (
                r["name"][:30], r["type"][:9], r["tier"],
                r["region"][:14], r["sector"][:8],
                r["fetched"], r["accepted"], r["editorial_rejected"],
                r["duplicates"], r["useful"],
            )
        )
    lines.append("")
    lines.append("SECTOR COVERAGE")
    lines.append("-" * 78)
    dist = report["sector_coverage"]["distribution"]
    for sec, n in dist.items():
        lines.append("  %-16s %5d" % (sec, n))
    lines.append("")
    lines.append("REGIONAL COVERAGE (event geography)")
    lines.append("-" * 78)
    rdist = report["regional_coverage"]["distribution"]
    for reg, n in rdist.items():
        lines.append("  %-22s %5d" % (reg, n))
    sdist = report["regional_coverage"].get("sub_regions") or {}
    if sdist:
        lines.append("  SUB-REGIONS")
        for reg, n in sdist.items():
            lines.append("    %-32s %5d" % (reg, n))
    lines.append("")
    lines.append("MISSING SECTORS: %s" % (
        report["missing_sectors"] or "none"))
    lines.append("WEAK SECTORS:    %s" % (
        report["weak_sectors"] or "none"))
    lines.append("MISSING REGIONS: %s" % (
        report["missing_regions"] or "none"))
    lines.append("WEAK REGIONS:    %s" % (
        report["weak_regions"] or "none"))
    lines.append("OVERREPRESENTED: %s" % (
        report["overrepresented_sources"] or "none"))
    lines.append("LOW-VALUE SOURCES:")
    if report["low_value_sources"]:
        for lv in report["low_value_sources"]:
            lines.append("  %-30s rejection=%.0f%% useful=%.0f%%" % (
                lv["name"], lv["editorial_rejection_rate"] * 100,
                lv["useful_share"] * 100,
            ))
    else:
        lines.append("  none")
    lines.append("SOURCE REDUNDANCY (same publisher, multiple feeds):")
    red = report["source_redundancy"]
    if red:
        for dom, names in red.items():
            lines.append("  %-24s %s" % (dom, ", ".join(names)))
    else:
        lines.append("  none")
    lines.append("SOURCE FAILURE RATE: %.1f%%" % (
        report["source_failure_rate"] * 100))
    for f in report["failed_sources"]:
        lines.append("  FAILED %-30s status=%s error=%s" % (
            f["name"], f.get("status"), (f.get("error") or "")[:60]))

    # ---- Phase C additions ----
    lines.append("")
    lines.append("SOURCE CONCENTRATION (useful events)")
    lines.append("-" * 78)
    conc = report.get("source_concentration") or {}
    lines.append(
        "  top-1 %.0f%%  top-3 %.0f%%  top-5 %.0f%%  top-10 %.0f%%  "
        "HHI %.3f" % (
            (conc.get("top_1_share") or 0) * 100,
            (conc.get("top_3_share") or 0) * 100,
            (conc.get("top_5_share") or 0) * 100,
            (conc.get("top_10_share") or 0) * 100,
            conc.get("hhi") or 0,
        )
    )
    raw = (conc.get("raw_fetched") or {})
    lines.append(
        "  raw-fetched: top-1 %.0f%%  top-3 %.0f%%  top-5 %.0f%%  "
        "HHI %.3f" % (
            (raw.get("top_1_share") or 0) * 100,
            (raw.get("top_3_share") or 0) * 100,
            (raw.get("top_5_share") or 0) * 100,
            raw.get("hhi") or 0,
        )
    )
    lines.append("  denominator: %s" % conc.get("denominator"))

    lines.append("")
    lines.append("SECTOR SOURCE COVERAGE")
    lines.append("-" * 78)
    lines.append("  %-16s %6s %6s %6s %8s %7s" % (
        "sector", "cfg", "ok", "fail", "articles", "useful"))
    for sec, b in (report.get("sector_coverage_by_source") or {}).items():
        lines.append("  %-16s %6d %6d %6d %8d %7d" % (
            sec[:16], b["configured"], b["successful"], b["failed"],
            b["articles"], b["useful"]))

    lines.append("")
    lines.append("REGIONAL SOURCE COVERAGE")
    lines.append("-" * 78)
    lines.append("  %-18s %6s %6s %6s %8s %7s" % (
        "region", "cfg", "ok", "fail", "articles", "useful"))
    for reg, b in (report.get("region_coverage_by_source") or {}).items():
        lines.append("  %-18s %6d %6d %6d %8d %7d" % (
            reg[:18], b["configured"], b["successful"], b["failed"],
            b["articles"], b["useful"]))

    lines.append("")
    lines.append("UNIQUE-EVENT CONTRIBUTION (top 10 / bottom 5)")
    lines.append("-" * 78)
    contrib = report.get("source_unique_event_contribution") or []
    lines.append("  %-32s %6s %7s %8s %9s" % (
        "source", "fetch", "useful", "dup", "useful%"))
    top = contrib[:10]
    bottom = [c for c in contrib if c["useful"] > 0][-5:]
    for c in top + bottom:
        lines.append("  %-32s %6d %7d %8d %8.0f%%" % (
            c["name"][:32], c["fetched"], c["useful"],
            c["duplicates"],
            (c["useful_news_rate"] or 0) * 100))

    lines.append("")
    lines.append("UNDERREPRESENTED SOURCES")
    lines.append("-" * 78)
    under = report.get("underrepresented_sources") or []
    if under:
        for u in under:
            lines.append("  %-30s fetched=%d useful=0 sector=%s region=%s" % (
                u["name"][:30], u["fetched"], u["sector"], u["region"]))
    else:
        lines.append("  none")

    hist = report.get("source_health_history") or []
    if any(h["attempt_count"] for h in hist):
        lines.append("")
        lines.append("SOURCE HEALTH HISTORY (persistent, all runs)")
        lines.append("-" * 78)
        lines.append("  %-30s %5s %5s %5s %7s %7s %9s" % (
            "source", "att", "ok", "fail", "success", "useful%",
            "last_err"))
        for h in hist:
            if h["attempt_count"] == 0:
                continue
            lines.append("  %-30s %5d %5d %5d %6.0f%% %6.0f%% %9s" % (
                h["name"][:30], h["attempt_count"], h["success_count"],
                h["failure_count"],
                (h["success_rate"] or 0) * 100,
                (h["articles_fetched_total"]
                 and h["articles_fetched_total"] > 0
                 and (h["articles_accepted_total"]
                      / h["articles_fetched_total"]) * 100
                 or 0),
                (h["last_error"] or "")[:9],
            ))

    lines.append("")
    lines.append("OVERREPRESENTED SOURCES (fetched volume):")
    over = report.get("overrepresented_sources") or []
    lines.append("  %s" % (over or "none"))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Audit the WorldNews source network coverage.")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--live", action="store_true",
                    help="fetch all feeds and audit actual articles")
    ap.add_argument("--health", default=None,
                    help="source_health.json from a real run")
    ap.add_argument("--db", default=None,
                    help="SQLite database with persistent source-health "
                         "history (data/news.db)")
    ap.add_argument("--json", action="store_true",
                    help="print machine-readable JSON")
    ap.add_argument("--out", default=None,
                    help="write JSON report to a file")
    args = ap.parse_args(argv)

    feeds, cfg = load_feeds(args.config)

    rows = None
    health = None
    health_rows = None
    if args.live:
        from src.collector import collect
        rows, health = collect(
            cfg["feeds"],
            int(cfg.get("max_feed_entries_per_source", 25)),
        )
    elif args.health:
        with open(args.health) as fh:
            health = json.load(fh).get("sources")
    if args.db:
        import sqlite3
        from src.storage import source_health_rows
        conn = sqlite3.connect(args.db)
        try:
            health_rows = source_health_rows(conn)
        finally:
            conn.close()

    report = compute_audit(
        feeds,
        rows=rows,
        health=health,
        cfg=cfg,
        health_rows=health_rows,
    )

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=1)

    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
