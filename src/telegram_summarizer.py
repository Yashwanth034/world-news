"""Source-grounded Telegram summarizer.

Deterministic, conservative summarization for every important
story.  A 2-4 sentence summary is composed from facts extracted
from the available source material:

    1. clean    - the existing editorial cleaning/filtering
                  pipeline (boilerplate, filler, headline
                  paraphrase, dedup, conflict) runs first;
    2. facts    - what happened, who/what is involved, where,
                  when, important numbers, consequences are
                  extracted from each surviving sentence;
    3. compose  - fact-important sentences are selected
                  (2-4), article text is the primary source
                  when article extraction is available, and
                  only conservative fact-preserving rewrites
                  (trailing attribution fronting) are applied;
    4. order    - selected sentences are ordered into a
                  natural narrative: what happened first, then
                  where/when/how-many detail, then the
                  consequence or current status;
    5. verify   - every fact token and every sentence tail
                  must exist verbatim in the source text;
    6. quality  - completeness, artifacts, truncation,
                  headline repetition and duplication checks;
    7. consistency - headline facts (score, win/loss,
                  casualties, strong claims) must agree with
                  the source; contradictions reject the story.

Nothing is ever invented: the summary contains only facts that
exist in the source, phrased in the source's own words.  A
story with fewer than two genuinely useful source-supported
sentences is rejected, and the final body never exceeds four
sentences.
"""
import re

from src.telegram_briefing import (
    ENTITY_ALIASES,
    KNOWN_ENTITIES,
    LOCATION_SET,
    NON_FACT_WORDS,
    PARAPHRASE_SYNONYMS,
    STOPWORDS,
    WORD_RE,
    _actions,
    _content_tokens,
    _entity_words,
    _normalized,
    _numbers_of,
    _stem_lite,
    _temporal_of,
    is_filler,
    is_headline_paraphrase,
    is_near_duplicate,
    strip_boilerplate,
)

_KEEP_CAPITAL = (
    KNOWN_ENTITIES
    | LOCATION_SET
    | set(ENTITY_ALIASES)
    | {
        "i", "january", "february", "march", "april", "june",
        "july", "august", "september", "october", "november",
        "december", "monday", "tuesday", "wednesday",
        "thursday", "friday", "saturday", "sunday",
    }
)

# ---------------------------------------------------------
# Config defaults
# ---------------------------------------------------------

MIN_SENTENCES = 2
MAX_SENTENCES = 4
TIER1_MAX = 4

# Hard ceiling for the final body.  Never exceeded, whatever
# a configuration file asks for.
HARD_MAX_SENTENCES = 4

ARTICLE_ITEM_SUFFIX = ":article"

# ---------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------

REPORTING_VERBS = {
    "said", "says", "told", "report", "reported", "reports",
    "warn", "warned", "warns", "added", "adds", "noted",
    "notes", "confirmed", "confirms", "stated", "states",
    "announced", "announces", "declared", "declares",
    "stressed", "claimed", "claims", "suggested", "suggests",
    "revealed", "reveals", "explained", "explains",
}

SPEAKER_MAX_WORDS = 4

# Trailing attribution: "Three people were injured, police
# said." or "... , according to officials."
ATTRIBUTION_TRAIL_RE = re.compile(
    r",\s*(?:"
    r"according to\s+"
    r"([A-Za-z][\w\u2019'-]*(?:\s+[A-Za-z][\w\u2019'-]*){0,3})"
    r"|"
    r"([A-Za-z][\w\u2019'-]*(?:\s+[A-Za-z][\w\u2019'-]*){0,3})"
    r"\s+("
    + "|".join(REPORTING_VERBS)
    + r")"
    r")\s*[.!?]?\s*$",
    re.IGNORECASE,
)

# Impact units: consequences stated by the source.
IMPACT_UNITS = {
    "killed", "injured", "dead", "deaths", "fatalities",
    "displaced", "evacuated", "evacuees", "missing",
    "homes", "residents", "families", "troops", "soldiers",
    "hostages", "officers", "people",
}

# Spelled-out numbers that carry the same consequence weight as
# digits ("kill five people", "two dozen wounded"): sources
# routinely spell small casualty figures.
_SPELLED_NUMBER_ALT = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|dozen|dozens|hundred|hundreds|"
    r"thousand|thousands|million|millions"
)

IMPACT_RE = re.compile(
    r"(?:(?:more than|at least|nearly|about|around|over|"
    r"up to|almost|roughly)\s+)?"
    r"(?:\d[\d,]*(?:\.\d+)?|\b(?:"
    + _SPELLED_NUMBER_ALT
    + r"))\s*"
    r"\b(?:"
    + "|".join(
        sorted(IMPACT_UNITS, key=len, reverse=True)
    )
    + r")\b",
    re.IGNORECASE,
)

QUANTIFIER_STRIP_RE = re.compile(
    r"^(?:more than|at least|nearly|about|around|over|"
    r"up to|almost|roughly)\s+",
    re.IGNORECASE,
)

LOCATION_RE = re.compile(
    r"\b(?:in|across|near|around|at|from|toward|towards)\s+"
    r"([A-Z][A-Za-z\u2019'-]*(?:\s+(?:"
    r"[A-Z][A-Za-z\u2019'-]*|(?:city|town|state|province|"
    r"county|island|prefecture|region|capital|coast|valley|"
    r"bay|river|village|district|area|park|north|south|east|"
    r"west|central|northern|southern|eastern|western|"
    r"national|metropolitan)\b"
    r")){0,2})"
)


def extract_facts(text):
    """Structured facts in a sentence: numbers, named entities,
    locations, temporal references, attribution and impact.

    Returns a dict of fact sets; never invents anything, every
    fact is literal text from `text`."""
    text = str(text or "")
    numbers = {
        n for n in _numbers_of(text)
    }
    entities = {
        e for e in _entity_words(text)
    }
    locations = set()
    for match in LOCATION_RE.finditer(text):
        location = match.group(1).strip()
        if len(location.split()) <= 4:
            locations.add(location.lower())
    temporal = set(_temporal_of(text))
    attribution = None
    if ATTRIBUTION_TRAIL_RE.search(text):
        attribution = "trailing"
    elif re.match(
        r"^[A-Za-z][\w\u2019'-]*(?:\s+[A-Za-z][\w\u2019'-]*){0,3}"
        r"\s+(?:(?:have|has|had|are|is|were|was|am)\s+)?"
        r"(?:"
        + "|".join(REPORTING_VERBS)
        + r")\b",
        text,
        re.IGNORECASE,
    ):
        attribution = "leading"
    impact = [
        match.group(0)
        for match in IMPACT_RE.finditer(text)
    ]
    return {
        "numbers": numbers,
        "entities": entities,
        "locations": locations,
        "temporal": temporal,
        "attribution": attribution,
        "impact": impact,
    }


def sentence_fact_score(facts):
    """Importance score of a sentence's facts.

    Consequences (impact numbers) weigh most, then plain
    numbers, locations, named entities, temporal references
    and attribution.  Scores are capped per category so long
    sentences cannot game the ranking."""
    score = 0.0
    score += 3.0 * min(2, len(facts["impact"]))
    score += 1.5 * min(2, len(facts["numbers"]))
    score += 1.0 * min(2, len(facts["locations"]))
    score += 0.75 * min(2, len(facts["entities"]))
    score += 0.5 if facts["temporal"] else 0
    score += 0.5 if facts["attribution"] else 0
    return score


def _has_fact(facts):
    return bool(
        facts["impact"]
        or facts["numbers"]
        or facts["locations"]
        or facts["entities"]
        or facts["temporal"]
        or facts["attribution"]
    )


# ---------------------------------------------------------
# Quote-boundary sentence re-splitting
#
# The formatter's split_sentences cannot see a boundary trapped
# inside a closing quote: "...food." The IMF said ...  -> the
# period sits inside the quote, so ". followed by a space is not
# a split point. The post-quote sentence (the IMF attribution +
# its statistic) fuses with the quoted fragment and, when it is
# the only sentence of the story, the whole story is rejected for
# having fewer than two sentences: the IMF statistic is lost.
#
# This splitter runs in the summarizer's own sentence-splitting
# stage: it peels a closing-quote boundary (".. punctuation,
# closing quote, whitespace, Capitalized word") into its own row
# so the post-quote sentence is selected, composed, verified and
# quality-checked as an independent fact. Continuous quoted
# sentences - where no closing quote precedes the boundary - are
# never touched, so quoted dialogue is preserved verbatim.
# ---------------------------------------------------------

# Terminal punctuation, a closing quote char, then whitespace.
# The lookbehind anchors on the two characters immediately before
# the whitespace; only the period-inside-quote gap matches.
_CLOSED_QUOTE_BOUNDARY_RE = re.compile(
    r"(?<=[.!?][\'\"\u2018\u2019\u201c\u201d])\s+"
)


def _split_quote_boundaries(rows, headline=None):
    """Split rows that fuse a quoted sentence with the sentence
    that follows its closing quote, e.g.

        "...food." The International Monetary Fund said ...

    Each fused row becomes one row per real sentence, the
    provenance fields (source, item_id and any others) copied to
    every derived row. Rows without a trapped closing-quote
    boundary are passed through unchanged.

    A split fragment that is a pure headline paraphrase (a lone
    quoted statement that restates the headline and adds no fact,
    e.g. the "We should reduce food." part of an IMF quote) is
    dropped: the headline is not repeated in the summary. The
    fact-bearing sentence after the quote is always kept."""
    if not rows:
        return rows
    out = []
    for row in rows:
        text = row.get("text") or ""
        if not _CLOSED_QUOTE_BOUNDARY_RE.search(text):
            out.append(row)
            continue
        parts = [
            p for p in _CLOSED_QUOTE_BOUNDARY_RE.split(text)
            if p.strip()
        ]
        if len(parts) <= 1:
            out.append(row)
            continue
        for part in parts:
            if (
                headline
                and is_headline_paraphrase(part, headline)
            ):
                continue
            new_row = dict(row)
            new_row["text"] = part
            out.append(new_row)
    return out


# ---------------------------------------------------------
# Fused-sentence repair (HTML / line-break extraction damage)
#
# Webpage formatting occasionally drops the boundary between two
# sentences, fusing them with no punctuation:
#
#     "...rehabilitation centre Orangutans rescued from wildlife
#      traffickers..."
#
# The lowercase-to-Capitalized transition is the only damage.  The
# capitalised word here is a plural common noun starting a fresh
# sentence ("Orangutans rescued ..."), NOT a proper name: the
# lowercased form also appears in the source as an ordinary word.
# Such rows are safely reconstructed by inserting ". " at the
# boundary.  A fused row whose capitalised word is never used as a
# common word elsewhere is treated as corrupted (a proper name is
# ambiguous - "Ponds and collection tanks" could be a facility
# name) and the whole sentence is dropped rather than published
# with damaged text.
# ---------------------------------------------------------

# Words that can begin the second half of a fused sentence: a verb
# or a conjunction.  Prepositions/articles are deliberately absent
# ("...in Paris today" is not a boundary).
_FUSED_FOLLOW_WORDS = {
    "and", "or", "but", "while", "as", "when", "although",
    "because", "since", "where", "though", "unless",
    "is", "are", "was", "were", "has", "have", "had", "will",
    "would", "can", "could", "should", "may", "might", "must",
    "does", "did", "do", "said", "says", "say", "told",
    "added", "claimed", "reported", "confirmed", "announced",
    "warned", "warns", "noted", "stated", "explained",
    "revealed", "rescued", "rescuing", "died", "kills",
    "killed", "struck", "strikes", "hit", "hits", "fled",
    "flees", "found", "discovered", "survived", "survives",
    "rose", "rises", "fell", "falls", "grew", "grows", "came",
    "comes", "began", "begins", "started", "starts",
    "continued", "continues", "remained", "remains", "faces",
    "faced", "forced", "pushed", "moved", "moves", "arrived",
    "arrives", "left", "leaves", "followed", "follows",
    "threatened", "threatens", "spread", "spreads", "ended",
    "ends", "took", "takes", "made", "makes", "raised",
    "raises", "warn", "expects", "expected", "plans", "planned",
    "called", "calls", "urged", "urges", "opens", "opened",
    "closes", "closed", "returns", "returned", "plunged",
    "plunges", "surged", "surges", "dropped", "drops", "jumped",
    "jumps", "wins", "won", "lost", "loses",
}

# Words that can directly precede a proper name mid-phrase, so a
# capitalised word after one of these is a name, not a boundary
# ("with Apple", "in Paris", "the White House", "mayor Johnson",
# "said Putin").  Checked one and two words back.
_FUSED_SKIP_PREV = {
    "the", "a", "an", "in", "on", "at", "of", "from", "to",
    "by", "with", "for", "against", "over", "under", "after",
    "before", "into", "onto", "upon", "about", "near", "around",
    "across", "within", "without", "behind", "between", "beyond",
    "during", "inside", "outside", "through", "throughout",
    "toward", "towards", "via", "per", "vs", "versus", "like",
    "including", "excluding", "amid", "beside", "besides",
    "his", "her", "their", "its", "our", "my", "your", "whose",
    "said", "says", "say", "told", "report", "reports",
    "reported", "meet", "met", "visits", "visited", "visit",
    "against",
}

# Month/day names and known proper names that are legitimately
# capitalised mid-sentence and never start a fused second half.
_FUSED_SKIP_WORDS = {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november",
    "december", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
} | set(KNOWN_ENTITIES) | set(LOCATION_SET) | set(ENTITY_ALIASES)


_FUSED_SCAN_RE = re.compile(
    r"\b([a-z][a-z\u2019'-]*)\s+"
    r"([A-Z][a-z]+(?:-[A-Za-z]+)*)\s+"
    r"([a-z][a-z\u2019'-]*)\b"
)

# A second fused form: the previous word ends with a CLOSING
# quote/apostrophe ("'digital lifeline' In Taipei...", "...taxing
# gas exports' Cross-benchers have...", "'over the top' Albanese
# said...").  The closing quote is itself the boundary signal - a
# new sentence starts right after it - so no verb/conjunction
# follow-check is needed.  Possessives ("president's State of the
# Union") never match: the word ends with "'s", not with the quote
# character.
_FUSED_QUOTE_SCAN_RE = re.compile(
    r"\b([a-z][a-z\u2019'-]*['\"\u2019\u201d])\s+"
    r"([A-Z][a-z]+(?:-[A-Za-z]+)*)\s+"
    r"([A-Za-z][a-z\u2019'-]*)\b"
)

# A third fused form: a NUMBER directly followed by a Capitalised
# word ("...since 2001 Looking back at the yen...").  The year is
# the boundary signal - like the closing quote - so no verb/
# conjunction follow-check is needed: the capitalised word only
# needs to be a plausible sentence start (itself a gerund like
# "Looking", or followed by a verb/conjunction) and not a known
# proper name or place.  The word before the number must be able
# to end a sentence ("in 2001 Looking" - "in" blocks the false
# split; "since 2001 Looking" splits).
_FUSED_NUMBER_SCAN_RE = re.compile(
    r"\b(\d{2,4})\s+"
    r"([A-Z][a-z]+(?:-[A-Za-z]+)*)\s+"
    r"([a-z][a-z\u2019'-]*)\b"
)

# A fourth fused form: a word glued to an ellipsis and then a
# Capitalised word ("...at the yen…Professor Costas Milas...").
# The ellipsis itself is the boundary signal - a live-blog
# subheading ends with "…" and the next item starts a fresh
# sentence - so no follow-word check is needed, exactly like the
# closing-quote rule.
_FUSED_ELLIPSIS_RE = re.compile(
    r"([a-z][a-z\u2019'-]*)\s*(?:\u2026|\.{3})\s*"
    r"([A-Z][a-z]+(?:-[A-Za-z]+)*)"
)


def _fused_boundary(text):
    """(position, after_closing_quote) of a fused sentence
    boundary in `text`, or (None, False).

    A boundary is a lowercase word directly followed by a
    Title-case word and then a lowercase verb/conjunction, where
    the capitalised word is not a known proper name, not a
    month/day, not preceded by an article/preposition/title
    (within two words) or another capitalised word.  A boundary
    after a closing quote ("...'digital lifeline' In Taipei")
    needs no follow-word check - the quote is the signal, and is
    reported with `after_closing_quote=True`.

    "...centre Orangutans rescued..." matches; "visited New
    York", "the White House", "in Paris", "mayor Johnson",
    "Hurricane Helene", "president's State of the Union" and
    "on Tuesday" never do.
    """
    for match in _FUSED_SCAN_RE.finditer(text or ""):
        prev, word, nxt = match.groups()
        if nxt.lower() not in _FUSED_FOLLOW_WORDS:
            continue
        if word.lower() in _FUSED_SKIP_WORDS:
            continue
        if prev[:1].isupper():
            continue
        # A previous word that ENDS with a closing quote is the
        # quote-rule case ("...'digital lifeline' In Taipei") and
        # is handled by _FUSED_QUOTE_SCAN_RE below - not here.
        if prev.endswith(("'", '"', "\u2019", "\u201d")):
            continue
        # The two words immediately before the capitalised word:
        # "the river Thames" (article before the name) must never
        # split, while "rehabilitation centre Orangutans" (plain
        # nouns) is a genuine dropped boundary.  The window starts
        # AT the capitalised word, not at the scan match start,
        # which would wrongly include the words before `prev`.
        before = text[: match.start(2)]
        prev_words = re.findall(
            r"[A-Za-z\u2019'-]+", before
        )[-2:]
        if any(
            w.lower() in _FUSED_SKIP_PREV
            for w in prev_words
        ):
            continue
        return match.start(2), False
    for match in _FUSED_QUOTE_SCAN_RE.finditer(text or ""):
        prev, word, _ = match.groups()
        if word.lower() in _FUSED_SKIP_WORDS:
            continue
        if prev[:1].isupper():
            continue
        return match.start(2), True
    # Number -> capital: "...costs since 2001 Looking back at the
    # yen".  A year directly followed by a Capitalised word that
    # is not a known name/place (and not preceded by an
    # article/preposition within two words) is a dropped boundary.
    for match in _FUSED_NUMBER_SCAN_RE.finditer(text or ""):
        prev, word, nxt = match.groups()
        if word.lower() in _FUSED_SKIP_WORDS:
            continue
        if not (
            nxt.lower() in _FUSED_FOLLOW_WORDS
            or word.lower().endswith("ing")
        ):
            continue
        before = text[: match.start(2)]
        prev_words = re.findall(
            r"[A-Za-z\u2019'-]+", before
        )[-2:]
        if any(
            w.lower() in _FUSED_SKIP_PREV
            for w in prev_words
        ):
            continue
        return match.start(2), False
    # Ellipsis -> capital: "...yen…Professor Costas Milas".  The
    # ellipsis is the boundary signal; the capitalised word only
    # needs to not be a known name/place.
    for match in _FUSED_ELLIPSIS_RE.finditer(text or ""):
        word = match.group(2)
        if word.lower() in _FUSED_SKIP_WORDS:
            continue
        return match.start(2), True
    return None, False


def _looks_fused(text):
    """True when a sentence fuses two sentences with no
    boundary ("...centre Orangutans rescued...").  Used by the
    quality gate to drop any fused sentence that survives to
    composition."""
    position, _ = _fused_boundary(text)
    return position is not None


# A reporting verb directly followed by a Capitalised word and a
# preposition: "...Rightmove says Searches for homes...".  The
# capitalised word is a common noun/verb here, NOT a proper name
# ("said Putin for..." is a name and must never split).  The
# distinction is decided by the same common-word evidence check
# the generic fused rule uses: only a capitalised word that is
# used as an ordinary word elsewhere in the source is a boundary.
_FUSED_REPORTING_RE = re.compile(
    r"\b([a-z][a-z\u2019'-]*)\s+"
    r"([A-Z][a-z]+(?:-[A-Za-z]+)*)\s+"
    r"(?:for|on|at|in|to|with|from|of|after|before|during|through|"
    r"across|within|without|about|over|under|into|onto|upon|per|"
    r"among|between|against|via|around|near|past|since|until)\b"
)


def _reporting_verb_boundary(text, evidence):
    """Position of a fused boundary after a reporting verb
    ("...Rightmove says Searches for homes...") or None.

    A reporting verb can close one sentence while the next starts
    with a Capitalised common word ("Searches", "Talks").  Unlike
    the generic rule this only fires when the capitalised word is
    used as an ordinary common word in the source evidence - a
    proper name like "Putin" or "Johnson" never is - so "said
    Putin for..." is never touched."""
    if not evidence:
        return None
    for match in _FUSED_REPORTING_RE.finditer(text or ""):
        prev, word = match.groups()
        if prev.lower() not in REPORTING_VERBS:
            continue
        if word.lower() in _FUSED_SKIP_WORDS:
            continue
        singular = word.lower().rstrip("s")
        parts = re.split(r"[- ]", singular)
        evidence_re = (
            r"\b" + r"[- ]?".join(
                re.escape(p) for p in parts
            ) + r"s?\b"
        )
        if re.search(evidence_re, evidence):
            return match.start(2)
    return None


def _repair_fused_rows(rows, headline, source_text):
    """Reconstruct or reject rows damaged by HTML/line-break
    extraction.

    A fused row ("...centre Orangutans rescued...") whose
    capitalised word is used as an ordinary common word elsewhere
    in the source (title + source text) is split into its two
    real sentences with ". " inserted - no words are deleted.
    A fused row without that evidence cannot be safely
    reconstructed (the capitalised word may be a proper name) and
    is dropped instead of publishing corrupted text.
    """
    if not rows:
        return rows
    out = []
    for row in rows:
        text = row.get("text") or ""
        position, after_quote = _fused_boundary(text)
        # A reporting verb followed by a Capitalised common word
        # and a preposition ("...Rightmove says Searches for
        # homes...") is a boundary only when the capitalised word
        # is used as a common word in the source; the evidence is
        # checked inside the helper, and the row is left untouched
        # otherwise (a proper name is not corruption).
        reporting_boundary = None
        if position is None:
            other_source = (str(source_text or "")).replace(
                text, " "
            )
            evidence = " ".join(
                [str(headline or ""), other_source]
            ).lower()
            reporting_boundary = _reporting_verb_boundary(
                text, evidence
            )
        if position is None and reporting_boundary is None:
            out.append(row)
            continue
        if position is None:
            # Reporting-verb fusion ("...Rightmove says Searches
            # for homes..."): the capitalised word is a common
            # noun/verb (verified inside _reporting_verb_boundary
            # against the source evidence), so the clean
            # reconstruction is to lowercase it - every word is
            # preserved and the original sentence is restored.
            # Splitting here would orphan a "Rightmove says."
            # fragment, so this path never splits.
            word_match = re.match(
                r"([A-Z][a-z]+(?:-[A-Za-z]+)*)",
                text[reporting_boundary:],
            )
            if not word_match:
                out.append(row)
                continue
            cap_word = word_match.group(1)
            new_row = dict(row)
            new_row["text"] = (
                text[:reporting_boundary]
                + cap_word.lower()
                + text[
                    reporting_boundary + len(cap_word):
                ]
            )
            out.append(new_row)
            continue
        # Common-word evidence must come from OUTSIDE this row: the
        # fused text itself always contains the capitalised form,
        # so the row's own text is excluded from the evidence.
        # A boundary after a closing quote is unambiguous on its
        # own (a quoted fragment ends, a new sentence starts), so
        # it is always reconstructed.
        word = re.match(
            r"([A-Z][a-z]+(?:-[A-Za-z]+)*)", text[position:]
        ).group(1).lower()
        if not after_quote:
            other_source = (str(source_text or "")).replace(
                text, " "
            )
            evidence = " ".join(
                [str(headline or ""), other_source]
            ).lower()
            # Common-word evidence, tolerant of hyphen/space
            # variants ("Cross-benchers" ~ "crossbenchers").
            singular = word.rstrip("s")
            parts = re.split(r"[- ]", singular)
            evidence_re = (
                r"\b" + r"[- ]?".join(
                    re.escape(p) for p in parts
                ) + r"s?\b"
            )
            if not re.search(evidence_re, evidence):
                # No safe reconstruction - the corrupted
                # sentence is dropped entirely.
                continue
        part1 = text[:position].rstrip()
        part2 = text[position:].strip()
        if not part1 or not part2:
            continue
        part1 = part1 + "." if not part1.endswith(
            (".", "!", "?")
        ) else part1
        for part in (part1, part2):
            if part.strip():
                new_row = dict(row)
                new_row["text"] = part
                out.append(new_row)
    return out


# ---------------------------------------------------------
# Live-blog relevance filtering
#
# Multi-topic live-blog / feed summaries often append unrelated
# stories ("Meanwhile, the IMF said ... 15 percent"; "Separately,
# a solar eclipse ...") after the lead. Such items carry no
# fact connected to the headline and must not enter the summary.
# Legitimate same-event context (a quote, a consequence, a
# shared place) is never dropped: a row is removed only when it
# has a STRONG off-topic signal AND no topical link to the
# headline at all.
# ---------------------------------------------------------

# Discourse markers that introduce a new sub-topic / separate
# story in a live-blog feed.
_TOPICSHIFT_PREFIX_RE = re.compile(
    r"^\s*(?:\"|\u201c|\u2018[^A-Za-z])*"  # optional leading quote
    r"(?:meanwhile|separately|however|in contrast|on the other "
    r"hand|elsewhere|additionally|also|furthermore|moreover|in "
    r"other news|in other developments|besides|as for|turning to|"
    r"on another note|by the way|in the meantime|on a different "
    r"note|shifting focus|in unrelated news|away from)|[^\w]",
    re.IGNORECASE,
)


def _capitalized_runs(text):
    """Multi-word proper-name runs (two-plus consecutive
    capitalized tokens) in the text, lowercased."""
    words = WORD_RE.findall(str(text or ""))
    runs = []
    i = 0
    while i < len(words):
        if words[i][:1].isupper():
            run = [words[i]]
            j = i + 1
            while j < len(words) and words[j][:1].isupper():
                run.append(words[j])
                j += 1
            if len(run) >= 2:
                runs.append(" ".join(run).lower())
            i = j
        else:
            i += 1
    return runs


def _off_topic_proper_entity(text, headline):
    """True when the text introduces a multi-word proper name
    that is not a location and is not mentioned in the headline.

    "International Monetary Fund" next to a wildfire headline is
    off-topic; "US, Canada and Mexico" (a shared location set) is
    not, and neither is "Bald Range wildfire" (links via the
    'wildfire' action)."""
    headline_lower = (headline or "").lower()
    headline_entities = set(_entity_words(headline))
    known = set(KNOWN_ENTITIES) | set(ENTITY_ALIASES)
    for run in _capitalized_runs(text):
        if run in headline_lower:
            continue
        if run in known:
            continue
        words = run.split()
        if any(
            w in LOCATION_SET
            or ENTITY_ALIASES.get(w, w) in LOCATION_SET
            for w in words
        ):
            continue
        if any(
            w in headline_entities
            or ENTITY_ALIASES.get(w, w) in headline_entities
            for w in words
        ):
            continue
        return True
    return False


def _row_links_to_headline(text, headline):
    """Whether the row shares any fact-bearing link with the
    headline: a named entity, a place, an action word, a content
    token (stem-equal or substring) or an impact/consequence
    fact."""
    if not text or not headline:
        return False
    he = set(_entity_words(headline))
    re_ = set(_entity_words(text))
    if re_ & he:
        return True
    if (re_ & LOCATION_SET) & (he & LOCATION_SET):
        return True
    if _actions(text) & _actions(headline):
        return True
    # Content-word overlap, normalized through the synonym and
    # word-family maps ("wildfire" ~ "the fire"/"the flames"), so
    # a same-event sentence that paraphrases the headline's subject
    # is not dropped as off-topic.
    headline_stems = _coherence_stems(headline)
    text_stems = _coherence_stems(text)
    if text_stems & headline_stems:
        return True
    for stem in text_stems:
        for hs in headline_stems:
            if (
                len(stem) >= 4
                and len(hs) >= 4
                and (stem in hs or hs in stem)
            ):
                return True
    if IMPACT_RE.search(text):
        return True
    return False


def _drop_unrelated_rows(rows, headline):
    """Filter out live-blog paragraphs that are clearly about a
    different story than the headline.

    A row is dropped only when it has a strong off-topic signal
    (a topic-shift marker or an unrelated multi-word proper name)
    AND no topical link to the headline. Same-event context that
    simply lacks a lexical overlap with the headline - a quote, a
    weather detail, a local place - is left untouched."""
    kept = []
    for row in rows:
        text = row.get("text") or ""
        if not text.strip():
            continue
        strong_signal = bool(_TOPICSHIFT_PREFIX_RE.match(text)) or (
            _off_topic_proper_entity(text, headline)
        )
        if strong_signal and not _row_links_to_headline(text, headline):
            continue
        kept.append(row)
    return kept


# ---------------------------------------------------------
# Conservative composition (fact-preserving rewrites only)
# ---------------------------------------------------------


def _lower_body_start(word):
    """Lowercase a body's first word after attribution
    fronting, unless it is a proper noun: acronyms, known
    entities/places/aliases, month/day names and possessives
    ("Max's") keep their capital."""
    if len(word) >= 2 and word.isupper():
        return word
    base = word.rstrip("\u2019'")
    if base.lower() in _KEEP_CAPITAL:
        return word
    if base.endswith("\u2019s") or base.endswith("'s"):
        return word
    return word[0].lower() + word[1:]


def front_attribution(sentence):
    """Move a trailing reporting attribution to the front.

    "Three people were injured, police said." becomes
    "Police said three people were injured." and
    "..., according to officials." becomes
    "According to officials, ...".

    Only applied when the trailing attribution is the final
    comma-separated clause of the sentence, the body is a
    complete clause of its own, and the body neither ends
    with terminal punctuation (it would be a second sentence)
    nor begins with a quotation.  Never rewrites facts: the
    speaker, the reporting verb and the body are verbatim.
    """
    if not sentence:
        return sentence
    match = ATTRIBUTION_TRAIL_RE.search(sentence)
    if not match:
        return sentence
    according_to = match.group(1) is not None
    speaker = (
        match.group(1)
        if according_to
        else match.group(2)
    )
    verb = match.group(3) or ""
    body = sentence[: match.start()].rstrip()
    body = re.sub(r"\s*[,.;:]+$", "", body)
    if len(body) < 6:
        return sentence
    if body[-1:] in ".!?":
        return sentence
    if body[:1] in "\"'\"\u201c\u2018\u00ab\u00bb":
        return sentence
    if speaker.split() and any(
        char.isdigit() for char in speaker
    ):
        return sentence
    if len(speaker.split()) > SPEAKER_MAX_WORDS:
        return sentence
    body_words = body.split()
    body_words[0] = _lower_body_start(body_words[0])
    body = " ".join(body_words)
    if according_to:
        front = "According to " + speaker + ","
        composed = front + " " + body
    else:
        front = (
            speaker[0].upper() + speaker[1:]
            if speaker[0].islower()
            else speaker
        ) + " " + verb
        composed = front + " " + body
    if not composed.endswith((".", "!", "?")):
        composed += "."
    return composed


# ---------------------------------------------------------
# Verification against the source
# ---------------------------------------------------------


# Fronted attribution, the composed form: "Police said ..."
# or "According to officials, ...".  For the tail check the
# body is what must mirror the source.
FRONT_ATTRIBUTION_RE = re.compile(
    r"^(?:"
    r"according to\s+"
    r"([A-Za-z][\w\u2019'-]*(?:\s+[A-Za-z][\w\u2019'-]*){0,3})"
    r",\s*"
    r"|"
    r"([A-Za-z][\w\u2019'-]*(?:\s+[A-Za-z][\w\u2019'-]*){0,3})"
    r"\s+("
    + "|".join(REPORTING_VERBS)
    + r")\s+"
    r")",
    re.IGNORECASE,
)


def _tail_substring(sentence, source_text):
    """The sentence tail (up to 40 chars) must exist verbatim
    in the source: catches truncation artifacts and rewrites
    that no longer mirror the source.  A fronted attribution
    is stripped first so short bodies verify: the attribution
    is itself verbatim from the source, only moved."""
    sentence = re.sub(r"\s+", " ", str(sentence or "")).strip()
    source = re.sub(r"\s+", " ", str(source_text or "")).strip()
    if not sentence:
        return True
    match = FRONT_ATTRIBUTION_RE.match(sentence)
    if match:
        sentence = sentence[match.end():]
    tail = sentence[-40:]
    return tail.lower() in source.lower()


def verify_row(row, source_text):
    """Check every fact token of a composed sentence against
    the source text.  Returns (verified, problems)."""
    text = row.get("text") or ""
    problems = []
    source_text = str(source_text or "")

    facts = extract_facts(text)

    missing_numbers = facts["numbers"] - _numbers_of(source_text)
    if missing_numbers:
        problems.append(
            "unsupported number(s): "
            + ", ".join(sorted(missing_numbers))
        )

    missing_entities = facts["entities"] - _entity_words(
        source_text
    )
    if missing_entities:
        problems.append(
            "unsupported name(s): "
            + ", ".join(sorted(missing_entities))
        )

    missing_temporal = facts["temporal"] - _temporal_of(
        source_text
    )
    if missing_temporal:
        problems.append(
            "unsupported time reference(s): "
            + ", ".join(sorted(missing_temporal))
        )

    for location in facts["locations"]:
        if location not in source_text.lower():
            problems.append(
                "unsupported location: " + location
            )

    if not _tail_substring(text, source_text):
        problems.append("sentence tail not in source")

    return (not problems, problems)


# ---------------------------------------------------------
# Final quality gate
# ---------------------------------------------------------

HTML_ENTITY_RE = re.compile(r"&[a-zA-Z#][a-zA-Z0-9#]*;")

# Function words that a well-formed sentence never ends on.
DANGLING_LAST_WORDS = {
    "and", "or", "but", "because", "while", "during", "after",
    "before", "with", "without", "from", "to", "of", "in",
    "on", "at", "for", "as", "than", "that", "which", "who",
    "where", "when", "an", "a", "the", "if", "then", "so",
}


def _balanced_quotes(text):
    """Quotes balance as matched open/close pairs.

    Handles straight quotes ('"') and curly pairs
    (\u201c/\u201d).  A single \u201cWe ...\u201d pair is
    balanced even though each character occurs once; a lone
    opening or closing quote of either family is not."""
    straight = 0
    stack = []
    for ch in text:
        if ch == '"':
            straight = 1 - straight
        elif ch == "\u201c":
            stack.append("\u201c")
        elif ch == "\u201d":
            if not stack:
                return False
            stack.pop()
    return straight == 0 and not stack


def _dangling_ending(text):
    """True when the sentence ends with a dangling function
    word ("in the region" is fine; a sentence that truly ends
    "in the" is a cut)."""
    lowered = text.lower()
    words = re.findall(
        r"[a-z0-9\u2019'-]+", lowered
    )
    if not words:
        return False
    return words[-1] in DANGLING_LAST_WORDS


# ---------------------------------------------------------
# Question / information gate
#
# The body must answer "what actually happened".  A summary that
# is only questions, rhetorical questions, vague background or
# article-introduction text explains nothing and is rejected -
# the summarizer never invents an answer.  A single question
# inside an otherwise factual summary is harmless; a summary
# with no plain declarative fact sentence is not news.
# ---------------------------------------------------------

_QUESTION_START_RE = re.compile(
    r"^(?:why|how|what|when|where|who|whose|which|whom|"
    r"is|are|was|were|do|does|did|can|could|will|would|"
    r"should|has|have|had|may|might|must)\b",
    re.IGNORECASE,
)


def _is_question(text):
    """True for a question sentence: terminal '?', or a
    question-word / inverted-auxiliary start.  "How to ..."
    instructions are not questions."""
    text = str(text or "").strip()
    if not text:
        return False
    if text.endswith("?"):
        return True
    if re.match(r"^how\s+to\b", text, re.IGNORECASE):
        return False
    return bool(_QUESTION_START_RE.match(text))


def check_summary_information(rows):
    """(ok, problem): the composed body must explain what
    actually happened.

    A body that is entirely questions is rejected ("question_
    only"), as is a body whose sentences carry no fact at all -
    pure vague background or article-introduction text
    ("no_news_content").  A solar-mission story must state the
    finding, not ask why the corona is hot; the answer is never
    invented.
    """
    if not rows:
        return False, "no_news_content"
    if all(
        _is_question(row.get("text"))
        for row in rows
    ):
        return False, "question_only"
    if not any(
        _has_fact(extract_facts(row.get("text")))
        for row in rows
    ):
        return False, "no_news_content"
    return True, None


# ---------------------------------------------------------
# Leading-quote attribution
# ---------------------------------------------------------

# Role / organisation words that identify a speaker without a
# name: "...the mayor said." is attributed, "...he said." is
# not.
_ROLE_SPEAKERS = {
    "officials", "authorities", "police", "government", "court",
    "minister", "president", "spokesman", "spokeswoman",
    "spokesperson", "agency", "department", "ministry", "company",
    "club", "team", "mayor", "governor", "doctor", "scientists",
    "researchers", "analysts", "campaigners", "residents",
    "witnesses", "expert", "chief", "director", "manager",
    "leader", "teacher", "student", "lawyer", "judge", "coach",
    "captain", "boss", "staff", "workers", "member", "chair",
    "founder", "ceo", "general", "admiral", "professor",
    "lawmakers", "legislators", "ministers", "officer",
    "officers", "executive", "executives", "union", "church",
    "party", "campaign", "foundation", "charity", "group",
}

_QUOTE_OPEN_TO_CLOSE = {
    "\u201c": "\u201d",
    "\u2018": "\u2019",
    '"': '"',
    "\u00ab": "\u00bb",
}


def _starts_with_quote(text):
    text = str(text or "").strip()
    return bool(text) and text[0] in _QUOTE_OPEN_TO_CLOSE


def _identifies_speaker(text):
    """True when a leading-quote sentence names who is speaking:
    a reporting verb plus a name or role after the closing quote,
    or an "according to" attribution.  "...he said." does not
    identify the speaker and is rejected."""
    text = str(text or "").strip()
    if not text:
        return False
    open_ch = text[0]
    if open_ch not in _QUOTE_OPEN_TO_CLOSE:
        return False
    close_ch = _QUOTE_OPEN_TO_CLOSE[open_ch]
    close = text.find(close_ch, 1)
    if close < 0:
        return False
    after = text[close + 1:]
    if re.search(r"\baccording to\b", after, re.IGNORECASE):
        return True
    if not re.search(
        r"\b(?:" + "|".join(REPORTING_VERBS) + r")\b",
        after,
        re.IGNORECASE,
    ):
        return False
    if re.search(r"\b[A-Z][a-z]+\b", after):
        return True
    if any(
        re.search(r"\b" + role + r"\b", after, re.IGNORECASE)
        for role in _ROLE_SPEAKERS
    ):
        return True
    return False


_ATTRIBUTION_VERB_RE = re.compile(
    r"\b(?:" + "|".join(REPORTING_VERBS) + r")\b",
    re.IGNORECASE,
)

# First-person spoken fragments that carry no attribution of their
# own: "I feel sick." or "Look, we're working constructively..."
# are direct speech without a speaker.  The summarizer never
# invents one, so such rows are rejected unless the sentence itself
# names the speaker.
_FIRST_PERSON_SPOKEN_RE = re.compile(
    r"^(?:i\s+(?:feel|felt|think|thought|believe|believed|hope|"
    r"hoped|want|wanted|need|needed|saw|heard|know|knew|remember|"
    r"remembered|worry|worried|understand|understood|wish|wished|"
    r"am|was|have|had|can't|cannot|don't|didn't|won't|wouldn't)"
    r"|we\s+(?:are|were|have|had|want|wanted|need|needed|feel|felt|"
    r"think|thought|can't|cannot|don't|didn't|won't|wouldn't)"
    r"|(?:i'm|we're|i've|we've|i'd|we'd)\s+"
    r"|(?:look|well|frankly|honestly|listen|to be honest),?\s+"
    r"(?:i|we)\b)",
    re.IGNORECASE,
)


def _spoken_sentence_attributed(text):
    """True when a first-person spoken sentence itself names the
    speaker ("We are confident, the minister said." or "...",
    according to officials)."""
    text = str(text or "").strip()
    if re.search(r"\baccording to\b", text, re.IGNORECASE):
        return True
    for match in _ATTRIBUTION_VERB_RE.finditer(text):
        before = text[max(0, match.start() - 40): match.start()]
        after = text[match.end(): match.end() + 40]
        if any(
            re.search(r"\b" + role + r"\b", after, re.IGNORECASE)
            for role in _ROLE_SPEAKERS
        ):
            return True
        if re.search(r"\b[A-Z][a-z]+\b", after):
            return True
        if re.search(r"\b(?:the\s+)?[A-Z][a-z]+\b", before):
            return True
    return False


def check_leading_quotes(rows):
    """(ok, problem): the composed body must not contain an
    unattributed quotation.

    A sentence that opens with a quotation ("...", said ...) must
    identify who said it - inside the sentence or in the
    immediately adjacent sentence that carries the reporting verb.
    A first-person spoken fragment ("I feel sick.", "Look, we're
    working constructively...") must name its own speaker - no
    neighbor escape, because the speaker is never recoverable from
    a bare first-person statement.  The summarizer never invents
    a speaker: unattributed quotes reject the story.
    """
    if not rows:
        return True, None
    for index, row in enumerate(rows):
        text = (row.get("text") or "").strip()
        quoted = _starts_with_quote(text)
        spoken = bool(_FIRST_PERSON_SPOKEN_RE.match(text))
        if not quoted and not spoken:
            continue
        if _identifies_speaker(text) or _spoken_sentence_attributed(text):
            continue
        if quoted:
            attributed_by_neighbor = False
            for neighbor in (index - 1, index + 1):
                if 0 <= neighbor < len(rows):
                    if _ATTRIBUTION_VERB_RE.search(
                        rows[neighbor].get("text") or ""
                    ):
                        attributed_by_neighbor = True
                        break
            if attributed_by_neighbor:
                continue
        return False, "unattributed_quote"
    return True, None


def quality_check_sentence(text, headline):
    """Problems for one composed sentence.  Empty list means
    the sentence passes every check."""
    problems = []
    text = str(text or "").strip()

    if not text:
        return ["empty sentence"]

    if not re.search(r"[.!?\u2026][\"'\u201d\u2019)\]]*$", text):
        problems.append("sentence is not complete")

    if _dangling_ending(text):
        problems.append("dangling ending")

    if HTML_ENTITY_RE.search(text):
        problems.append("html entity artifact")

    if strip_boilerplate(text) != text:
        problems.append("boilerplate fragment")

    if is_filler(text):
        problems.append("filler sentence")

    if is_headline_paraphrase(text, headline):
        problems.append("headline repetition")

    if not _balanced_quotes(text):
        problems.append("unbalanced quotation marks")

    # A sentence that still fuses two sentences (a lowercase word
    # glued to a Capitalised sentence start) is extraction damage:
    # it must never be published.
    if _looks_fused(text):
        problems.append("fused sentence artifact")

    first = text[0]
    if (
        first.islower()
        and text[:1] not in "\"'(\u2018\u201c\u201d\u00ab\u00bb\u2026"
    ):
        problems.append("sentence starts lowercase")

    return problems


def quality_check_summary(headline, rows):
    """(ok, problems) for the whole composed summary.

    Problems reference the offending row index; rows are
    never mutated here."""
    problems = []
    if not rows:
        return (False, ["empty summary"])
    if len(rows) < MIN_SENTENCES:
        problems.append("fewer than 2 useful sentences")
    if len(rows) > MAX_SENTENCES:
        problems.append("more than %d sentences" % MAX_SENTENCES)
    if not (headline or "").strip():
        problems.append("missing headline")

    seen_keys = set()
    for index, row in enumerate(rows):
        text = row.get("text") or ""
        for problem in quality_check_sentence(text, headline):
            problems.append("sentence %d: %s" % (index + 1, problem))
        key = _normalized(text)
        if key in seen_keys:
            problems.append(
                "sentence %d: duplicate of an earlier sentence"
                % (index + 1)
            )
        seen_keys.add(key)
        if any(
            is_near_duplicate(
                {"text": text},
                {"text": other.get("text")},
            )
            for other in rows[:index]
        ):
            problems.append(
                "sentence %d: near-duplicate of an earlier "
                "sentence" % (index + 1)
            )

    return (not problems, problems)


# ---------------------------------------------------------
# Summary composition
# ---------------------------------------------------------


def _is_article_row(row, article_item_ids):
    item_id = row.get("item_id") or ""
    return item_id in article_item_ids or item_id.endswith(
        ARTICLE_ITEM_SUFFIX
    )


# Consequence verbs adjacent to an impact phrase, so
# "12 people were killed" and "12 people were injured" never
# count as the same consequence.
CONSEQUENCE_VERBS = {
    "killed", "injured", "hurt", "dead", "deaths",
    "fatalities", "displaced", "evacuated", "missing",
    "forced", "affected", "destroyed", "damaged",
}

QUANTIFIER_STRIP_RE = re.compile(
    r"^(?:more than|at least|nearly|about|around|over|"
    r"up to|almost|roughly)\s+",
    re.IGNORECASE,
)


def _impact_signature(text):
    """(impact phrase, consequence verb) pairs in a sentence.

    The phrase is the matched number+unit; the verb is the
    consequence word adjacent after the phrase ("evacuated"
    in "20,000 residents were evacuated"), or None."""
    out = set()
    for match in IMPACT_RE.finditer(text or ""):
        phrase = QUANTIFIER_STRIP_RE.sub(
            "", match.group(0)
        ).strip()
        verb = None
        after = text[match.end() : match.end() + 60]
        for candidate in CONSEQUENCE_VERBS:
            if re.search(r"\b" + candidate + r"\b", after):
                verb = candidate
                break
        out.add((phrase, verb))
    return out


def _fact_signature(row):
    """The consequential facts of a row: canonical numbers and
    normalized impact (phrase + consequence verb).  Two rows
    sharing both numbers and the same consequence report the
    same fact."""
    facts = extract_facts(row.get("text"))
    return (
        frozenset(facts["numbers"]),
        frozenset(_impact_signature(row.get("text"))),
    )


def _covers_signature(selected_rows, row):
    numbers, impact = _fact_signature(row)
    if not numbers or not impact:
        return False
    for selected in selected_rows:
        sel_numbers, sel_impact = _fact_signature(selected)
        if (
            numbers & sel_numbers
            and impact & sel_impact
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Narrative ordering
# ---------------------------------------------------------------------------


def order_narrative(rows):
    """Order selected rows into a natural news narrative.

    Buckets:
      lead        - a self-contained event statement (a consequence
                    with a place/time), or a plain factual statement;
      detail      - where / when / how-many sentences;
      consequence - impact sentences (killed, injured, evacuated,
                    displaced, ...).

    Leads come first (strongest-fact lead first), then detail in
    source order, then consequences.  When no standalone lead
    exists, the most fact-dense detail sentence opens the summary
    so the body never starts with an empty-feeling statement.
    """
    if len(rows) <= 1:
        return rows

    lead = []
    detail = []
    consequence = []

    for row in rows:
        facts = extract_facts(row.get("text"))
        has_impact = bool(facts["impact"])
        has_place_time = bool(
            facts["locations"] or facts["temporal"]
        )
        if has_impact and has_place_time:
            lead.append(row)
        elif has_impact:
            consequence.append(row)
        elif (
            facts["locations"]
            or facts["temporal"]
            or facts["numbers"]
        ):
            detail.append(row)
        else:
            lead.append(row)

    if not lead and detail:
        strongest = max(
            detail,
            key=lambda r: sentence_fact_score(
                extract_facts(r.get("text"))
            ),
        )
        lead = [strongest]
        detail = [r for r in detail if r is not strongest]

    if lead:
        lead = sorted(
            lead,
            key=lambda r: sentence_fact_score(
                extract_facts(r.get("text"))
            ),
            reverse=True,
        )

    return lead + detail + consequence


# ---------------------------------------------------------------------------
# Headline / body consistency
# ---------------------------------------------------------------------------

# Unambiguous win/loss verbs.  Bare "defeat" (noun) is ambiguous
# and deliberately excluded from both sets.
WIN_WORDS = {
    "won", "wins", "win", "winning", "defeated", "defeats",
    "beat", "beats", "victory", "victories", "triumph",
    "triumphed",
}

LOSE_WORDS = {
    "lost", "loses", "losing", "beaten",
}

KILL_WORDS = {
    "killed", "kills", "kill", "dead", "deaths", "fatalities",
    "died", "dies",
}

INJURE_WORDS = {
    "injured", "injures", "injure", "hurt", "wounded", "wounds",
}

# Strong claim -> weaker alternatives that contradict it.  A
# headline claiming the strong form is rejected when the source
# only supports the weak form.
MODALITY_PAIRS = [
    (
        {"confirmed", "confirms"},
        {"reportedly", "allegedly", "unconfirmed"},
    ),
    (
        {"arrested", "arrests", "arrest"},
        {"suspected", "suspects", "investigating", "investigates"},
    ),
    (
        {"announced", "announces"},
        {
            "proposed", "proposes", "considering", "considered",
            "considers", "planned", "plans", "planning", "mulled",
            "mulling", "weighing",
        },
    ),
    (
        {"approved", "approves"},
        {
            "considered", "considers", "considering", "proposed",
            "proposes", "weighed", "weighing",
        },
    ),
]

# Score-like patterns only: "3-1", "2-0", "5-4".  ISO dates and
# clock times ("2026-08-13", "11:30") are deliberately excluded.
_SCORE_RE = re.compile(r"\b\d{1,2}\s*[-\u2013\u2014]\s*\d{1,2}\b")


def _words_present(text, words):
    return any(
        re.search(r"\b" + re.escape(w) + r"\b", text, re.IGNORECASE)
        for w in words
    )


def _normalize_score(text):
    return re.sub(
        r"\s+", "", text
    ).replace("\u2013", "-").replace("\u2014", "-").lower()


# ---------------------------------------------------------
# Headline / body topic coherence
# ---------------------------------------------------------

# Common-noun families: a body may paraphrase the headline's key
# subject without repeating the exact word ("wildfire" -> "the
# fire"/"the blaze", "hurricane" -> "the storm").  Only used for
# comparing whether the body touches the headline's topic; never
# changes any rendered text.
_WORD_FAMILY = {
    "wildfire": "fire", "wildfires": "fire",
    "blaze": "fire", "blazes": "fire",
    "flame": "fire", "flames": "fire",
    "hurricane": "storm", "typhoon": "storm",
    "cyclone": "storm", "tornado": "storm",
    "blizzard": "storm", "monsoon": "storm",
}


def _coherence_stems(text):
    """Content-word stems of a headline/body, synonym- and
    word-family-normalized, for topic-overlap comparison only."""
    stems = set()
    for token in _content_tokens(text):
        token = PARAPHRASE_SYNONYMS.get(token, token)
        token = _WORD_FAMILY.get(token, token)
        stem = _stem_lite(token)
        if stem not in NON_FACT_WORDS:
            stems.add(stem)
    return stems


def _texts_link(a, b):
    """True when two texts share any meaningful fact link: a
    named entity, a place, an action, a number, a time reference
    or a content word."""
    ea = set(_entity_words(a))
    eb = set(_entity_words(b))
    if ea & eb:
        return True
    ea_aliased = {ENTITY_ALIASES.get(e, e) for e in ea}
    eb_aliased = {ENTITY_ALIASES.get(e, e) for e in eb}
    if ea_aliased & eb_aliased:
        return True
    if (ea & LOCATION_SET) & (eb & LOCATION_SET):
        return True
    if _actions(a) & _actions(b):
        return True
    if _numbers_of(a) & _numbers_of(b):
        return True
    if _temporal_of(a) & _temporal_of(b):
        return True
    sa = _coherence_stems(a)
    sb = _coherence_stems(b)
    if sa & sb:
        return True
    for x in sa:
        for y in sb:
            if (
                len(x) >= 4
                and len(y) >= 4
                and (x in y or y in x)
            ):
                return True
    return False


def check_headline_body_coherence(headline, rows):
    """(ok, problems): the composed body must actually explain
    the headline's main event.

    A multi-topic live-blog headline ("... - business live") can
    yield a body extracted from a DIFFERENT story in the same
    blog: the summary would post a headline about topic A above a
    body about topic B.  When the composed body shares NO
    meaningful link with the headline (entity, place, action,
    number, time reference or content word), the body does not
    explain the headline and the candidate is rejected - never
    repaired by guessing.
    """
    if not rows:
        return False, ["empty body"]
    if not (headline or "").strip():
        return True, []
    body_text = " ".join(
        (row.get("text") or "") for row in rows
    )
    if _texts_link(headline, body_text):
        return True, []
    return False, ["body does not explain the headline's main event"]


def check_headline_consistency(headline, source_text):
    """(ok, problems): the headline must not contradict the source.

    Catches the classic inconsistencies: a headline claiming a win
    where the source reports a loss, a headline score that never
    appears in the source, deaths where the source only reports
    injuries, and confirmed/arrested/announced headlines whose
    source only supports the weaker claim.  Contradictions reject
    the story rather than guessing.
    """
    problems = []
    headline = str(headline or "")
    source_text = str(source_text or "")
    if not headline:
        return True, problems

    normalized_source = _normalize_score(source_text)

    # 1. Score / result: a headline score ("3-1", "2-0") must
    #    appear somewhere in the source.
    for score in _SCORE_RE.findall(headline):
        if _normalize_score(score) not in normalized_source:
            problems.append(
                "headline score %s is not supported by the source"
                % score
            )

    # 2. Win/loss polarity.
    hl_win = _words_present(headline, WIN_WORDS)
    hl_lose = _words_present(headline, LOSE_WORDS)
    src_win = _words_present(source_text, WIN_WORDS)
    src_lose = _words_present(source_text, LOSE_WORDS)
    if hl_win and not src_win and src_lose:
        problems.append(
            "headline claims a win but the source reports a loss"
        )
    if hl_lose and not src_lose and src_win:
        problems.append(
            "headline reports a loss but the source reports a win"
        )

    # 3. Casualties: killed vs injured.
    hl_kill = _words_present(headline, KILL_WORDS)
    hl_injure = _words_present(headline, INJURE_WORDS)
    src_kill = _words_present(source_text, KILL_WORDS)
    src_injure = _words_present(source_text, INJURE_WORDS)
    if hl_kill and not src_kill and src_injure:
        problems.append(
            "headline reports deaths but the source reports "
            "only injuries"
        )
    if hl_injure and not src_injure and src_kill:
        problems.append(
            "headline reports injuries but the source reports "
            "only deaths"
        )

    # 4. Modality: strong vs weak claims.
    for strong, weak in MODALITY_PAIRS:
        if (
            _words_present(headline, strong)
            and not _words_present(source_text, strong)
            and _words_present(source_text, weak)
        ):
            problems.append(
                "headline claims %s but the source only supports "
                "a weaker claim" % min(strong)
            )

    return (not problems, problems)


def _select_pool(rows, tier1_max, cap):
    """Select up to `cap` rows from one source pool.

    Exact duplicates (same normalized text) are dropped.
    Fact-bearing sentences are ranked by fact score first
    (up to tier1_max), then the remaining informative
    sentences fill the pool in source order.  Returns rows in
    selection order."""
    seen = set()
    unique = []
    for row in rows:
        key = _normalized(row.get("text"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    accepted = []
    tier1 = []
    tier2 = []
    for row in unique:
        if _covers_signature(accepted, row):
            continue
        facts = extract_facts(row.get("text"))
        if _has_fact(facts):
            tier1.append(
                (sentence_fact_score(facts), row)
            )
        else:
            tier2.append(row)
    tier1.sort(key=lambda pair: pair[0], reverse=True)
    tier1 = [row for _, row in tier1]
    for row in tier1:
        if len(accepted) >= min(tier1_max, cap):
            break
        if _covers_signature(accepted, row):
            continue
        accepted.append(row)
    for row in tier2:
        if len(accepted) >= cap:
            break
        if _covers_signature(accepted, row):
            continue
        accepted.append(row)
    return accepted


def select_fact_rows(rows, article_item_ids, cfg=None):
    """Select the 2-4 fact-important sentences of the summary.

    Article-provenanced rows are the primary source and come
    first; the RSS rows fill any remaining slots.  Within
    each pool, fact-bearing sentences (impact, numbers,
    locations, entities, time, attribution) are ranked by
    fact score first, then the remaining informative
    sentences fill the pool in source order.  An RSS row that
    reports the same consequence (same numbers AND same
    impact unit) as an article row is dropped: the fact is
    already covered by the primary source."""
    tier1_max = int(
        (cfg or {}).get("tier1_max", TIER1_MAX)
    )
    max_sentences = int(
        (cfg or {}).get("max_sentences", MAX_SENTENCES)
    )
    max_sentences = min(max_sentences, HARD_MAX_SENTENCES)

    article_rows = [
        r for r in rows if _is_article_row(r, article_item_ids)
    ]
    other_rows = [
        r for r in rows if not _is_article_row(r, article_item_ids)
    ]

    selected = _select_pool(
        article_rows,
        tier1_max,
        max_sentences,
    )

    if len(selected) < max_sentences:
        other_rows = [
            r
            for r in other_rows
            if not _covers_signature(selected, r)
        ]
        selected.extend(
            _select_pool(
                other_rows,
                tier1_max,
                max_sentences - len(selected),
            )
        )

    return selected[:max_sentences]


def compose_summary(rows, article_item_ids, cfg=None):
    """Apply conservative rewrites to selected rows.

    Only attribution fronting is performed; every factual
    token remains verbatim from the source.  Returns rows
    with the composed "text"."""
    composed = []
    for row in rows:
        composed_row = dict(row)
        composed_row["text"] = front_attribution(row.get("text"))
        composed_row["composed"] = (
            composed_row["text"] != row.get("text")
        )
        composed.append(composed_row)
    return composed


def verify_summary(rows, source_text):
    """(kept_rows, problems): drop rows that do not verify
    against the source; every problem is recorded."""
    kept = []
    problems = []
    for index, row in enumerate(rows):
        verified, row_problems = verify_row(row, source_text)
        if verified:
            kept.append(row)
        else:
            problems.append(
                {
                    "index": index + 1,
                    "text": (row.get("text") or "")[:80],
                    "problems": row_problems,
                }
            )
    return kept, problems


def apply_quality_gate(headline, rows):
    """(kept_rows, problems): drop rows that fail the quality
    gate.  Returns the surviving rows and every problem."""
    kept = []
    problems = []
    for index, row in enumerate(rows):
        sentence_problems = quality_check_sentence(
            row.get("text"), headline
        )
        if sentence_problems:
            problems.append(
                {
                    "index": index + 1,
                    "text": (row.get("text") or "")[:80],
                    "problems": sentence_problems,
                }
            )
        else:
            kept.append(row)
    return kept, problems


def summarize_rows(
    rows,
    source_text,
    headline,
    article_item_ids=None,
    cfg=None,
):
    """Compose, verify, order, quality-check and consistency-
    check the 2-4 sentence summary for one story.

    Returns (rows, stats) where stats records every rejection
    and problem.  Returns (None, stats) when fewer than two
    genuinely useful source-supported sentences survive, or
    when the headline contradicts the source: the story must
    be rejected, never padded or invented."""
    cfg = cfg or {}
    min_sentences = int(
        cfg.get("min_sentences", MIN_SENTENCES)
    )
    max_sentences = int(
        cfg.get("max_sentences", MAX_SENTENCES)
    )
    max_sentences = min(max_sentences, HARD_MAX_SENTENCES)
    article_item_ids = article_item_ids or set()

    stats = {
        "selected": 0,
        "composed": 0,
        "verify_problems": [],
        "quality_problems": [],
        "coherence_problems": [],
        "info_problem": None,
        "quote_problem": None,
        "consistency_problems": [],
        "rejected": None,
    }

    if not rows:
        stats["rejected"] = "insufficient_information"
        return None, stats

    # 1. Quote-boundary sentence splitting: peel a sentence that
    #    follows a closing quote (". " The IMF said ...) into its
    #    own row so its fact survives independently. See
    #    _split_quote_boundaries for why this lives here.
    rows = _split_quote_boundaries(rows, headline)

    # 1.5. Fused-sentence repair: reconstruct rows damaged by
    #    HTML/line-break extraction ("...centre Orangutans
    #    rescued...") when the split is safe, drop the corrupted
    #    sentence otherwise.  See _repair_fused_rows.
    rows = _repair_fused_rows(rows, headline, source_text)

    # 2. Live-blog relevance: drop paragraphs that are clearly a
    #    separate story (topic-shift marker or an unrelated proper
    #   name) and share no fact with the headline.
    rows = _drop_unrelated_rows(rows, headline)

    selected = select_fact_rows(rows, article_item_ids, cfg)

    if len(selected) < min_sentences:
        stats["selected"] = len(selected)
        stats["rejected"] = "insufficient_information"
        return None, stats

    stats["selected"] = len(selected)

    composed = compose_summary(selected, article_item_ids, cfg)
    stats["composed"] = sum(
        1 for row in composed if row.get("composed")
    )

    kept, verify_problems = verify_summary(
        composed, source_text
    )
    stats["verify_problems"] = verify_problems

    if len(kept) < min_sentences:
        stats["rejected"] = "verification"
        return None, stats

    kept, quality_problems = apply_quality_gate(
        headline, kept
    )
    stats["quality_problems"] = quality_problems

    if len(kept) < min_sentences:
        stats["rejected"] = "quality"
        return None, stats

    if len(kept) > max_sentences:
        kept = kept[:max_sentences]

    # 7. Narrative ordering: what happened, then where/when/
    #    how-many detail, then the consequence or current status.
    kept = order_narrative(kept)

    # 8. Headline/body topic coherence: the composed body must
    #    actually explain the headline's main event.  A
    #    multi-topic live-blog headline whose body belongs to a
    #    different story in the blog is rejected, never repaired.
    coherent, coherence_problems = check_headline_body_coherence(
        headline, kept
    )
    stats["coherence_problems"] = coherence_problems
    if not coherent:
        stats["rejected"] = "coherence"
        return None, stats

    # 9. Information gate: the body must answer "what actually
    #    happened".  Question-only or fact-free vague summaries
    #    are rejected; the answer is never invented.
    info_ok, info_problem = check_summary_information(kept)
    stats["info_problem"] = info_problem
    if not info_ok:
        stats["rejected"] = info_problem
        return None, stats

    # 9.5. Leading-quote attribution: a quoted sentence must name
    #    who said it (in-sentence or in the adjacent sentence).
    #    "I feel sick." with no attribution rejects the story.
    quote_ok, quote_problem = check_leading_quotes(kept)
    stats["quote_problem"] = quote_problem
    if not quote_ok:
        stats["rejected"] = quote_problem
        return None, stats

    # 10. Headline/body consistency: a headline that claims a
    #    score, a victory, deaths or a confirmed development
    #    must agree with the source.  Contradiction rejects.
    consistent, consistency_problems = check_headline_consistency(
        headline, source_text
    )
    stats["consistency_problems"] = consistency_problems
    if not consistent:
        stats["rejected"] = "consistency"
        return None, stats

    return kept, stats
