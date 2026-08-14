"""Cross-run event memory with an immutable canonical identity.

decide() classifies every incoming story against each event the
pipeline has recently seen:

    NEW       - no existing event matches: a new event is created.
    DUPLICATE - the story belongs to a matched event and adds no
                material fact (reworded retelling, same toll,
                reaction/statement without new information).
    UPDATE    - the story belongs to a matched event and adds a
                material development (a changed death toll, a new
                consequence, an emergency declaration, an arrest,
                a ceasefire, a signed deal, ...).

EVENT IDENTITY MODEL
--------------------

An event's identity is anchored ONCE, to the first story that
created it, and is never broadened by later stories.  Every
event's persisted state therefore has two clearly separated
halves:

    identity      - IMMUTABLE.  The compact event-defining core:
                    event-type words, primary actions, distinctive
                    entities, locations, bound facts (impact
                    pairs), storm names, magnitudes, and the
                    content tokens of the original title+summary.
                    This is the ONLY matching surface.

    accumulated   - grows with every merged story.  Titles,
                    sources, numbers, consequences, actions,
                    summary and development facts accumulate so
                    that material-change detection and reporting
                    remember the event's history.  NEVER used to
                    decide whether a story matches.

The split is structural, not a tuning knob: a story that merges
into an event (even as an UPDATE) can never widen the event's
matching surface, so chain merges of the form

    A  <-(weak link)-  B  <-(shared vocabulary)-  C

cannot happen.  C only ever faces the original identity of A.

WHAT COUNTS AS IDENTITY (and what never does)
---------------------------------------------

Identity signals are restricted to strong event-defining
information:

  - event-type words (earthquake, typhoon, eclipse, blast, ...)
  - distinctive named entities (companies, named storms, people,
    organizations) - role/title words, temporal words, day/month
    names, years and live-blog boilerplate are excluded
  - places, used only as CONTEXT (never alone)
  - bound facts such as "100 killed" (number + unit together)

The following NEVER establish identity by themselves: country,
city, person, company, year, weekday, month, generic numbers,
generic actions (visit, strike, hit, say...), generic topics,
boilerplate, source names, and development vocabulary (death
toll, rescue, emergency, arrest...).

MATCHING RULES
--------------

A story matches an event only when it satisfies one of these
combinations (never a single shared attribute):

  R1  impact+type     shared bound fact AND shared event type/entity
  R2  type+place      shared event type AND shared place AND a
                      third signal (action, fact, or real overlap)
  R2b action-overlap  two+ shared action words with real overlap
                      (reworded phrasings that keep the event type)
  R3  entities        three+ shared distinctive entities; or two
                      with a topical link; or one with a place and
                      a topical link
  R4  action+place    a NARROW specific action (diplomatic rupture,
                      seizure, signed pact, ceasefire...) with a
                      shared place and lexical overlap
  R6  semantic        high-confidence paraphrase (semantic >= 0.35)
                      that still shares the event-type word

Only after one of those has already established SAME EVENT may a
development marker ("death toll", "emergency declared", "arrest",
"rescue") or a reaction marker ("calls it a tragedy") attach the
story - and even then only together with an identity anchor.
Development markers are never identity by themselves.

UPDATE vs DUPLICATE
-------------------

Once the event is matched, a material-change check runs against
the ACCUMULATED state: new development facts, new material
numbers, new consequences or new canonical actions produce an
UPDATE; everything else is suppressed as a DUPLICATE.  Repeats of
an already-known development (the same death-toll figure reported
twice, a second "official calls it a tragedy") stay suppressed.
"""

import hashlib
import json
import re
from datetime import datetime, timezone, timedelta

from src.telegram_briefing import (
    ENTITY_ALIASES,
    LOCATION_SET,
    NUMBER_RE,
    _actions,
    _entity_words,
    _stem_lite,
)

# ---------------------------------------------------------
# Words that must never count as identity signals
# ---------------------------------------------------------

# Live-blog/boilerplate words are not entities: "Follow today's
# news live", "Get the latest...", "Sign up..." appear in almost
# every live-blog summary and must never serve as shared
# identity between unrelated stories.
_BOILERPLATE_ENTITY_WORDS = frozenset(
    {
        "follow", "following", "get", "gets", "got", "live",
        "read", "reads", "see", "watch", "join", "sign",
        "share", "find", "findings", "make", "makes", "take",
        "takes", "keep", "look", "looks", "come", "comes",
        "go", "goes", "say", "says", "said", "tell", "told",
        "call", "calls", "called", "latest", "update",
        "updates", "updated", "breaking", "now", "here", "how",
        "why", "what", "when", "where", "new", "top", "best",
        "first", "more", "also", "still", "will", "could",
        "would", "should", "may", "might", "must", "can",
        "want", "need", "needs", "know", "knows", "think",
        "thinks", "believe", "believes", "see", "told", "show",
        "shows", "showed", "set", "plan", "plans", "planned",
        "expected", "reported", "confirmed", "announced",
    }
)


# Words that are capitalized in headlines but are NOT distinctive
# named entities: Google News appends the publisher to discovery
# titles ("... - ABC News & Headlines - Australian Broadcasting
# Corporation"), and geographic/topical words (Pacific, islands,
# coast, crisis, market) are context, never event identity.  Two
# stories that share only these words share a feed or a topic, not
# an event.  A "strong" entity is a distinctive entity outside
# this set.
_WEAK_ENTITY_WORDS = frozenset({
    # publisher / source suffixes appended to discovery titles
    "abc", "cbc", "pbs", "npr", "bbc", "cnn", "nbc", "cbs",
    "fox", "wral", "times", "herald", "post", "tribune",
    "journal", "telegraph", "daily", "weekly", "observer",
    "chronicle", "gazette", "mercury", "wire", "media",
    "network", "digital", "online", "radio", "television",
    "tv", "news", "headlines", "broadcasting", "corporation",
    "reports", "report", "station", "channel", "al", "jazeera",
    "reuters", "ap", "afp", "bloomberg", "forbes", "axios",
    "politico", "nyt", "wsj", "ft", "guardian", "economist",
    "verge", "wired", "techcrunch", "hill", "salon", "slate",
    "vox", "insider", "usatoday", "latimes", "msnbc", "sky",
    "itv", "dw", "france24", "xinhua", "tass", "rt", "sputnik",
    "cbsnews", "nbcnews", "abcnews", "cnn", "france", "africa",
    "vnexpress", "energynow", "billboard", "hyperallergic",
    "table", "briefings", "review", "business", "international",
    "foreign", "conservative", "oped", "op-ed", "editorial",
    "uss", "lng", "oil", "gas", "policy", "american", "latin",
    # CISA/security advisory boilerplate (CSAF JSON fields)
    "csaf", "summary", "view", "vulnerabilities", "vulnerability",
    "vendor", "equipment", "infrastructure", "sectors", "background",
    "critical", "cvss", "deployed", "severity", "versions",
    "affected", "advisory", "alert", "alerts", "bulletin",
    "product", "products", "remediation", "mitigation",
    # places / regions and geographic modifiers
    "pacific", "atlantic", "indian", "ocean", "island",
    "islands", "hawaii", "big", "little", "east", "west",
    "north", "south", "southeast", "southwest", "northeast",
    "northwest", "central", "coast", "coastal",
    "northern", "southern", "eastern", "western", "region",
    "regions", "province", "provinces", "state", "states",
    "capital", "city", "county", "country", "countries",
    "nation", "nations", "world", "gulf", "sea", "black",
    "red", "caspian", "mediterranean",
    # generic topical nouns commonly capitalized in headlines
    "crisis", "threat", "threats", "economy", "economic",
    "market", "markets", "trade", "war", "summit", "election",
    "elections", "vote", "voters", "voting", "quarter",
    "second", "third", "first", "fourth", "half", "year",
    "years", "company", "companies", "group", "board", "staff",
    "workers", "troops", "forces", "military", "army", "police",
    "fire", "rescuers", "rescue", "official", "officials",
    "ministry", "government", "parliament", "senate", "house",
})


# Roles/titles are not entities: "President Trump", "Prime
# Minister Modi" and "the ambassador" describe a person's job,
# never an event.  Without this, every "Trump" story shares
# {president, trump} with every other "Trump" story and merges
# unrelated events on the name alone.
_ROLE_ENTITY_WORDS = frozenset(
    {
        "president", "vice", "prime", "minister", "premier",
        "chancellor", "secretary", "spokesperson", "spokesman",
        "spokeswoman", "ambassador", "envoy", "governor", "senator",
        "representative", "official", "chief", "director", "boss",
        "chairman", "chairwoman", "chair", "ceo", "founder",
        "leader", "head", "king", "queen", "prince", "princess",
        "pope", "judge", "lawyer", "attorney", "prosecutor",
        "officer", "general", "admiral", "commander", "captain",
        "executive", "manager", "deputy", "former", "ex-",
        "mayor", "council", "committee", "board", "panel", "team",
    }
)


# Days and months are temporal references, not entities: two
# stories that both mention "Wednesday" share no event, and a
# day name must never serve as the identity link that merges a
# ferry disaster into an unrelated eclipse story.
_TEMPORAL_ENTITY_WORDS = frozenset(
    {
        "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday", "january", "february", "march",
        "april", "may", "june", "july", "august", "september",
        "october", "november", "december", "today", "tomorrow",
        "yesterday", "weekend", "morning", "afternoon", "evening",
        "night", "week", "month", "year", "monday's", "tuesday's",
        "wednesday's", "thursday's", "friday's", "saturday's",
        "sunday's", "today's", "tomorrow's", "yesterday's",
    }
)


# Entity aliases used by event memory.  Extends the briefing's
# table with demonyms/capitals that appear in real feeds.
#
# "american" is deliberately NOT aliased to "us": a generic
# demonym link makes "Latin American..." collide with "US..."
# stories that share no event.  Same-event stories still match
# through their real entities, locations and actions.
_ENTITY_ALIASES = dict(ENTITY_ALIASES)
_ENTITY_ALIASES.pop("american", None)
_ENTITY_ALIASES.update({
    "french": "france",
})

# Multi-word locations are matched as ATOMIC tokens so "South
# Korea" and "North Korea" never share the bare word "korea":
# the constituent words are removed from the entity set when a
# phrase is found.
_MULTI_WORD_LOCATIONS = {
    "south korea": "south_korea",
    "north korea": "north_korea",
    "new zealand": "new_zealand",
    "saudi arabia": "saudi_arabia",
    "united states": "united_states",
    "united kingdom": "united_kingdom",
    "south africa": "south_africa",
    "sri lanka": "sri_lanka",
    "costa rica": "costa_rica",
    "puerto rico": "puerto_rico",
    "hong kong": "hong_kong",
    "new york": "new_york",
    "los angeles": "los_angeles",
    "papua new guinea": "papua_new_guinea",
    "united arab emirates": "uae",
}

# Location words used for the location signal and the
# location-conflict veto.  Extends the briefing's set with
# common single-word country names so two earthquakes in two
# different countries are never merged.
_LOCATION_SET = set(LOCATION_SET) | {
    "niger", "qatar", "iraq", "lebanon", "jordan", "yemen",
    "oman", "uae", "angola", "senegal", "ghana", "mali",
    "sudan", "ethiopia", "somalia", "mozambique", "zambia",
    "zimbabwe", "cambodia", "laos", "myanmar", "nepal",
    "bangladesh", "kazakhstan", "uzbekistan", "azerbaijan",
    "georgia", "armenia", "moldova", "belarus", "iceland",
    "ireland", "croatia", "slovenia", "slovakia", "austria",
    "switzerland", "luxembourg", "malta", "cyprus", "finland",
    "estonia", "latvia", "lithuania", "uruguay", "paraguay",
    "bolivia", "ecuador", "honduras", "guatemala", "nicaragua",
    "panama", "jamaica", "czechia", "montenegro", "kosovo",
    "north korea", "south korea", "new caledonia",
}


# ---------------------------------------------------------
# Canonical state schema (stored as JSON in events.canonical_state)
#
# {
#   "identity": {                    # IMMUTABLE - matching surface
#     "title": "...",                #   first story title
#     "summary": "...",              #   first story summary
#     "entities": [...],             #   distinctive named entities
#     "locations": [...],            #   place words (context only)
#     "actions": [...],              #   canonical action words
#     "core_words": [...],           #   event-type words
#     "numbers": [...],              #   canonical numeric facts
#     "impact": [[num, unit]],       #   bound facts ("100:killed")
#     "magnitudes": [...],           #   earthquake magnitudes
#     "storm_names": [...],          #   named storms
#     "content_tokens": [...],       #   tokens of title + summary
#   },
#   "entities": [...],               # ACCUMULATED - grows with
#   "locations": [...],              # every merged story; used for
#   "actions": [...],                # material-change detection and
#   "core_words": [...],             # reporting only, NEVER for
#   "storm_names": [...],            # matching.
#   "numbers": [...],
#   "consequences": [...],
#   "impact": [...],
#   "magnitudes": [...],
#   "dev_facts": [...],              # development facts already seen
#   "titles": [...],                 # representative titles
#   "summary": "...",                # most detailed summary seen
#   "status": "...",
#   "last_development": "...",
#   "sources": [...],
#   "category": "...",
# }
# ---------------------------------------------------------


def init_events(conn):
    # The events table is created with the full website-ready
    # column set; older databases are upgraded in place by
    # storage.init_schema (idempotent, additive only).  Matching
    # logic below only ever reads the legacy columns.
    from src.storage import create_events

    create_events(conn)

    # Legacy upgrade path kept for databases that predate
    # canonical_summary / canonical_state.
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(events)"
        ).fetchall()
    }

    if "canonical_summary" not in columns:
        conn.execute(
            """
            ALTER TABLE events
            ADD COLUMN canonical_summary TEXT DEFAULT ''
            """
        )

    if "canonical_state" not in columns:
        conn.execute(
            """
            ALTER TABLE events
            ADD COLUMN canonical_state TEXT DEFAULT '{}'
            """
        )

    conn.commit()


# ---------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------

# Function words excluded from semantic-similarity tokens.
_EVENT_STOPWORDS = {
    "the", "a", "an", "and", "of", "to", "in", "on", "at", "for",
    "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "has", "have", "had", "it", "its", "this", "that",
    "these", "those", "their", "they", "them", "his", "her", "he",
    "she", "we", "our", "you", "your", "us", "i", "who", "whom",
    "which", "what", "when", "where", "why", "how", "will", "would",
    "can", "could", "should", "may", "might", "must", "shall", "do",
    "does", "did", "not", "no", "yes", "but", "or", "so", "if",
    "then", "than", "too", "very", "also", "just", "now", "new",
    "news", "says", "said", "after", "before", "during", "into",
    "about", "over", "under", "again", "more", "most", "some",
    "such", "same", "own", "amid", "via", "across", "between",
    "among", "against", "including", "according", "reportedly",
    "toward", "towards", "near", "around", "within", "without",
    "first", "last", "next", "other", "another", "former", "later",
    "today", "yesterday", "tomorrow", "officials", "official",
    "people", "residents", "several", "many", "few", "various",
}

# High-specificity event-type nouns.  Sharing one is a strong
# same-event signal, but it is NEVER sufficient alone: two
# different earthquakes are distinguished by location, magnitude
# or facts, not by the word "earthquake".
CORE_EVENT_WORDS = {
    "earthquake", "quake", "tsunami", "typhoon", "hurricane",
    "cyclone", "tornado", "storm", "wildfire", "flood", "landslide",
    "avalanche", "volcano", "eruption", "eclipse", "blackout",
    "outage", "shooting", "gunman", "blast", "explosion", "bombing",
    "airstrike", "missile", "drone", "ceasefire", "truce", "coup",
    "referendum",
}

_CORE_NORMALIZE = {
    "quake": "earthquake",
    "quakes": "earthquake",
    "tremor": "earthquake",
    "temblor": "earthquake",
    "aftershock": "earthquake",
    "flooding": "flood",
    "earthquakes": "earthquake",
    "shootings": "shooting",
    "wildfires": "wildfire",
    "blackouts": "blackout",
}

# quake -> earthquake so the same seismic event written either
# way produces the same canonical action.
_ACTION_NORMALIZE = {
    "quake": "earthquake",
}

# Extra canonical action group for event memory.  "France
# recalls ambassador" and "Niger expels French ambassador" are
# the same diplomatic rupture from opposite framings.
_ACTION_GROUPS = {
    "diplomatic-rupture": {
        "recall", "recalls", "recalled", "expel", "expels",
        "expelled", "expulsion", "withdraws", "withdrew",
    },
}

# Consequence words and their canonical form.  "dead",
# "deaths", "fatalities" and "died" all mean the same fact as
# "killed"; "injured" is a different fact and stays separate.
_CONSEQUENCE_NORMALIZE = {
    "kill": "killed",
    "kills": "killed",
    "killing": "killed",
    "killed": "killed",
    "dead": "killed",
    "deaths": "killed",
    "fatalities": "killed",
    "died": "killed",
    "dies": "killed",
    "injure": "injured",
    "injures": "injured",
    "injuring": "injured",
    "injured": "injured",
    "hurt": "injured",
    "wounds": "injured",
    "wounding": "injured",
    "wounded": "injured",
    "displace": "displaced",
    "displaces": "displaced",
    "displacing": "displaced",
    "displaced": "displaced",
    "evacuate": "evacuated",
    "evacuates": "evacuated",
    "evacuating": "evacuated",
    "evacuated": "evacuated",
    "arrest": "arrested",
    "arrests": "arrested",
    "arresting": "arrested",
    "arrested": "arrested",
    "detain": "detained",
    "detains": "detained",
    "detaining": "detained",
    "detained": "detained",
    "hospitalize": "hospitalized",
    "hospitalizes": "hospitalized",
    "hospitalizing": "hospitalized",
    "hospitalized": "hospitalized",
    "hospitalise": "hospitalised",
    "hospitalised": "hospitalised",
    "death": "killed",
}

_CONSEQUENCE_WORDS = {
    "kill", "kills", "killing", "killed", "dead", "death",
    "deaths", "fatalities", "died", "dies", "injure",
    "injures", "injuring", "injured", "hurt", "wounds",
    "wounding", "wounded", "displace", "displaces",
    "displacing", "displaced", "evacuate", "evacuates",
    "evacuating", "evacuated", "missing", "arrest", "arrests",
    "arresting", "arrested", "detain", "detains", "detaining",
    "detained", "hospitalize", "hospitalizes", "hospitalizing",
    "hospitalized", "hospitalise", "hospitalised",
}

# Event-type synonyms applied to CONTENT tokens so reworded
# headlines ("Powerful quake leaves 100 dead" after "Earthquake
# kills 100 people") overlap semantically.  Only event-type
# words are normalized - never generic template words (kill,
# dead, 100, people, country names), so "Earthquake kills 100
# in Chile" and "Flood kills 100 in Chile" do NOT become the
# same story.
_SEMANTIC_SYNONYMS = {
    "quake": "earthquake",
    "quakes": "earthquake",
    "tremor": "earthquake",
    "temblor": "earthquake",
    "aftershock": "earthquake",
}


def _content_tokens(text):
    """Lowercased, lightly stemmed content tokens for semantic
    similarity.  Years (1900-2100) and function words are dropped;
    event-type synonyms are normalized; ASCII and Unicode
    possessives are stripped so "Putin's" and "Putin" are the
    same token."""
    out = set()
    for w in re.findall(
        r"[a-z0-9][a-z0-9'-]*",
        (text or "").lower().replace("’", "'"),
    ):
        if w in _EVENT_STOPWORDS:
            continue
        if w.isdigit():
            try:
                value = float(w)
            except ValueError:
                value = 0.0
            if 1900 <= value <= 2100:
                continue
            out.add(w)
            continue
        if len(w) >= 3:
            w = w[:-2] if w.endswith("'s") else w
            out.add(_SEMANTIC_SYNONYMS.get(w, _stem_lite(w)))
    return out


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _consequences(text):
    """Normalized consequence words present in the text."""
    words = set(
        re.findall(
            r"[a-z0-9]+",
            (text or "").lower(),
        )
    )
    out = set()
    for w in words:
        if w in _CONSEQUENCE_WORDS:
            out.add(_CONSEQUENCE_NORMALIZE.get(w, w))
    return out


# number + consequence/unit phrases, e.g. "100 dead",
# "20,000 evacuated", "magnitude 6.4".
_IMPACT_UNIT_RE = re.compile(
    r"\b(\d[\d,.]*)\s*"
    r"(killed|dead|deaths|fatalities|injured|displaced|evacuated|"
    r"missing|arrested|detained|hospitalized|hospitalised|people|"
    r"residents|homes|families|troops|soldiers|hostages|workers|"
    r"officers|magnitude|percent|%|km|miles|billion|million|"
    r"thousand|degrees|toll)\b",
    re.IGNORECASE,
)


def _impact_pairs(text):
    """Canonical "number:unit" impact facts in the text, e.g.
    "100:killed", "20000:evacuated", "64:magnitude".  Stored
    as plain strings so the state survives JSON round-trips."""
    pairs = set()
    for m in _IMPACT_UNIT_RE.finditer(text or ""):
        number = re.sub(r"[^0-9.]", "", m.group(1))
        unit = m.group(2).lower()
        if not number:
            continue
        if unit in _CONSEQUENCE_WORDS:
            unit = _CONSEQUENCE_NORMALIZE.get(unit, unit)
        pairs.add(number + ":" + unit)
    return pairs


# Narrow official-action development markers, labelled so that a
# repeated development fact (the same death-toll figure, a second
# "official says") is not re-published as a new UPDATE.
DEVELOPMENT_PATTERNS = [
    ("emergency", r"\bstate\s+of\s+emergency\b"),
    ("emergency", r"\bnational\s+emergency\b"),
    ("emergency", r"\bemergency\s+(?:declared|declaration)\b"),
    ("emergency",
     r"\bdeclares?\s+(?:a\s+|the\s+|national\s+|state\s+of\s+)?"
     r"emergency\b"),
    ("death_toll", r"\bdeath\s+toll\b"),
    ("death_toll", r"\btoll\s+(?:rises?|climbs?|grows?|increases?|"
                    r"now)\b"),
    ("death_toll", r"\brises?\s+to\s+\d"),
    ("evacuation", r"\bevacuat\w*\b"),
    ("arrest", r"\barrest\w*\b"),
    ("rescue", r"\brescu\w*\b"),
    ("rescue", r"\bsurvivor\w*\b"),
    ("suspect_identified", r"\bsuspect\w*\s+identified\b"),
    ("suspect_identified",
     r"\bidentified\s+(?:the\s+)?(?:suspect|attacker|gunman|"
     r"driver)\b"),
    ("ceasefire", r"\bcease-?fire\b"),
    ("ceasefire", r"\btruce\b"),
    ("peace_deal", r"\bpeace\s+(?:deal|agreement|accord|talks|"
                   r"plan|treaty)\b"),
    ("deal_signed",
     r"\bsign(?:ed|s)?\s+(?:a\s+|the\s+)?(?:deal|agreement|accord|"
     r"treaty|pact|contract)\b"),
    ("deal_signed",
     r"\b(?:deal|agreement|accord|treaty|pact|contract)\s+"
     r"(?:signed|reached|struck)\b"),
    ("result_confirmed", r"\bresult\w*\s+(?:confirmed|announced|"
                         r"declared)\b"),
    ("withdrawal", r"\bwithdraw\w*\b"),
    ("second_event",
     r"\bsecond\s+(?:earthquake|quake|blast|explosion|attack|"
     r"wave)\b"),
    ("troop_move",
     r"\b(?:troops?|forces?)\s+(?:enter|leave|withdraw|pull\s+out)\b"),
    ("operation_launch",
     r"\b(?:launch|launches|launched)\s+(?:a\s+|an\s+|the\s+)?"
     r"(?:strike|attack|operation|probe|investigation)\b"),
]

_DEVELOPMENT_RE = re.compile(
    "|".join(
        "(?:" + pattern + ")"
        for _, pattern in DEVELOPMENT_PATTERNS
    ),
    re.IGNORECASE,
)


def _dev_facts(text):
    """Labels of the development markers present in the text
    (e.g. {"death_toll", "rescue"})."""
    out = set()
    for label, pattern in DEVELOPMENT_PATTERNS:
        if re.search(pattern, text or "", re.IGNORECASE):
            out.add(label)
    return out


def _has_development_marker(text):
    return bool(_DEVELOPMENT_RE.search(text or ""))


# Reaction/statement markers: a comment, tribute or statement
# ABOUT a matched event.  Such stories carry no material fact of
# their own and are suppressed unless something is new.
REACTION_PATTERNS = [
    r"\b(?:calls?|called|describes?|described|terms?|praises?|"
    r"praised|condemns?|condemned|mourns?|mourned|says?|say|said)\b",
    r"\b(?:statement|remarks?|comments?|tribute|condolences?)\b",
]

_REACTION_RE = re.compile(
    "|".join(
        "(?:" + pattern + ")"
        for pattern in REACTION_PATTERNS
    ),
    re.IGNORECASE,
)


def _has_reaction_marker(text):
    return bool(_REACTION_RE.search(text or ""))


# Named storms: "Tropical Storm Lala" and "Tropical Storm
# Hernan" are different events; two named storms that share no
# name are never merged even when every topic word overlaps.
_NAMED_STORM_RE = re.compile(
    r"\b(?:(?:tropical|super|major|powerful|severe)\s+)?"
    r"(?:storm|typhoon|hurricane|cyclone)\s+"
    r"([A-Z][A-Za-z'-]+)\b",
    re.IGNORECASE,
)


def _storm_names(text):
    return {
        m.group(1).lower()
        for m in _NAMED_STORM_RE.finditer(text or "")
    }


# Earthquake magnitudes: "magnitude 6.8", "a 6.8-magnitude
# quake", "a 5.9 tremor" and "quake of 6.4".  Two quakes with
# materially different magnitudes are different events even in
# the same region.  The number must be bound to a magnitude
# context: a bare toll or count ("100 dead", "20,000
# evacuated") is never a magnitude.
_MAGNITUDE_RE = re.compile(
    r"\b(?:"
    r"magnitude\s+(\d+(?:\.\d+)?)"
    r"|"
    r"(\d+(?:\.\d+)?)\s*-\s*magnitude"
    r"|"
    r"(\d+(?:\.\d+)?)\s+(?:earthquake|quake|tremor|temblor|"
    r"aftershock)"
    r"|"
    r"(?:earthquake|quake|tremor|temblor|aftershock)\s+of\s+"
    r"(\d+(?:\.\d+)?)"
    r")",
    re.IGNORECASE,
)


def _magnitudes_of(text):
    """Earthquake magnitudes mentioned in the text, as floats."""
    out = set()
    for m in _MAGNITUDE_RE.finditer(text or ""):
        for group in m.groups():
            if group is None:
                continue
            try:
                out.add(float(group))
            except ValueError:
                continue
    return out


def _locations_of(text):
    """Location words mentioned anywhere in the text (lowercase
    scan), so "a quake in Niger" and "Niger expels..." both
    count even when the place is not capitalized.  Multi-word
    locations are returned as their atomic canonical token."""
    lowered = (text or "").lower()
    words = set(
        re.findall(
            r"[a-z0-9]+",
            lowered,
        )
    )
    found = words & _LOCATION_SET
    for phrase, canonical in _MULTI_WORD_LOCATIONS.items():
        if phrase in lowered:
            found.add(canonical)
            found -= set(phrase.split())
    return found


def _numbers_of_event(text):
    """Canonical numbers, excluding digits embedded in a longer
    alphanumeric token ("LOS40", "A380", "5G") and bare years
    ("2026"): those are brand/model names and calendar years,
    never event facts, and their digits must not collide with a
    real count ("40 nations") or make every story published in
    the same year look related."""
    out = set()
    for m in NUMBER_RE.finditer(text or ""):
        canonical = re.sub(r"[^0-9.]", "", m.group(0))
        if not canonical:
            continue
        try:
            value = float(canonical)
        except ValueError:
            continue
        if 1900 <= value <= 2100:
            continue
        before = (text or "")[max(0, m.start() - 1):m.start()]
        after = (text or "")[m.end():m.end() + 1]
        if re.search(r"[a-zA-Z]", before + after):
            continue
        out.add(canonical)
    return out


def _signals_text(title, summary=""):
    """Structured signals extracted from a story's text."""
    # Normalize Unicode apostrophes so "Putin's" and "Putin’s"
    # produce the same entity token.
    text = ((title or "") + " " + (summary or "")).replace("’", "'")
    entities = {
        _ENTITY_ALIASES.get(e, e)
        for e in _entity_words(text)
        if len(e) >= 2
        and e not in _TEMPORAL_ENTITY_WORDS
        and e not in _ROLE_ENTITY_WORDS
        and e not in _BOILERPLATE_ENTITY_WORDS
    }

    # Multi-word locations become atomic entity tokens; their
    # constituent words are dropped so "South Korea" and "North
    # Korea" share nothing.
    lowered = text.lower()
    for phrase, canonical in _MULTI_WORD_LOCATIONS.items():
        if phrase in lowered:
            entities.add(canonical)
            entities -= set(phrase.split())

    locations = _locations_of(text)
    # City names are aliased to their country ("Paris" ->
    # "france", "Tehran" -> "iran") and therefore appear in the
    # entity set as place words.  Places are CONTEXT, never
    # distinctive entities: two "Paris" stories share the place
    # "france", and that must never act as a named-entity anchor
    # ("arrest in Paris" + "arrest in Paris" are two incidents).
    locations |= entities & _LOCATION_SET

    actions = {
        _ACTION_NORMALIZE.get(a, a)
        for a in _actions(text)
    }
    # The broad "strike" synset (hit/slams/batters/pummel/landfall)
    # links unrelated senses (a stock hit vs shore battering vs a
    # drill slammed); every variant is dropped for event memory.
    # The entities and other actions still carry genuine matches
    # ("Typhoon Yagi" merges on Yagi + typhoon).
    actions -= {"strike", "hit", "hits", "slam", "slams",
                "pummel", "pummels", "batters", "batter",
                "landfall"}

    raw_words = set(
        re.findall(
            r"[a-z][a-z0-9]*",
            lowered,
        )
    )
    for canonical, variants in _ACTION_GROUPS.items():
        if variants & raw_words:
            actions.add(canonical)
    core_words = set()
    for w in entities | raw_words:
        cw = _CORE_NORMALIZE.get(w, w)
        if cw in CORE_EVENT_WORDS:
            core_words.add(cw)
    return {
        "entities": entities,
        "locations": locations,
        "actions": actions,
        "storm_names": _storm_names(text),
        "core_words": core_words,
        "numbers": _numbers_of_event(text),
        "consequences": _consequences(text),
        "impact": _impact_pairs(text),
        "magnitudes": _magnitudes_of(text),
        "dev": _has_development_marker(text),
        "dev_facts": _dev_facts(text),
        "reaction": _has_reaction_marker(text),
        "content_tokens": _content_tokens(text),
        "text": text,
    }


def _signals(item):
    return _signals_text(
        item.get("title", ""),
        item.get("summary", ""),
    )


def story_entities(title, summary=""):
    """Distinctive named entities of a story (people, companies,
    institutions, named storms, ships, ...) as a sorted list.

    Pure observability helper for the website-ready data model; it
    reuses the SAME signal extraction the matching rules use, so it
    is guaranteed to never disagree with event-memory entity
    handling.  It never touches matching state.
    """
    return sorted(
        _signals_text(title, summary)["entities"]
    )


# ---------------------------------------------------------
# Canonical state construction / merge
# ---------------------------------------------------------

def _status_of(signals):
    if signals["dev"]:
        return "developing"
    if signals["consequences"]:
        return "ongoing"
    return "reported"


def _identity_from_signals(item, signals):
    """The IMMUTABLE identity core, built from the first story
    only.  Nothing that merges later ever touches this dict."""
    title = item.get("title") or ""
    summary = item.get("summary") or ""
    return {
        "title": title,
        "summary": summary,
        "entities": sorted(signals["entities"]),
        "locations": sorted(signals["locations"]),
        "actions": sorted(signals["actions"]),
        "core_words": sorted(signals["core_words"]),
        "storm_names": sorted(signals["storm_names"]),
        "numbers": sorted(signals["numbers"]),
        "impact": sorted(signals["impact"]),
        "magnitudes": sorted(signals["magnitudes"]),
        "title_tokens": sorted(_content_tokens(title)),
        "content_tokens": sorted(
            _content_tokens(title + " " + summary)
        ),
    }


def _accumulated_from_signals(item, signals):
    """The initial ACCUMULATED half.  This grows as stories
    merge; it is used for material-change detection and
    reporting, never for matching."""
    title = item.get("title") or ""
    summary = item.get("summary") or ""
    return {
        "entities": sorted(signals["entities"]),
        "locations": sorted(signals["locations"]),
        "actions": sorted(signals["actions"]),
        "core_words": sorted(signals["core_words"]),
        "storm_names": sorted(signals["storm_names"]),
        "numbers": sorted(signals["numbers"]),
        "consequences": sorted(signals["consequences"]),
        "impact": sorted(signals["impact"]),
        "magnitudes": sorted(signals["magnitudes"]),
        "dev_facts": sorted(signals["dev_facts"]),
        "titles": [title],
        "summary": summary,
        "status": _status_of(signals),
        "last_development": title,
        "sources": (
            [item.get("source")]
            if item.get("source")
            else []
        ),
        "category": item.get("category", "world"),
    }


def _state_from_signals(item, signals):
    return {
        "identity": _identity_from_signals(item, signals),
        **_accumulated_from_signals(item, signals),
    }


def _state_from_text(title, summary=""):
    """Canonical state built from raw strings (legacy rows that
    predate canonical_state, or rows whose identity must be
    rebuilt)."""
    signals = _signals_text(title, summary)
    fake_item = {
        "title": title,
        "summary": summary,
        "category": "world",
        "source": None,
    }
    return _state_from_signals(fake_item, signals)


def _parse_state(row, canonical_title, canonical_summary):
    raw = row[7] if len(row) > 7 else None
    state = None
    if raw:
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            state = None
    if not isinstance(state, dict):
        state = _state_from_text(
            canonical_title,
            canonical_summary,
        )
    # States written before the identity split (or rows whose
    # stored identity is missing/corrupt) get a fresh identity
    # anchored to the canonical title/summary.
    if not isinstance(state.get("identity"), dict):
        rebuilt = _state_from_text(
            state.get("identity", {}).get("title")
            if isinstance(state.get("identity"), dict)
            else canonical_title,
            canonical_summary or state.get("summary", ""),
        )
        state["identity"] = rebuilt["identity"]
        for key in (
            "entities", "locations", "actions", "core_words",
            "storm_names", "numbers", "consequences", "impact",
            "magnitudes", "dev_facts", "titles", "summary",
            "status", "last_development", "sources", "category",
        ):
            if key not in state:
                state[key] = rebuilt.get(key)
    return state


def _merge_sorted(existing, new_items):
    return sorted(set(existing or []) | set(new_items or []))


def _merge_state(item, signals, state):
    """Merge a matched story's signals into the ACCUMULATED half
    of the state.  The identity half is deliberately untouched:
    a merged story can never widen the event's matching surface."""
    title = item.get("title") or ""
    summary = item.get("summary") or ""
    state["entities"] = _merge_sorted(
        state.get("entities"), signals["entities"]
    )
    state["locations"] = _merge_sorted(
        state.get("locations"), signals["locations"]
    )
    state["actions"] = _merge_sorted(
        state.get("actions"), signals["actions"]
    )
    state["core_words"] = _merge_sorted(
        state.get("core_words"), signals["core_words"]
    )
    state["storm_names"] = _merge_sorted(
        state.get("storm_names"), signals["storm_names"]
    )
    state["numbers"] = _merge_sorted(
        state.get("numbers"), signals["numbers"]
    )
    state["consequences"] = _merge_sorted(
        state.get("consequences"), signals["consequences"]
    )
    state["impact"] = _merge_sorted(
        state.get("impact"), signals["impact"]
    )
    state["magnitudes"] = _merge_sorted(
        state.get("magnitudes"), signals["magnitudes"]
    )
    state["dev_facts"] = _merge_sorted(
        state.get("dev_facts"), signals["dev_facts"]
    )
    titles = [t for t in [title] + (state.get("titles") or []) if t]
    state["titles"] = titles[:8]
    if summary and len(summary) > len(state.get("summary") or ""):
        state["summary"] = summary
    sources = list(state.get("sources") or [])
    source = item.get("source")
    if source and source not in sources:
        sources.append(source)
    state["sources"] = sources
    if item.get("category"):
        state["category"] = item.get("category")
    return state


# ---------------------------------------------------------
# Matching - against the immutable identity ONLY
# ---------------------------------------------------------

# Generic action words that describe HOW a story is reported, not
# WHICH event it is ("visits", "hits", "meets", "says").  Two
# stories that share only a generic action plus a place are
# different events ("Putin visits Tehran" vs "Blinken visits
# Tehran").  These are excluded from topical/action signals.
_GENERIC_ACTIONS = frozenset({
    "strike", "visit", "arriv", "hold", "meet", "talks", "talk",
    "discuss", "say", "said", "announce", "report", "confirm",
    "plan", "pledge", "promise", "call", "urge", "pass", "extend",
    "approv", "deploy", "exercise", "flee", "cross", "kill",
    "evacuat", "arrest",
})

# Narrow, event-defining actions.  Sharing one of these with a
# place and lexical overlap is a strong same-event signal
# ("France recalls ambassador" / "Niger expels French
# ambassador" on the diplomatic-rupture group).  Signing a deal
# is only narrow when combined with an extra anchor, because
# "France signs climate deal" and "France signs trade deal" are
# different events that share the action and the country.
_NARROW_ACTIONS = frozenset({
    "diplomatic-rupture", "seize", "seizure", "sign", "pact",
    "ceasefire", "truce", "convict", "verdict", "indict", "guilty",
    "hack", "breach", "sanction", "summit",
})


def _match_score(signals, identity):
    """Multi-signal similarity between a story and an event's
    IMMUTABLE identity.  Returns a breakdown dict; the score is
    used for ranking and for the reaction floor, while the
    accept/decline decision is made by _match_rule() alone."""
    state_entities = set(identity.get("entities") or [])
    state_locations = set(identity.get("locations") or [])
    state_actions = set(identity.get("actions") or [])
    state_core = set(identity.get("core_words") or [])
    state_numbers = set(identity.get("numbers") or [])
    state_impact = set(identity.get("impact") or [])

    shared_entities = signals["entities"] & state_entities
    shared_locations = signals["locations"] & state_locations
    shared_actions = signals["actions"] & state_actions
    shared_numbers = signals["numbers"] & state_numbers
    shared_core = signals["core_words"] & state_core
    shared_impact = signals["impact"] & state_impact

    # Semantic overlap is computed against BOTH the identity's
    # title tokens (short, so reworded headlines overlap well) and
    # its full title+summary tokens (richer, for longer stories).
    # The higher of the two is used: a long summary must not
    # dilute a clear reworded headline below the match threshold.
    semantic = max(
        _jaccard(
            signals["content_tokens"],
            set(identity.get("title_tokens") or []),
        ),
        _jaccard(
            signals["content_tokens"],
            set(identity.get("content_tokens") or []),
        ),
    )

    score = 0.0
    score += 3.0 * min(2, len(shared_entities))
    score += 2.5 if shared_locations else 0.0
    score += 2.0 * min(2, len(shared_actions))
    score += 1.5 * min(2, len(shared_numbers))
    score += 3.0 * semantic

    return {
        "score": score,
        "semantic": semantic,
        "shared_entities": shared_entities,
        "shared_locations": shared_locations,
        "shared_actions": shared_actions,
        "shared_numbers": shared_numbers,
        "shared_core": shared_core,
        "shared_impact": shared_impact,
        # Distinctive entities = shared entities that are NOT
        # also places.  A shared country/city (which appears in
        # both the entity and location sets) is context, never
        # a named-entity identity signal.
        "distinctive": shared_entities - shared_locations,
        "identity_locations": state_locations,
        "narrow_actions": (
            shared_actions & _NARROW_ACTIONS
        ),
        "identity": bool(
            shared_entities
            or shared_locations
            or shared_actions
            or shared_core
            or shared_impact
        ),
    }


def _match_rule(signals, m):
    """Whether the signals match the event's identity, and WHY.

    Returns a short reason string when the story belongs to the
    event, or None.  Every path requires a COMBINATION of
    signals; no single shared attribute (person, place, topic,
    number, action, development word) is ever enough.

    Development and reaction markers are evaluated LAST: they can
    only attach a story to an event that has already been
    identified by real identity signals.
    """
    distinctive = m["distinctive"]
    # "Strong" entities exclude publisher suffixes ("- ABC News -"),
    # geographic/topical generics (Pacific, islands, crisis, market)
    # and other weak capitalized words: those are context, never
    # event identity.
    strong = distinctive - _WEAK_ENTITY_WORDS
    topical_act = m["shared_actions"] - _GENERIC_ACTIONS
    topical = bool(
        m["shared_core"] or topical_act or m["shared_impact"]
    )

    # R1: same bound fact ("100 killed") + same event type or a
    # distinctive (non-place) entity.  A shared place alone never
    # counts: "Flood kills 100 in Paris" and "Quake kills 100 in
    # Paris" share the fact and the city but are different events.
    if m["shared_impact"] and (m["shared_core"] or distinctive):
        return "impact+type"

    # R2: same event type + same place + a third signal.
    if (
        m["shared_core"]
        and m["shared_locations"]
        and (
            topical_act
            or m["shared_impact"]
            or m["semantic"] >= 0.25
        )
    ):
        return "type+place"

    # R2b: reworded phrasing that keeps the event-type action
    # vocabulary, with real lexical overlap ("Coastal city quake:
    # 100 confirmed dead" after "Earthquake kills 100 people").
    # At least one shared action must be an event-type word, so
    # the sign/pact template cannot merge "France signs climate
    # accord" with "France signs trade deal".
    if (
        len(m["shared_actions"]) >= 2
        and (m["shared_actions"] & CORE_EVENT_WORDS)
        and m["semantic"] >= 0.15
    ):
        return "action-overlap"

    # R3: shared STRONG named entities (non-place, non-generic).  A
    # person alone never merges events, and there is NO location
    # escape: {donald, trump} is ONE person, and "Donald Trump
    # imposes drone tariffs" must never merge into "Karoline
    # Leavitt steps down" merely because both happened in the US.
    # The story must actually be about the same subject (real
    # lexical overlap).  Publisher suffixes and topic/place words
    # (hawaii, pacific, tropical, east, middle, al jazeera) never
    # count as entities.
    if len(strong) >= 2 and m["semantic"] >= 0.15:
        return "entities"
    if len(distinctive) >= 2 and topical and (
        len(strong) >= 1
        # The story names the event's ENTIRE place set (e.g. the
        # Black-Sea grain story that names both Russia and
        # Ukraine).  A partial overlap ("Middle East" + Iran in a
        # story about a South Korean airport) is not enough.
        or m["shared_locations"] >= m["identity_locations"]
        or m["semantic"] >= 0.15
    ):
        return "entities+topical"
    if (
        len(distinctive) == 1
        and m["shared_locations"]
        and topical
    ):
        return "entity+place+topical"

    # R4: a NARROW event-defining action + shared place + overlap.
    # The diplomatic-rupture group alone is distinctive enough;
    # other narrow actions (sign, pact, ceasefire) need an extra
    # anchor so "France signs climate deal" and "France signs
    # trade deal" stay separate.
    if m["narrow_actions"] and m["shared_locations"] and m["semantic"] >= 0.10:
        if "diplomatic-rupture" in m["narrow_actions"]:
            return "action+place"
        if (
            m["shared_core"]
            or m["distinctive"]
            or m["shared_impact"]
            or m["semantic"] >= 0.45
        ):
            return "action+place"

    # R6: high-confidence paraphrase anchored by the same
    # event-type word ("Total solar eclipse sweeps across Spain"
    # after "Europe braces for the solar eclipse").  Template
    # collisions ("Earthquake kills 100 in Chile" vs "Flood kills
    # 100 in Chile") share no core word and stay separate.
    if m["shared_core"] and m["semantic"] >= 0.35:
        return "semantic"

    # R6b: very high lexical overlap (>= 0.40) about the same
    # place with at least one shared entity or action: "Nigeria to
    # miss Women's World Cup after South Africa and Ghana win
    # play-offs" and "Ghana, South Africa end WAFCON with wins"
    # report the same result.  The floor is high enough that
    # "Voters in Latin America..." + "Socialism on the rise..."
    # (0.385) and the US/China template pairs stay separate.
    if (
        m["semantic"] >= 0.40
        and m["shared_locations"]
        and (m["shared_entities"] or topical_act)
    ):
        return "semantic+place"

    # A development marker ("death toll rises", "rescuers",
    # "emergency declared") may attach the story ONLY when an
    # identity anchor already exists: the event-type word or a
    # bound fact, or a distinctive entity together with a shared
    # place.  The marker itself never creates the link, and a
    # lone shared name token ("Donald" in "Donald Tusk" vs
    # "Donald Trump") plus a development word is never enough.
    if signals["dev"] and (
        (m["shared_core"] or m["shared_impact"])
        or (len(strong) >= 2 and m["shared_locations"])
        or (
            len(strong) >= 1
            and m["shared_locations"]
            and m["semantic"] >= 0.10
        )
    ):
        return "dev+link"

    # A reaction/statement ("official calls earthquake a
    # tragedy") directly referencing the event's core subject
    # attaches and is suppressed unless it adds a material fact.
    # The score floor requires at least a shared action word,
    # entity or place in addition to the event-type reference.
    if signals["reaction"] and m["shared_core"] and m["score"] >= 2.0:
        return "reaction"

    return None


def _location_conflict(signals, identity):
    """True when the story and the event's identity name places
    but share none: a Canada wildfire is never a Spokane
    wildfire, however many other signals overlap.  Also true
    when both sides name multiple places and each names a place
    the other does not: "US signs a deal with Canada" and "US
    signs a pact with Japan" share only the US but are different
    events."""
    story_locations = signals["locations"]
    state_locations = set(identity.get("locations") or [])
    if not story_locations or not state_locations:
        return False
    shared = story_locations & state_locations
    if not shared:
        return True
    if (
        len(story_locations) >= 2
        and len(state_locations) >= 2
        and (story_locations - shared)
        and (state_locations - shared)
    ):
        return True
    return False


def _storm_conflict(signals, identity):
    """True when both the story and the event's identity name
    storms but share no storm name: Tropical Storm Lala and
    Tropical Storm Hernan are different events."""
    story_names = signals["storm_names"]
    state_names = set(identity.get("storm_names") or [])
    if not story_names or not state_names:
        return False
    return not (story_names & state_names)


def _magnitude_conflict(signals, identity):
    """True when both the story and the event's identity report
    earthquake magnitudes but share no magnitude: a
    magnitude-6.8 offshore quake and a magnitude-5.9 tremor in
    the same country are different events, however many signals
    overlap."""
    story_mags = signals["magnitudes"]
    state_mags = set(identity.get("magnitudes") or [])
    if not story_mags or not state_mags:
        return False
    return not (story_mags & state_mags)


# ---------------------------------------------------------
# Material-change detection (UPDATE vs DUPLICATE)
# ---------------------------------------------------------

# Units that make a small bare number a real fact (magnitude 6.4,
# 45 percent, 12 km).  Years (1900-2100) are never facts.
_SMALL_NUMBER_UNIT_RE = re.compile(
    r"(?:(?:more than|at least|nearly|about|around|over|up to|"
    r"almost|roughly)\s+)?"
    r"(\d[\d,.]*)\s*"
    r"(magnitude|percent|%|km|miles|billion|million|thousand|"
    r"degrees|celsius|fahrenheit|killed|dead|deaths|fatalities|"
    r"injured|evacuated|displaced|hostages|troops|people|"
    r"residents|homes|families|workers|officers|toll)\b",
    re.IGNORECASE,
)


def _material_number(number, text):
    """Whether a number absent from the event state is a material
    fact: at least 100, or a small number attached to a real unit.
    Years are never material."""
    try:
        value = float(number)
    except ValueError:
        return False
    if 1900 <= value <= 2100:
        return False
    if value >= 100:
        return True
    for m in _SMALL_NUMBER_UNIT_RE.finditer(text or ""):
        if re.sub(r"[^0-9.]", "", m.group(1)) == number:
            return True
    return False


def _material_change(signals, state):
    """True when a matched story adds a material fact beyond the
    event's ACCUMULATED state.

    A development marker is material only when it is a NEW
    development fact: "death toll rises to 180" updates once, and
    a second story repeating the same toll (or a second
    "official calls it a tragedy") stays suppressed."""
    state_dev = set(state.get("dev_facts") or [])
    if signals["dev_facts"] - state_dev:
        return True

    state_numbers = set(state.get("numbers") or [])
    for number in signals["numbers"] - state_numbers:
        if _material_number(number, signals["text"]):
            return True

    state_consequences = set(state.get("consequences") or [])
    if signals["consequences"] - state_consequences:
        return True

    # New canonical actions are new developments (a new strike, a
    # new arrest, a newly signed deal...).  Consequence words are
    # compared above and excluded here: "killed" / "dead" appear
    # in the action lexicon too and must never count twice.
    state_actions = set(state.get("actions") or [])
    new_actions = (
        signals["actions"] - state_actions
    ) - _CONSEQUENCE_WORDS
    if new_actions:
        return True

    return False


# ---------------------------------------------------------
# DB helpers
# ---------------------------------------------------------

def _new_id(title):
    return hashlib.sha256(
        title.strip().lower().encode()
    ).hexdigest()[:24]


def _same_event_source(conn, event_id, source):
    """Whether this source has already produced a story belonging
    to this event."""
    if not source:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM stories
        WHERE event_id=? AND source=?
        LIMIT 1
        """,
        (event_id, source),
    ).fetchone()
    return row is not None


def _is_major(item):
    return int(
        item.get(
            "priority_score",
            item.get("score", 0),
        )
        >= 85
    )


def _insert_event(conn, event_id, item, state, now):
    from src.storage import event_meta

    meta = event_meta(item, state, now.isoformat())

    conn.execute(
        """
        INSERT OR REPLACE INTO events(
            event_id,
            canonical_title,
            category,
            first_seen,
            last_seen,
            major,
            queued_count,
            canonical_summary,
            canonical_state,
            sector,
            subsector,
            region,
            subregion,
            country,
            entities,
            event_time,
            last_development,
            related_sources,
            verification
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            item.get("title", ""),
            item.get("category", "world"),
            now.isoformat(),
            now.isoformat(),
            _is_major(item),
            0,
            item.get("summary", ""),
            json.dumps(state, ensure_ascii=False),
            meta["sector"],
            meta["subsector"],
            meta["region"],
            meta["subregion"],
            meta["country"],
            meta["entities"],
            meta["event_time"],
            # A NEW event's last_development starts at the event
            # time (the first meaningful development).
            meta["event_time"] or now.isoformat(),
            meta["related_sources"],
            meta["verification"],
        ),
    )


def _write_event_meta(conn, event_id, item, state, now,
                      advance_development, canonical_title,
                      canonical_summary):
    """Refresh the website-ready observability columns of an event
    row.  Matching/identity columns are never touched here.

    advance_development is True only for a genuine UPDATE: it moves
    last_development to the incoming story's effective time.
    Duplicate articles and touch refreshes keep the stored value.

    canonical_title / canonical_summary anchor the event's sector,
    region and country: a merged article about a sub-aspect must
    never retag the event.
    """
    from src.storage import event_meta

    meta = event_meta(
        item,
        state,
        now.isoformat(),
        canonical_title=canonical_title,
        canonical_summary=canonical_summary,
        advance_development=advance_development,
    )
    conn.execute(
        """
        UPDATE events
        SET
            sector=?,
            subsector=?,
            region=?,
            subregion=?,
            country=?,
            entities=?,
            last_development=COALESCE(?, last_development),
            related_sources=?,
            verification=?
        WHERE event_id=?
        """,
        (
            meta["sector"],
            meta["subsector"],
            meta["region"],
            meta["subregion"],
            meta["country"],
            meta["entities"],
            meta["last_development"],
            meta["related_sources"],
            meta["verification"],
            event_id,
        ),
    )


def _touch_event(conn, event_id, item, now):
    """Refresh last_seen / major without changing the story."""
    conn.execute(
        """
        UPDATE events
        SET last_seen=?,
            major=MAX(major,?)
        WHERE event_id=?
        """,
        (now.isoformat(), _is_major(item), event_id),
    )


def _update_event(conn, event_id, item, state, now):
    """Apply a matched UPDATE: refresh last_seen and persist the
    merged ACCUMULATED state.  canonical_title / canonical_summary
    stay anchored to the event's ORIGINAL identity story.

    A genuine UPDATE also advances last_development to the incoming
    story's effective time (the latest meaningful development).
    """
    canonical_title = conn.execute(
        "SELECT canonical_title FROM events WHERE event_id=?",
        (event_id,),
    ).fetchone()[0]
    canonical_summary = conn.execute(
        "SELECT canonical_summary FROM events WHERE event_id=?",
        (event_id,),
    ).fetchone()[0]

    conn.execute(
        """
        UPDATE events
        SET
            last_seen=?,
            major=MAX(major,?),canonical_state=?
        WHERE event_id=?
        """,
        (
            now.isoformat(),
            _is_major(item),
            json.dumps(state, ensure_ascii=False),
            event_id,
        ),
    )
    _write_event_meta(
        conn,
        event_id,
        item,
        state,
        now,
        advance_development=True,
        canonical_title=canonical_title,
        canonical_summary=canonical_summary,
    )


def _persist_state(conn, event_id, state, item, now):
    """Refresh last_seen and persist the (merged) accumulated
    state without treating the story as an update."""
    canonical_title = conn.execute(
        "SELECT canonical_title FROM events WHERE event_id=?",
        (event_id,),
    ).fetchone()[0]
    canonical_summary = conn.execute(
        "SELECT canonical_summary FROM events WHERE event_id=?",
        (event_id,),
    ).fetchone()[0]

    conn.execute(
        """
        UPDATE events
        SET
            last_seen=?,
            major=MAX(major,?),canonical_state=?
        WHERE event_id=?
        """,
        (
            now.isoformat(),
            _is_major(item),
            json.dumps(state, ensure_ascii=False),
            event_id,
        ),
    )
    # Duplicate / same-source refresh: metadata is folded in but
    # last_development stays anchored - a repeat article never
    # advances the event timeline.
    _write_event_meta(
        conn,
        event_id,
        item,
        state,
        now,
        advance_development=False,
        canonical_title=canonical_title,
        canonical_summary=canonical_summary,
    )


# ---------------------------------------------------------
# Public decision
# ---------------------------------------------------------

def decide(conn, item, memory_hours=48, major_memory_hours=168):
    """Classify one incoming story: NEW / DUPLICATE / UPDATE."""
    now = datetime.now(timezone.utc)

    rows = conn.execute(
        """
        SELECT
            event_id,
            canonical_title,
            first_seen,
            last_seen,
            major,
            queued_count,
            canonical_summary,
            canonical_state
        FROM events
        """
    ).fetchall()

    signals = _signals(item)

    best = None
    best_score = -1.0

    for row in rows:
        (
            event_id,
            canonical,
            first_seen,
            last_seen,
            major,
            queued_count,
            canonical_summary,
        ) = row[:7]

        try:
            last = datetime.fromisoformat(
                last_seen.replace("Z", "+00:00")
            )
        except Exception:
            continue

        hours = (
            major_memory_hours
            if major
            else memory_hours
        )

        if (now - last).total_seconds() > hours * 3600:
            continue

        state = _parse_state(row, canonical, canonical_summary)
        identity = state.get("identity") or {}

        if _location_conflict(signals, identity):
            continue

        if _storm_conflict(signals, identity):
            continue

        if _magnitude_conflict(signals, identity):
            continue

        m = _match_score(signals, identity)

        # Matching is decided by the rule-based combination
        # checks ONLY, against the event's immutable identity.
        # The score ranks among accepted matches.
        if m["score"] > best_score and _match_rule(signals, m):
            best_score = m["score"]
            best = (row, state, m)

    # ---------------------------------------------------------
    # No matching recent event: this is a new event.
    # ---------------------------------------------------------
    if best is None:
        event_id = _new_id(
            item.get("title", "")
            + "|"
            + item.get("source", "")
        )
        state = _state_from_signals(item, signals)
        _insert_event(conn, event_id, item, state, now)
        return ("NEW", event_id, 1.0)

    (row, state, m) = best
    event_id = row[0]
    source = item.get("source", "")

    # ---------------------------------------------------------
    # Same source + same event: never repost the same feed's
    # copy of the same story.
    # ---------------------------------------------------------
    if _same_event_source(conn, event_id, source):
        _merge_state(item, signals, state)
        _persist_state(conn, event_id, state, item, now)
        return ("DUPLICATE", event_id, m["score"])

    # ---------------------------------------------------------
    # Matched event + material development -> UPDATE.
    # ---------------------------------------------------------
    if _material_change(signals, state):
        _merge_state(item, signals, state)
        _update_event(conn, event_id, item, state, now)
        return ("UPDATE", event_id, m["score"])

    # ---------------------------------------------------------
    # Matched event, no material change -> DUPLICATE.  The
    # phrasing is still folded into the ACCUMULATED memory so a
    # future reworded headline is recognized, but it never
    # touches the identity and never broadens the match surface.
    # ---------------------------------------------------------
    _merge_state(item, signals, state)
    _persist_state(conn, event_id, state, item, now)
    return ("DUPLICATE", event_id, m["score"])


def mark_queued(conn, event_id):
    conn.execute(
        """
        UPDATE events
        SET queued_count=queued_count+1
        WHERE event_id=?
        """,
        (event_id,),
    )


def purge_expired(
    conn,
    story_memory_hours=48,
    memory_hours=48,
    major_memory_hours=168
):
    """Delete only records whose retention period has elapsed.

    - Individual stories expire after story_memory_hours.
    - Normal events expire after memory_hours.
    - Major events expire after major_memory_hours.

    Timestamp-based, idempotent, and safe to run on every
    collection cycle. Active records are never touched.
    """
    now = datetime.now(timezone.utc)

    story_cutoff = (
        now - timedelta(hours=story_memory_hours)
    ).isoformat()

    event_cutoff = (
        now - timedelta(hours=memory_hours)
    ).isoformat()

    major_cutoff = (
        now - timedelta(hours=major_memory_hours)
    ).isoformat()

    stories_expired = conn.execute(
        """
        DELETE FROM stories
        WHERE first_seen < ?
        """,
        (story_cutoff,),
    ).rowcount

    normal_events_expired = conn.execute(
        """
        DELETE FROM events
        WHERE major=0 AND last_seen < ?
        """,
        (event_cutoff,),
    ).rowcount

    major_events_expired = conn.execute(
        """
        DELETE FROM events
        WHERE major=1 AND last_seen < ?
        """,
        (major_cutoff,),
    ).rowcount

    conn.commit()

    return {
        "stories_expired": stories_expired,
        "normal_events_expired": normal_events_expired,
        "major_events_expired": major_events_expired,
    }
