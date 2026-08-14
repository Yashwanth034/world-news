import re
from collections import Counter
from src.source_reliability import reliability_bonus, get_tier

URGENT_TERMS = {
    "earthquake","tsunami","hurricane","cyclone","tornado","wildfire",
    "volcano","eruption","evacuation","missile","airstrike","invasion",
    "explosion","plane crash","train crash","bridge collapse","coup",
    "market crash","bank failure","default","state of emergency",
    "data breach","cyberattack","terror attack"
}

CATEGORY_TERMS = {
    "finance": {
        "bank", "stocks", "stock market", "bond", "inflation",
        "interest rate", "central bank", "economy", "economic",
        "tariff", "trade", "earnings", "revenue", "ipo",
        "debt", "default", "economic collapse", "economic crisis",
        "energy crisis", "financial crisis", "sanctions"
    },

    "politics": {
        "president", "presidential", "prime minister",
        "government", "administration", "election",
        "parliament", "senate", "congress",
        "minister", "vote", "coalition",
        "sanctions", "diplomatic", "diplomacy",
        "political", "politics",
        "court", "courts", "appeals court",
        "federal court", "supreme court",
        "judge", "judges", "ruling",
        "legislation", "law", "bill",
        "white house", "presidency",
        "president trump", "appeal", "appeals"
    },

    "disaster": {
        "earthquake", "tsunami", "hurricane", "cyclone",
        "tornado", "flood", "wildfire", "wildfires",
        "volcano", "eruption", "landslide", "evacuation",
        "disaster"
    },

    "conflict": {
        "war", "attack", "airstrike", "missile", "invasion",
        "ceasefire", "coup", "military"
    },

    "technology": {
        "technology", "ai", "artificial intelligence", "chip",
        "semiconductor", "software", "cybersecurity",
        "cyberattack", "data breach", "robot",
        "drone", "drones"
    },

    "science": {
        "science", "research", "study", "scientist",
        "astronomy", "biology", "physics", "chemistry"
    },

    "space": {
        "space", "nasa", "esa", "jpl", "moon", "mars",
        "rocket", "satellite", "astronaut", "orbit",
        "spacecraft", "launch"
    },

    "health": {
        "health", "disease", "virus", "outbreak", "hospital",
        "who", "vaccine", "pandemic"
    },

    "environment": {
        "environment", "climate", "climate change", "pollution",
        "emissions", "deforestation", "biodiversity",
        "conservation", "wildlife", "crocodile", "crocodiles",
        "extreme rainfall", "rainfall"
    },

    "industry": {
        "company", "factory", "manufacturing", "oil", "gas",
        "energy", "automotive", "aviation", "shipping",
        "industry", "production"
    },

    "sports": {
        "football", "soccer", "cricket", "tennis", "basketball",
        "baseball", "golf", "formula 1", "f1", "olympics",
        "athlete", "championship", "tournament", "league"
    },
}

def _words(text):
    return set(re.findall(r"[a-z0-9][a-z0-9'-]*", (text or "").lower()))

def _category(text, source_category):
    raw = (source_category or "").lower().strip()

    # These are geographic/source labels, NOT article topics.
    regional_categories = {
        "world",
        "africa",
        "india",
        "japan",
        "china",
        "south-korea",
        "southeast-asia",
        "europe",
        "middle-east",
        "latin-america",
        "canada",
        "australia",
        "pacific",
        "south-asia",
        "east-asia",
        "oceania",
    }

    lower = (text or "").lower()

    scores = {}

    # Count OCCURRENCES of each term (word-boundary matched), not
    # mere presence: a headline that says "football" twice is a
    # sports story, and a regional feed label is never the topic.
    for category, terms in CATEGORY_TERMS.items():
        score = 0
        for term in terms:
            if term in _TERM_RES[category]:
                score += len(_TERM_RES[category][term].findall(lower))
        scores[category] = score

    best_score = max(scores.values())

    if best_score == 0:
        return "world"

    tied = [
        category
        for category, score in scores.items()
        if score == best_score
    ]

    # Stable tie-break: prefer the source's own topic label, then
    # the first category in definition order.
    if len(tied) > 1 and raw in tied:
        best_category = raw
    else:
        best_category = tied[0]

    # A regional/source label should never become the article topic.
    if raw in regional_categories:
        if best_score >= 2:
            return best_category
        return "world"

    # If the source already supplies a specific topic category,
    # preserve it unless the article strongly indicates another topic.
    known_topics = set(CATEGORY_TERMS.keys())

    if raw in known_topics:
        if best_score >= 2 and best_category != raw:
            return best_category
        return raw

    if best_score >= 2:
        return best_category

    return "world"


def _compile_terms():
    """Word-boundary regexes per term so short terms ("ai",
    "f1", "bank") never match inside unrelated words."""
    res = {}
    for category, terms in CATEGORY_TERMS.items():
        res[category] = {
            term: re.compile(
                r"\b" + re.escape(term) + r"\b",
                re.IGNORECASE,
            )
            for term in terms
        }
    return res


_TERM_RES = _compile_terms()

def classify(title, summary, source_category, item=None):
    item = item or {}
    text = f"{title} {summary}".lower()
    category = _category(text, source_category)
    urgency_hits = [term for term in URGENT_TERMS if term in text]
    base = 35 + min(25, len(urgency_hits) * 8)
    base += reliability_bonus(item)
    if item.get("primary_source"):
        base += 10
    if len(summary or "") >= 180:
        base += 5
    score = max(0, min(100, base))
    confidence = "high" if item.get("primary_source") else ("medium" if get_tier(item) <= 2 else "low")
    return {
        "category": category,
        "score": score,
        "confidence": confidence,
        "urgency_terms": urgency_hits,
    }

def _tokens(text):
    return _words(text)

def _similarity(a, b):
    aa, bb = _tokens(a), _tokens(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, len(aa | bb))
def verify(item, all_items):
    title = item.get("title", "")
    matches = []

    for other in all_items:
        if other.get("id") == item.get("id"):
            continue

        # A source cannot independently corroborate itself.
        if other.get("source") == item.get("source"):
            continue

        sim = _similarity(title, other.get("title", ""))

        if sim >= 0.38:
            matches.append((sim, other))

    matches.sort(reverse=True, key=lambda x: x[0])

    corroborating = []
    strong = []
    seen_sources = set()

    for sim, other in matches:
        source = other.get("source")

        if not source or source in seen_sources:
            continue

        seen_sources.add(source)
        corroborating.append(other)

        if other.get("tier", 4) <= 2:
            strong.append(other)

    return {
        "corroborating_sources": len(corroborating),
        "strong_corroboration": len(strong),
        "corroborating_source_names": [
            x.get("source") for x in strong[:5]
        ],
        "verified_match_count": len(matches),
    }
