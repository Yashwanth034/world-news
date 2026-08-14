"""Transparent, evidence-based importance model (Phase D).

The old priority score was a keyword sum ("earthquake = +35"), so a
routine article could outrank a major development.  This module
replaces it with a bounded, explainable multi-signal score:

    importance = impact + urgency + novelty + scope
               + reliability + corroboration
               + significance (human/economic/security/scientific/
                 infrastructure/geopolitical, tiered + capped)
               + coverage_adjustment (soft, <= +3)

Design principles:

- Severity comes from the actual FACTS (magnitude, casualties,
  displacement, affected systems), not from sector keywords: an
  M2 earthquake scores near-zero impact, an M7.5 in a populated
  area scores very high.  "earthquake = +35" does not exist here.
- Significance dimensions are TIERED (strong/moderate/weak terms
  and fact levels) and CAPPed, so no single sector dominates and
  no fixed sector priority exists.  A major bank failure, an
  actively exploited critical vulnerability and a peace treaty
  can all score high - each on its own evidence.
- Novelty is event-level: NEW gets full novelty, a material
  UPDATE gets a boost, a DUPLICATE gets none.  Duplicates are
  already suppressed by event memory, so the queue only ever
  sees NEW/UPDATE candidates.
- Source reliability and corroboration raise CONFIDENCE, never
  importance by themselves: a reliable source reporting trivia
  still scores low.
- Coverage awareness is a <=+3 soft adjustment only: it can help
  a comparable story from an under-covered sector win a tie, it
  can never lift a 50-point story over a 90-point one.

Every candidate gets an explainable `importance_breakdown` dict
(never shown on Telegram; used for debugging/auditing).
"""

import re

# ---------------------------------------------------------------------------
# Fact extraction (severity from actual numbers/context)
# ---------------------------------------------------------------------------

_MAGNITUDE_RE = re.compile(
    r"(?:\b(?:magnitude|m|mw)\s*[-\s]?\s*(\d+(?:\.\d+)?)\b)"
    r"|(?:\b(\d+(?:\.\d+)?)\s*-?\s*magnitude\b)"
    r"|(?:\b(\d+(?:\.\d+)?)\s*(?:mag|m)\s*quake\b)",
    re.IGNORECASE,
)

# Casualty / scale keywords matched against nearby numbers so
# "150 feared dead", "more than 100 people killed", "toll rises
# to 180" and "thousands evacuated" are all captured regardless of
# the exact wording between number and keyword.
_DEAD_WORDS = re.compile(
    r"(?:killed|kills|kill|killing|dead|deaths|fatalities|die|"
    r"dies|died|death toll|toll)",
    re.IGNORECASE,
)
_INJURED_WORDS = re.compile(
    r"(?:injured|wounded|hurt|hospitalised|hospitalized)",
    re.IGNORECASE,
)
_DISPLACED_WORDS = re.compile(
    r"(?:displaced|evacuated|fleeing|homeless)",
    re.IGNORECASE,
)
_MISSING_WORDS = re.compile(r"missing", re.IGNORECASE)
_AFFECTED_WORDS = re.compile(
    r"(?:affected|without power|hit by)",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Word magnitudes used by feeds that write "thousands displaced" or
# "hundreds feared dead" instead of digits.  Values are conservative
# (e.g. "thousands" counts as 3000, not 9999) so severity is never
# overstated.
_WORD_MAGNITUDES = (
    (r"\btens of thousands\b", 30000),
    (r"\bhundreds of thousands\b", 300000),
    (r"\bmillions\b", 3000000),
    (r"\bthousands\b", 3000),
    (r"\bhundreds\b", 300),
    (r"\bdozens\b", 30),
)
_WORD_MAG_RE = [
    (re.compile(p), v) for p, v in _WORD_MAGNITUDES
]


def _nearby_numbers(text, keyword_re, window=8):
    """Largest number appearing within `window` words of a keyword.

    Digit counts ("500 killed") and word magnitudes ("thousands
    displaced") are both recognised; the larger wins.
    """
    words = re.split(r"\s+", text)
    best = 0
    for i, w in enumerate(words):
        if not keyword_re.search(w):
            continue
        lo = max(0, i - window)
        hi = min(len(words), i + window)
        for chunk in words[lo:hi]:
            m = _NUMBER_RE.search(chunk)
            if m:
                try:
                    best = max(best, int(m.group().replace(",", "")))
                except ValueError:
                    pass
            for pat, val in _WORD_MAG_RE:
                if pat.search(chunk):
                    best = max(best, val)
    return best

_CRISIS_TERMS = {
    "state of emergency", "emergency declared", "declares emergency",
    "national emergency", "collapse", "collapsed", "collapsing",
    "catastrophic", "catastrophe", "devastating", "destroyed",
    "worst in", "record high", "all-time high", "worst-ever",
}


def _has_term(lower_text, term):
    """Word-boundary-aware term match.

    Multi-word terms use substring matching ("market crash" is safe
    as a unit); single-word terms require word boundaries so "war"
    never matches inside "software" or "warning".
    """
    if len(term.split()) > 1:
        return term in lower_text
    return re.search(r"\b" + re.escape(term) + r"\b", lower_text) is not None


def _any_term(lower_text, terms):
    return any(
        _has_term(lower_text, t) for t in terms
    )

_DEVELOPMENT_TERMS = {
    "breaking", "developing", "update", "updated", "warns", "warning",
    "rises", "climbs", "reaches", "announces", "declares", "suspends",
    "confirms", "launches", "withdraws", "recalls", "orders", "now",
}


def _to_num(text):
    try:
        return int(text.replace(",", "").replace(".", ""))
    except (TypeError, ValueError):
        try:
            return int(float(text.replace(",", "")))
        except (TypeError, ValueError):
            return 0


def _extract_numbers(pattern, text):
    return [
        _to_num(m.group(1))
        for m in pattern.finditer(text)
        if m.group(1)
    ]


def _max_of(pattern, text):
    nums = _extract_numbers(pattern, text)
    return max(nums) if nums else 0


def _facts(text):
    """Structured severity facts from the article text."""
    mags = []
    for m in _MAGNITUDE_RE.finditer(text):
        val = next((g for g in m.groups() if g), None)
        if val:
            try:
                mags.append(float(val))
            except ValueError:
                pass
    lower = text.lower()
    # Aircraft / maritime / industrial accidents are high-impact
    # regardless of the exact casualty wording.
    major_incident = any(
        t in lower for t in (
            "plane crash", "aircraft crash", "jet crash",
            "passenger jet", "airliner", "train crash",
            "derailment", "ship sinks", "vessel sinks", "capsizes",
            "bridge collapse", "dam collapse", "plant explosion",
            "factory explosion", "mine collapse", "building collapse",
            "mass shooting", "hostage", "gas leak", "chemical spill",
            # active exploitation / attacks with broad reach
            "cyberattack", "cyber attack", "actively exploited",
            "critical vulnerability", "ransomware", "data breach",
            "shipping halted", "shipping suspended", "suspends routes",
            "tanker attacks", "pipeline rupture", "gas pipeline",
            "nuclear plant", "reactor", "meltdown",
            # financial crises with broad market reach
            "bank failure", "market crash", "financial crisis",
            "currency collapse", "emergency rate", "debt crisis",
            "banking crisis",
            # assassination / head-of-state death
            "assassinated", "assassination", "president killed",
            "prime minister killed", "leader killed",
        )
    )
    return {
        "magnitude": max(mags) if mags else 0.0,
        "dead": _nearby_numbers(lower, _DEAD_WORDS),
        "injured": _nearby_numbers(lower, _INJURED_WORDS),
        "displaced": _nearby_numbers(lower, _DISPLACED_WORDS),
        "missing": _nearby_numbers(lower, _MISSING_WORDS),
        "affected": _nearby_numbers(lower, _AFFECTED_WORDS),
        "major_incident": major_incident,
        "crisis": any(t in lower for t in _CRISIS_TERMS),
    }


# ---------------------------------------------------------------------------
# Impact (max 34)
# ---------------------------------------------------------------------------

def _impact(facts):
    """Impact from magnitude + casualties + context (fact-driven)."""
    mag = facts["magnitude"]
    mag_score = 0
    if mag >= 8.0:
        mag_score = 30
    elif mag >= 7.0:
        mag_score = 26
    elif mag >= 6.0:
        mag_score = 18
    elif mag >= 5.0:
        mag_score = 12
    elif mag > 0:
        mag_score = 4

    dead = facts["dead"]
    if dead >= 1000:
        dead_score = 30
    elif dead >= 500:
        dead_score = 26
    elif dead >= 100:
        dead_score = 22
    elif dead >= 10:
        dead_score = 16
    elif dead >= 1:
        dead_score = 10
    else:
        dead_score = 0

    extra = 0.0
    if facts["injured"] >= 1000:
        extra = 6
    elif facts["injured"] >= 100:
        extra = 5
    elif facts["injured"] >= 10:
        extra = 3
    elif facts["injured"]:
        extra = 2
    if facts["displaced"] >= 100000:
        extra = max(extra, 6)
    elif facts["displaced"] >= 10000:
        extra = max(extra, 5)
    elif facts["displaced"] >= 1000:
        extra = max(extra, 4)
    elif facts["displaced"] >= 100:
        extra = max(extra, 3)
    if facts["missing"]:
        extra = max(extra, 2)
    if facts["affected"] >= 1000000:
        extra = max(extra, 6)
    elif facts["affected"] >= 100000:
        extra = max(extra, 5)

    score = max(mag_score, dead_score) + extra
    if facts["crisis"]:
        score = max(score, 8)
    # A passenger jet down, a bridge collapse, a mine disaster etc
    # is inherently high-impact even when the casualty count is
    # still being established ("feared dead").
    if facts.get("major_incident"):
        score = max(score, 18)
    # Mass-casualty ceilings: 1000+ dead or 1M+ affected can exceed
    # the default cap.
    if dead >= 1000 or facts["affected"] >= 1000000:
        score = max(score, 32)
    return round(min(34, score), 1)


# ---------------------------------------------------------------------------
# Urgency (max 10)
# ---------------------------------------------------------------------------

def _urgency(text, effective_at, now=None):
    score = 4.0  # baseline: it is current news
    lower = (text or "").lower()
    if any(t in lower for t in ("breaking", "developing", "live ")):
        score += 3
    if any(t in lower for t in ("warns", "warning", "urgent",
                                "emergency", "imminent")):
        score += 2
    if any(t in lower for t in ("rises", "climbs", "reaches", "now",
                                "continues")):
        score += 1

    if effective_at and now:
        try:
            from datetime import datetime, timezone
            eff = datetime.fromisoformat(
                effective_at.replace("Z", "+00:00")
            )
            if eff.tzinfo is None:
                eff = eff.replace(tzinfo=timezone.utc)
            age_hours = (now - eff).total_seconds() / 3600.0
            if age_hours < 1:
                score += 3
            elif age_hours < 6:
                score += 2
            elif age_hours < 24:
                score += 1
        except Exception:
            pass
    return round(min(10, score), 1)


# ---------------------------------------------------------------------------
# Novelty (event-level, max 10)
# ---------------------------------------------------------------------------

def _novelty(event_status, text):
    status = (event_status or "NEW").upper()
    if status == "UPDATE":
        score = 8.0  # material development is new information
    elif status == "NEW":
        score = 10.0
    else:  # DUPLICATE - never reaches the queue anyway
        score = 0.0
    lower = (text or "").lower()
    if any(t in lower for t in _DEVELOPMENT_TERMS):
        score = min(10, score + 1)
    return round(min(10, score), 1)


# ---------------------------------------------------------------------------
# Scope (max 8)
# ---------------------------------------------------------------------------

_GLOBAL_TERMS = (
    "worldwide", "globally", "across the world", "around the world",
    "global", "g7", "g20", "united nations", "u.n.", "world bank",
    "imf", "who said", "world health organization",
)

_MULTI_TERMS = (
    "cross-border", "multinational", "multilateral",
    "international community", "between the two countries",
    "between countries", "bilateral", "relations between",
    "global agreement", "global deal", "global summit",
    "international stability", "international relations",
    "world leaders", "global powers",
)


def _scope(text, region):
    """Local / National / Regional / Multi-country / Global."""
    lower = (text or "").lower()
    if any(t in lower for t in ("worldwide", "globally",
                                "across the world", "around the world",
                                "global", "g7", "g20", "world cup",
                                "world series", "olympics",
                                "united nations", "u.n.")):
        return "GLOBAL", 8
    if any(t in lower for t in _MULTI_TERMS):
        return "MULTI_COUNTRY", 7
    if region in ("Europe", "Africa", "Asia", "North America",
                  "South America", "Oceania"):
        return "REGIONAL", 5
    if region:
        return "NATIONAL", 6
    return "LOCAL", 3


# ---------------------------------------------------------------------------
# Reliability + corroboration (max 8 + 10)
# ---------------------------------------------------------------------------

def _reliability(item):
    tier = int(item.get("tier", 4) or 4)
    score = {1: 8, 2: 6, 3: 4, 4: 2}.get(tier, 2)
    if item.get("primary_source"):
        score += 2
    return round(min(8, score), 1)


def _corroboration(item):
    strong = int(item.get("strong_corroboration", 0) or 0)
    count = int(item.get("corroborating_sources", 0) or 0)
    if strong >= 3:
        return 10
    if strong == 2:
        return 8
    if strong == 1:
        return 6
    if count >= 3:
        return 5
    if count >= 1:
        return 3
    return 0


# ---------------------------------------------------------------------------
# Significance (tiered; each dimension max 8, total capped at 24)
# ---------------------------------------------------------------------------

# STRONG terms indicate a genuinely major event in that dimension.
_SIG_STRONG = {
    "human": (
        "massacre", "genocide", "famine", "starvation", "pandemic",
        "epidemic", "outbreak", "refugee crisis", "ethnic cleansing",
    ),
    "economic": (
        "market crash", "stock market crash", "bank failure",
        "bank collapses", "systemic", "default", "debt crisis",
        "recession", "depression", "hyperinflation", "trade war",
        "currency crisis", "banking crisis", "financial crisis",
        "emergency rate", "emergency move", "currency collapse",
        "market turmoil", "rate shock",
    ),
    "security": (
        "cyberattack", "cyber attack", "data breach", "ransomware",
        "actively exploited", "critical vulnerability",
        "exploited in the wild", "nuclear", "missile strike",
        "airstrike", "air strike", "invasion", "war", "terror attack",
        "shooting", "military operation", "drone attack",
    ),
    "scientific": (
        "breakthrough", "discovery", "discover", "first time",
        "cure", "vaccine", "nobel", "lands on", "reaches orbit",
        "launches", "major finding", "new species", "defeats",
        "clinical trials", "medical breakthrough", "superbug",
        "researchers found", "major study",
    ),
    "infrastructure": (
        "power grid", "blackout", "outage", "dam collapse",
        "bridge collapse", "nuclear plant", "reactor", "meltdown",
        "pipeline", "port closure", "airport shutdown", "plant shutdown",
        "cooling failure", "evacuated", "evacuation",
    ),
    "geopolitical": (
        "treaty", "peace treaty", "ceasefire", "coup",
        "referendum", "annex", "sanctions", "diplomatic rupture",
        "expels", "ambassador", "withdraws from", "invasion", "war",
        "alliance", "peace deal", "peace accord", "ceasefire deal",
        "decades-long conflict", "turning point", "historic agreement",
        "world leaders", "international stability", "assassinated",
        "assassination", "president killed", "prime minister killed",
        "head of state",
    ),
    "cultural": (
        "repatriation", "landmark deal", "world heritage",
        "returns looted", "national museum", "historic discovery",
        "world cup final", "world championship", "championship",
        "final victory", "wins the world cup", "shootout",
        "penalty shootout", "champions", "olympic gold",
    ),
}

# WEAK terms indicate the dimension is touched but not necessarily
# major; they score less than STRONG terms.
_SIG_WEAK = {
    "human": (
        "killed", "dead", "deaths", "fatalities", "injured",
        "displaced", "evacuated", "missing", "refugee", "protest",
        "hostage", "human rights", "violence", "casualties",
    ),
    "economic": (
        "tariff", "inflation", "interest rate", "central bank",
        "currency", "supply chain", "bankruptcy", "oil price",
        "economy", "gdp", "unemployment",
    ),
    "security": (
        "military", "troops", "defense", "defence", "missile",
        "drone", "intelligence", "weapons", "attack", "security forces",
    ),
    "scientific": (
        "scientists", "researchers", "study", "finds", "found",
        "research", "experiment", "astronomers", "mission",
    ),
    "infrastructure": (
        "power", "electricity", "transport", "rail", "highway",
        "bridge", "airport", "port", "water supply", "telecom",
        "internet", "grid", "dam", "utility",
    ),
    "geopolitical": (
        "diplomatic", "embassy", "summit", "relations", "border",
        "election", "vote", "parliament", "government", "minister",
        "president", "prime minister",
    ),
}


def _significance(text, facts):
    """Tiered significance: strong terms score 8, weak terms score 4
    per dimension, with a fact-driven human dimension that scales
    with casualty levels (so a 500-dead catastrophe reads as a
    catastrophe, not as a generic "killed" mention).  Total capped
    at 40 across the top dimensions."""
    lower = (text or "").lower()
    dims = {}
    for dim, strong in _SIG_STRONG.items():
        if _any_term(lower, strong):
            dims[dim] = 8
    for dim, weak in _SIG_WEAK.items():
        if dim in dims:
            continue
        if _any_term(lower, weak):
            dims[dim] = 4

    # Fact-driven human dimension: scales with actual casualties so
    # severity comes from the facts, not from wording alone.
    human = 0
    if facts["dead"] >= 1000:
        human = 12
    elif facts["dead"] >= 100:
        human = 10
    elif facts["dead"] >= 10:
        human = 8
    elif (facts["dead"] or facts["injured"] or facts["displaced"]
          or facts["missing"]):
        human = 6
    if facts["crisis"] and human:
        human = min(12, human + 2)
    if human:
        dims["human"] = max(dims.get("human", 0), human)

    total = sum(sorted(dims.values(), reverse=True)[:5])
    return dims, round(min(40, total), 1)


# ---------------------------------------------------------------------------
# Coverage awareness (soft, <= +3)
# ---------------------------------------------------------------------------

def _coverage_adjustment(sector, sector_source_counts=None):
    """Small nudge for under-covered sectors (soft tie-break only).

    `sector_source_counts` maps sector -> number of contributing
    sources in the current network (or None to skip).  A sector
    with very few sources can be naturally under-represented; a
    tiny boost (max 3) helps a comparable story there win a tie.
    A 50-point story never beats a 90-point one because of this.
    """
    if not sector_source_counts:
        return 0.0
    count = sector_source_counts.get(sector, 0)
    if count <= 0:
        return 0.0
    if count <= 2:
        return 3.0
    if count <= 4:
        return 2.0
    if count <= 6:
        return 1.0
    return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_importance(
    item,
    now=None,
    sector_source_counts=None,
    event_text=None,
):
    """Compute the transparent importance score for one candidate.

    `item` must carry: title, summary, event_status, tier,
    primary_source, corroborating_sources, strong_corroboration,
    effective_at.  Optional: sector, region, score, category.

    `event_text` (optional) is the event's canonical material
    (canonical title + canonical summary from event memory).  It
    makes importance EVENT-LEVEL: a thin follow-up article about
    a major event inherits the event's severity facts ("M7.4
    quake, 281 dead") instead of being judged only on its own
    wording, so a routine-sounding update cannot demote a
    genuinely major event.

    Returns (score, level, breakdown).
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    text = f"{title}. {summary}"
    if event_text:
        text = f"{event_text}. {text}"
    lower = text.lower()

    facts = _facts(lower)

    impact = _impact(facts)
    urgency = _urgency(lower, item.get("effective_at"), now)
    novelty = _novelty(item.get("event_status"), lower)
    scope_label, scope = _scope(lower, item.get("region"))
    reliability = _reliability(item)
    corroboration = _corroboration(item)
    sig_dims, significance = _significance(lower, facts)
    coverage = _coverage_adjustment(
        item.get("sector"),
        sector_source_counts,
    )

    score = round(
        impact + urgency + novelty + scope + reliability
        + corroboration + significance + coverage,
        1,
    )

    if score >= 85:
        level = "CRITICAL"
    elif score >= 70:
        level = "HIGH"
    elif score >= 50:
        level = "MEDIUM"
    else:
        level = "LOW"

    breakdown = {
        "impact": impact,
        "urgency": urgency,
        "novelty": novelty,
        "scope": scope_label,
        "scope_points": scope,
        "reliability": reliability,
        "corroboration": corroboration,
        "significance": significance,
        "coverage_adjustment": coverage,
        "facts": facts,
        "significance_dims": sig_dims,
    }

    return score, level, breakdown
