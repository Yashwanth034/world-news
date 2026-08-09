"""Conservative Telegram briefing builder.

Pure rules over pipeline data. No generation:

- Every sentence in a briefing exists VERBATIM in a source
  summary.
- Sentences retain source provenance internally.
- Event grouping requires multiple meaningful signals;
  false merges are worse than short briefings.
- Materially conflicting facts are not reconciled: the
  lower-ranked version of a disputed detail is dropped.
"""
import re

from src.formatter import clean, split_sentences
from src.telegram_scheduler import story_age_minutes

# ---------------------------------------------------------
# Public labels
# ---------------------------------------------------------

BREAKING = "\U0001F6A8 BREAKING"
JUST_IN = "\u26A1 JUST IN"
NEWS = "\U0001F4F0 NEWS"
UPDATE = "\U0001F504 UPDATE"

URGENT_CATEGORIES = {
    "conflict",
    "disaster",
    "politics",
    "finance",
    "health",
    "cybersecurity",
    "world",
}

# ---------------------------------------------------------
# Conservative event grouping
# ---------------------------------------------------------

STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "and", "or", "as", "is", "are",
    "was", "were", "be", "been", "has", "have", "had", "it",
    "its", "this", "that", "these", "those", "their", "they",
    "them", "his", "her", "over", "under", "after", "before",
    "during", "into", "about", "more", "most", "new", "says",
    "said", "amid", "live", "watch", "video", "photo", "report",
    "reports", "update", "updates", "could", "will", "would",
    "may", "might", "up", "down", "what", "who", "when",
    "where", "why", "how", "not", "no", "yes", "but", "while",
    "across", "near", "around", "between", "among", "via",
    "including", "against", "aftermath", "latest", "first",
    "world", "today", "yesterday", "officials", "people",
}

WORD_RE = re.compile(
    r"[A-Za-z0-9]+(?:[\u2019'-][A-Za-z0-9]+)*"
)


def _tokens(text):
    words = WORD_RE.findall(
        str(text or "").lower()
    )
    return {
        w
        for w in words
        if len(w) >= 4
        and w not in STOPWORDS
    }


def same_event(a, b):
    """Whether two items are the same event.

    Conservative: requires multiple meaningful signals.
    Geography or urgency alone is never enough.
    """
    a_id = a.get("event_id")
    b_id = b.get("event_id")

    if a_id and b_id and a_id == b_id:
        return True

    ta = _tokens(a.get("title"))
    tb = _tokens(b.get("title"))

    shared = ta & tb

    if len(shared) < 2:
        return False

    jaccard = len(shared) / max(
        1,
        len(ta | tb),
    )

    shared_urgency = (
        set(
            a.get("urgency_terms")
            or []
        )
        & set(
            b.get("urgency_terms")
            or []
        )
    )

    # Categories may differ between sources ("world" vs
    # "disaster") but both must be urgent-domain for a
    # merge: non-urgent stories never merge on tokens.
    urgent_category = (
        a.get("category") in URGENT_CATEGORIES
        and b.get("category") in URGENT_CATEGORIES
    )

    if (
        len(shared) >= 4
        and jaccard >= 0.30
        and urgent_category
    ):
        return True

    if (
        len(shared) >= 2
        and jaccard >= 0.15
        and shared_urgency
        and urgent_category
    ):
        return True

    return False


def _primary_of(group):
    """Pick the strongest item: score, then primary source,
    then better tier."""
    def key(item):
        return (
            item.get("score", 0)
            or item.get("priority_score", 0)
            or 0,
            int(
                bool(item.get("primary_source"))
            ),
            -int(item.get("tier", 4)),
        )

    return sorted(
        group,
        key=key,
        reverse=True,
    )[0]


def group_items(items):
    """Cluster items into events.

    An item joins a cluster only when it matches the
    cluster's primary item, avoiding chain drift where
    A~B and B~C would wrongly merge an unrelated C.
    """
    groups = []

    for item in items:

        placed = False

        for group in groups:

            if same_event(
                item,
                _primary_of(group),
            ):
                group.append(item)
                placed = True
                break

        if not placed:
            groups.append([item])

    return groups


# ---------------------------------------------------------
# Sentence aggregation with provenance
# ---------------------------------------------------------

NUMBER_RE = re.compile(r"\d[\d,.]*")
UNIT_RE = re.compile(
    r"(?:"
    r"(?:more than|at least|nearly|about|around|over|"
    r"up to|almost|roughly)?\s*"
    r"(\d[\d,.]*)\s*"
    r"(million|billion|thousand|percent|%|people|residents|"
    r"homes|families|troops|soldiers|killed|injured|dead|"
    r"displaced|evacuated|evacuees|sq miles|sq km|sq m|"
    r"miles|km|hectares|acres|fires|officers|hostages)?"
    r")",
    re.IGNORECASE,
)


def _number_units(text):
    """unit -> normalized number pairs in a sentence."""
    pairs = []

    for m in UNIT_RE.finditer(text):
        number = m.group(1)
        unit = m.group(2)

        if unit:
            pairs.append(
                (
                    unit.lower(),
                    re.sub(
                        r"[.,]",
                        "",
                        number,
                    ),
                )
            )

    return pairs


def _conflicting(a, b):
    """Whether two sentences materially disagree.

    A conflict requires: overlapping vocabulary AND a shared
    numeric unit with different values. "36 sq miles" and
    "20,000 people" do not conflict; "20,000" and "30,000"
    with the same unit do.
    """
    shared = (
        _tokens(a["text"])
        & _tokens(b["text"])
    )

    if len(shared) < 2:
        return False

    units_a = dict(
        _number_units(a["text"])
    )
    units_b = dict(
        _number_units(b["text"])
    )

    common_units = set(
        units_a
    ) & set(
        units_b
    )

    for unit in common_units:

        if (
            units_a[unit] != units_b[unit]
        ):
            return True

    return False


def aggregate_sentences(group, primary):
    """Verbatim sentences from the whole event group,
    deduplicated, with source provenance retained.

    Higher-ranked items come first. A sentence that
    materially conflicts with an already-accepted
    sentence is dropped (disputed detail omitted).
    """
    ranked = sorted(
        group,
        key=lambda x: (
            x.get("score", 0)
            or x.get("priority_score", 0)
            or 0,
            int(
                bool(x.get("primary_source"))
            ),
            -int(x.get("tier", 4)),
        ),
        reverse=True,
    )

    seen = set()
    accepted = []

    for item in ranked:

        source = item.get("source") or "Unknown"

        for sentence in split_sentences(
            item.get("summary")
        ):

            key = sentence.lower()

            if key in seen:
                continue

            seen.add(key)

            row = {
                "text": sentence,
                "source": source,
                "item_id": (
                    item.get("id")
                    or item.get("story_id")
                ),
            }

            if any(
                _conflicting(row, other)
                for other in accepted
            ):
                continue

            accepted.append(row)

    # Primary source sentences first, then corroborators,
    # preserving each source's internal order.
    primary_rows = [
        r for r in accepted
        if r["source"]
        == (primary.get("source") or "Unknown")
    ]
    other_rows = [
        r for r in accepted
        if r["source"]
        != (primary.get("source") or "Unknown")
    ]

    return primary_rows + other_rows


# ---------------------------------------------------------
# Conservative bullet extraction (literal evidence only)
# ---------------------------------------------------------

LOCATION_RE = re.compile(
    r"\b(?:in|across|near|around|at)\s+"
    r"([A-Z][A-Za-z\u2019'-]*(?:\s+(?:"
    r"[A-Z][A-Za-z\u2019'-]*|(?:city|town|state|province|"
    r"county|island|prefecture|region|capital|coast|valley|"
    r"bay|river|village|district|area|park|north|south|east|"
    r"west|central|northern|southern|eastern|western|"
    r"national|metropolitan)\b"
    r")){0,2})"
)

STATUS_PHRASES = [
    "state of emergency",
    "out of control",
    "under control",
    "continues to",
    "still burning",
    "under way",
    "underway",
]

IMPACT_RE = re.compile(
    r"(?:more than|at least|nearly|about|around|over|"
    r"up to|almost|roughly)?\s*"
    r"[\d,.]+\s*"
    r"(?:people|residents|homes|families|troops|soldiers|"
    r"killed|injured|dead|displaced|evacuated|evacuees|"
    r"fires|hostages|officers)",
    re.IGNORECASE,
)

NEXT_RE = re.compile(
    r"\b(?:expected to|is expected|officials (?:said|say|"
    r"warned)|continues to|are (?:continuing|working|"
    r"expected))",
    re.IGNORECASE,
)


def extract_bullets(rows):
    """Bullets from literal sentence evidence only.

    Returns a list of {"icon", "label", "text"}.
    """
    bullets = []
    texts = [r["text"] for r in rows]

    # Location: first credible "in <Place>" match.
    for text in texts:
        match = LOCATION_RE.search(text)

        if not match:
            continue

        location = match.group(1).strip()

        if (
            len(location.split()) <= 4
            and not location.lower().startswith(
                "the "
            )
        ):
            bullets.append(
                {
                    "icon": "\U0001F4CD",
                    "label": "Location",
                    "text": location,
                }
            )
            break

    # Status: first literal phrase match.
    for phrase in STATUS_PHRASES:

        for text in texts:
            if phrase in text.lower():
                bullets.append(
                    {
                        "icon": "\u26A0\uFE0F",
                        "label": "Status",
                        "text": phrase,
                    }
                )
                break

        if len(bullets) >= 2:
            break

    # Impact: first number+unit phrase with a people/impact
    # unit, e.g. "more than 20,000 people".
    for text in texts:
        match = IMPACT_RE.search(text)

        if match:
            impact = clean(match.group(0))

            if impact:
                bullets.append(
                    {
                        "icon": "\U0001F465",
                        "label": "Impact",
                        "text": impact,
                    }
                )
                break

    # Next steps: first full sentence that reports what
    # happens next.
    for row in rows:
        if NEXT_RE.search(row["text"]):
            bullets.append(
                {
                    "icon": "\u27A1\uFE0F",
                    "label": "Next",
                    "text": row["text"],
                }
            )
            break

    return bullets


# ---------------------------------------------------------
# Headline cleaning (never paraphrases, never invents)
# ---------------------------------------------------------

SOURCE_SUFFIX_RE = re.compile(
    r"\s*(?:-\s*|[\u2013\u2014]\s*|\|\s*)"
    r"(?:BBC(?: World)?|CNN|Reuters|AP|Al Jazeera|"
    r"The Guardian|Guardian|NPR|Sky News|France24|"
    r"The Independent|Independent|DW|Euronews|"
    r"The Times of India|Nikkei Asia|The Japan Times|"
    r"Global Times|South China Morning Post|SCMP|"
    r"Los Angeles Times|The Wall Street Journal|WSJ|"
    r"Financial Times|FT|Bloomberg|CNBC|ABC News|"
    r"CBS News|NBC News|The Washington Post|"
    r"USA Today|The Economist|Forbes|Time|Newsweek|"
    r"Politico|Axios|The Hill|The Verge|Ars Technica|"
    r"Wired|Engadget|TechCrunch)$",
    re.IGNORECASE,
)

PREFIX_RE = re.compile(
    r"^(?:Live|Watch|Video|Update|Breaking|Flash)"
    r"[:\s-]+\s*",
    re.IGNORECASE,
)


def clean_headline(title):
    """Remove RSS artifacts without changing the headline."""
    headline = clean(title)

    if not headline:
        return headline

    headline = SOURCE_SUFFIX_RE.sub(
        "",
        headline,
    )
    headline = PREFIX_RE.sub(
        "",
        headline,
    )
    headline = clean(headline)

    if headline:
        headline = (
            headline[0].upper()
            + headline[1:]
        )

    return headline


# ---------------------------------------------------------
# Public label selection
# ---------------------------------------------------------


def public_label(
    item,
    just_in_freshness_minutes=15,
    now=None,
):
    """One of BREAKING / JUST IN / NEWS / UPDATE.

    BREAKING needs urgency terms + non-low confidence + a
    high score + verification (primary source or strong
    corroboration) + an urgent category. Priority level is
    never used alone. JUST IN needs freshness AND importance.
    """
    status = item.get(
        "event_status",
        "NEW",
    )
    score = item.get("score", 0) or 0
    confidence = item.get("confidence")
    urgent = bool(
        item.get("urgency_terms")
    )
    category = item.get("category")
    primary = bool(
        item.get("primary_source")
    )
    strong = int(
        item.get("strong_corroboration", 0)
    )

    if (
        status == "UPDATE"
        and score >= 60
    ):
        return UPDATE

    if (
        urgent
        and confidence != "low"
        and score >= 80
        and (primary or strong >= 1)
        and category in URGENT_CATEGORIES
    ):
        return BREAKING

    age = story_age_minutes(item, now)

    if (
        age is not None
        and age <= just_in_freshness_minutes
        and confidence != "low"
        and score >= 60
    ):
        return JUST_IN

    return NEWS


# ---------------------------------------------------------
# Briefing assembly
# ---------------------------------------------------------


def build_briefing(
    primary,
    group,
    just_in_freshness_minutes=15,
    now=None,
):
    """Build the enriched briefing for one event cluster.

    Returns a dict consumed by telegram_formatter.
    """
    rows = aggregate_sentences(
        group,
        primary,
    )

    primary_source = (
        primary.get("source")
        or "Unknown"
    )

    contributing = [
        r["source"]
        for r in rows
        if r["source"] != primary_source
    ]

    corroborating = []

    for source in contributing:
        if source not in corroborating:
            corroborating.append(source)

    opening = rows[:2]

    bullets = extract_bullets(rows)

    sentence_texts = [
        r["text"] for r in rows
    ]

    bullet_sentences = {
        b["text"]
        for b in bullets
        if b["text"] in sentence_texts
    }

    body_rows = [
        r["text"]
        for r in rows[len(opening):]
        if r["text"] not in bullet_sentences
    ]

    return {
        "headline": clean_headline(
            primary.get("title")
        ),
        "label": public_label(
            primary,
            just_in_freshness_minutes,
            now,
        ),
        "opening": [
            r["text"] for r in opening
        ],
        "body": body_rows,
        "bullets": bullets,
        "sentences": rows,
        "source": primary_source,
        "corroborating": corroborating,
        "url": primary.get("url"),
    }
