"""Editorial eligibility filter for the news pipeline.

Rejects content whose primary purpose is not current news: product
reviews, buying guides, opinion columns, evergreen how-tos, sponsored
and promotional material, listicles, quizzes, recipes and similar
junk.  It does NOT reject science, sports, technology, culture or
environment content as such — only clearly non-news formats.

The filter is deliberately conservative: a story is rejected only on
a strong, specific signal (a buying-guide phrase, a first-person
opinion marker, a "review" paired with a consumer product, sponsored
content, ...).  A single ambiguous word (e.g. "review" alone, "best"
alone) is never enough.
"""
import re

# Consumer-product / media nouns. A "review" is rejected only when it
# is paired with one of these; "court to review the ruling" is news.
PRODUCT_WORDS = (
    "air conditioner", "air-conditioner", "aircon", "vacuum",
    "robot vacuum", "doorbell", "e-bike", "ebike", "scooter",
    "smartphone", "phone", "iphone", "galaxy", "pixel", "tablet",
    "ipad", "laptop", "macbook", "chromebook", "pc", "desktop",
    "monitor", "keyboard", "mouse", "headphones", "headphone",
    "earbuds", "speaker", "soundbar", "tv", "television", "projector",
    "camera", "mirrorless", "dslr", "gopro", "drone", "watch",
    "smartwatch", "fitness tracker", "console", "playstation",
    "xbox", "nintendo", "switch", "gpu", "graphics card", "cpu",
    "ssd", "hard drive", "router", "modem", "charger", "power bank",
    "game", "video game", "movie", "film", "album", "book", "novel",
    "restaurant", "hotel", "resort", "app", "gadget", "gadgets",
    "car", "suv", "ev", "electric vehicle", "bike", "kettle",
    "toaster", "microwave", "oven", "air fryer", "blender",
    "coffee maker", "fridge", "refrigerator", "washer", "dryer",
    "mattress", "chair", "desk", "sofa", "grill", "smoker",
    "lawn mower", "snow blower", "generator", "stroller", "car seat",
    "tent", "backpack", "luggage", "suitcase", "sunglasses",
    "sneakers", "running shoes", "jacket", "boots",
)

_PRODUCT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in PRODUCT_WORDS) + r")\b",
    re.IGNORECASE,
)

# Review forms: "Review: X", "X review", "our verdict", "hands-on",
# "unboxing", "we tested/tried", "tested:".  The strong forms
# ("Review:", "hands-on", "our verdict", "we tested") reject without
# a product word; the bare "review" word only rejects next to a
# consumer product ("court to review the ruling" stays news).
STRONG_REVIEW_PATTERNS = [
    r"\breview\s*[:|]\s*",
    r"\bhands?-on\b",
    r"\bunboxing\b",
    r"\bour\s+verdict\b",
    r"\bverdict\s*[:|]\s*",
    r"\bwe\s+(?:tested|tried|reviewed)\b",
    r"\btested\s*[:|]\s*",
    r"\b(?:first\s+)?(?:look|thoughts)\s*:\s*",
    r"\bvs\.?\s+[\w-]+\s+review\b",
]

REVIEW_PATTERNS = STRONG_REVIEW_PATTERNS + [
    r"\breview\b",
]

# Opinion / commentary / first-person markers.
OPINION_PATTERNS = [
    r"^opinion\b",
    r"\bopinion\s*[:|]\s*",
    r"\bopinion\s+(?:piece|column|essay)\b",
    r"\bcommentary\s*[:|]\s*",
    r"\bcommentary\s+(?:piece|column)\b",
    r"\b(?:my|our)\s+(?:take|view)\s+(?:on|about)\b",
    r"\bwhy\s+i\s+(?:bought|tried|tested|quit|started|switched|gave up)\b",
    r"\bi\s+(?:tried|tested|bought|reviewed|visited|spent|quit|gave\s+up)\s+",
    r"\bcolumn\s*:\s*",
    r"\bessay\b",
    r"\bthe\s+guardian\s+view\b",
    r"\beditorial\s*[:|]\s*",
    # Letter formats: "... | Letter", "Letter: ...",
    # "Readers' letters", "Letters to the editor", "Letters"
    r"\|\s*letter\b",
    r"\bletter\s*[:|]\s*",
    r"\b(?:reader|readers['\u2019]?|your)\s+letters?\b",
    r"\bletters?\s+(?:to\s+the\s+editor|page|from\s+our\s+readers)\b",
    r"^letters?\b",
    r"\bletters?\s+editor\b",
    # Other publisher-specific opinion formats.
    r"\bop[- ]ed\b",
    r"\bguest\s+(?:essay|column|opinion|commentary)\b",
    r"\bthe\s+editorial\s+board\b",
]

# How-to / evergreen advice / tips / listicle markers.  "How to
# watch the eclipse" is NOT rejected: the rejected verbs are the
# advice/shopping ones, not event-coverage ones.
HOWTO_PATTERNS = [
    r"\bhow\s+to\s+(?:choose|buy|save|make|fix|build|install|clean|"
    r"organize|cook|bake|lose|gain|start|grow|earn|pick|select|"
    r"compare|upgrade|replace|repair|remove|prevent|avoid|budget|"
    r"pack|plan|find|get|use|set\s+up|cancel)\b",
    r"\btips?\s+for\b",
    r"\b(?:top\s+\d+|5|10)\s+(?:best|tips|ways|reasons|things)\b",
    r"\b(?:best|cheapest|top)\s+[\w-]+\s+(?:to\s+buy|of\s+\d{4}|"
    r"for\s+\d{4}|under\s+\$?\d+|on\s+a\s+budget)\b",
    r"\bbuying\s+guide\b",
    r"\bprice\s+comparison\b",
    r"\bwhere\s+to\s+buy\b",
    # "Buy Now Pay Later" is a financial/industry term, not a
    # shopping call-to-action: the generic "buy now" pattern is
    # narrowed with a lookahead so BNPL coverage stays news.
    r"\bbuy\s+now\b(?![\s,]*(?:pay|payment|bnpl|and\s+pay))",
    r"\bshop\s+now\b",
    r"\b(?:great|best|big|hot)\s+(?:deals?|offers?)\b",
    r"\bdiscount\s+code\b",
    r"\bcoupons?\b",
    r"\bquiz\b",
    r"\bhoroscope\b",
    r"\brecipe\b",
    r"\b(?:10|7|5|3)\s+ways\s+to\b",
    r"\b(?:best|top|cheapest)\s+[\w-]+(?:(?:\s+[\w-]+){0,3})\s+"
    r"(?:of\s+\d{4}|for\s+\d{4})\b",
    r"\beverything\s+you\s+need\s+to\s+(?:buy|choose|know\s+before)\b",
    r"\bthings?\s+to\s+(?:do|buy|see|know)\s+(?:this\s+)?(?:weekend|week|year)\b",
]

# Sponsored / promotional / affiliate content.
SPONSORED_PATTERNS = [
    r"\bsponsored\b",
    r"\badvertorial\b",
    r"\bpartner\s+content\b",
    r"\bpromoted\b",
    r"\bpaid\s+(?:content|post|partnership|promotion)\b",
    r"\baffiliate\s+(?:links?|content)\b",
    r"\bin\s+partnership\s+with\b",
    r"\bin\s+association\s+with\b",
    r"\bproudly\s+(?:sponsored|presented)\b",
]

# Routine weather / lifestyle signals that need an extreme-event or
# major-development override.
ROUTINE_WEATHER_RE = re.compile(
    r"\bweather\s+(?:forecast|outlook|this\s+week(?:end)?|tonight|"
    r"tomorrow)\b",
    re.IGNORECASE,
)

# Narrowly scoped current-event override: a major current
# astronomical event (eclipse, meteor shower, comet, aurora,
# supermoon, planetary alignment).  "Tips for watching the solar
# eclipse" or "Eclipse weather forecast" is legitimate news
# coverage of that event, not evergreen how-to/lifestyle junk.
MAJOR_ASTRONOMY_EVENT_RE = re.compile(
    r"\b(?:solar|total|partial|annular|annual|lunar|blood)?"
    r"\s*eclipse\b"
    r"|\bmeteor\s+showers?\b"
    r"|\b(?:meteor|meteors)\b"
    r"|\b(?:comet|aurora|auroras|northern\s+lights|aurora\s+borealis)\b"
    r"|\bplanetary\s+alignment\b"
    r"|\bsuper\s*moon\b",
    re.IGNORECASE,
)

EXTREME_WEATHER_WORDS = (
    "hurricane", "typhoon", "cyclone", "tornado", "blizzard",
    "flood", "flooding", "heatwave", "heat wave", "wildfire",
    "thunderstorm", "monsoon", "drought", "avalanche", "landslide",
    "earthquake", "tsunami", "snowstorm", "ice storm", "gale",
    "storm warning", "state of emergency",
)

_EXTREME_WEATHER_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(w) for w in EXTREME_WEATHER_WORDS
    ) + r")\b",
    re.IGNORECASE,
)

LIFESTYLE_PATTERNS = [
    r"\blifestyle\s+(?:news|tips?|advice)\b",
    r"\bself-care\b",
    r"\bwellness\s+(?:tips?|routine|trends?)\b",
    # Merely mentioning a dating/social app ("Bumble ditches its
    # women-first chat rule") is company/technology news, not
    # lifestyle: only explicit dating advice/tips are lifestyle.
    r"\bdating\s+(?:tips?|advice)\b",
    r"\bcelebrity\s+(?:gossip|style|fashion|net\s+worth)\b",
    r"\btravel\s+guide\b",
    r"\bbest\s+(?:hotels?|restaurants?|places?)\s+to\b",
    r"\b(?:get|stay)\s+fit\b",
    r"\bskin\s+care\s+routine\b",
]

_COMPILED = [
    (name, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, patterns in (
        ("review", REVIEW_PATTERNS),
        ("opinion", OPINION_PATTERNS),
        ("howto", HOWTO_PATTERNS),
        ("sponsored", SPONSORED_PATTERNS),
        ("lifestyle", LIFESTYLE_PATTERNS),
    )
]

_STRONG_REVIEW_COMPILED = [
    re.compile(p, re.IGNORECASE) for p in STRONG_REVIEW_PATTERNS
]

# ---------------------------------------------------------------------------
# Low-value feature / analysis formats
#
# Narrow headline-format rules for content that is not genuinely
# current news, even though it is not "junk" in the review/opinion
# sense: transfer gossip and speculation, "Why X matters" analysis,
# profile pieces, commemorations, generic explainers, soft
# poll/survey consumer-data stories and "how X are coping" features.
#
# Deliberately conservative - every pattern is a specific format
# phrase, applied to the headline (and to the body only for the
# commemoration forms, which are unambiguous):
#
# - legitimate financial news ("US borrowing costs rise...")
# - science discoveries ("Hidden moth population found...")
# - technology developments ("Flock adds safeguards...")
# - elections ("Nigeria Farage defeats Count Binface...")
# - environmental events ("Record rainfall leaves four dead...")
# - disasters, major sports news and major corporate news
#
# all stay eligible.  A completed transfer ("X signs for Y") is a
# done deal, not gossip, and stays eligible.
# ---------------------------------------------------------------------------

FEATURE_PATTERNS = [
    # Transfer gossip / speculation (not completed transfers).
    r"\btransfer\s+(?:gossip|rumou?rs?|speculation|round[- ]?up)\b",
    r"\b(?:closes? in on|closing in on)\b.{0,40}?\b(?:transfer|move|switch)\b",
    r"\b(?:set|poised|tipped|touted)\s+to\s+(?:join|leave)\s+"
    r"(?!(?:nato|eu|un)\b)[A-Z][a-z][A-Za-z\u2019'-]*(?:"
    r"\s+[A-Z][a-z][A-Za-z\u2019'-]*)?\b",
    r"\bon\s+the\s+verge\s+of\s+(?:a\s+)?(?:move|transfer|switch|joining|leaving)\b",
    r"\b(?:make|makes|making|pursue|pursues|pursuing)\s+a\s+move\s+for\b",
    r"\binterested\s+in\s+signing\b",
    r"\bwants?\s+to\s+leave\s+(?:the\s+)?[A-Z][a-z][A-Za-z\u2019'-]*(?:"
    r"\s+[A-Z][a-z][A-Za-z\u2019'-]*)?\b",
    # "Why X matters" / "What would X mean" analysis.
    r"\bwhy\s+[^.!?\n]{1,60}?\bmatters\b",
    r"\bwhat\s+would\b.{0,60}?\bmean\b",
    r"\bwhy\s+are\s+we\b",
    # Profile pieces: the imperative "Meet the ..." headline form
    # at the START of the title.  "Lawyers to meet federal
    # prosecutors" (a subject + verb) is news and never matches.
    r"^meet\s+(?:the\s+)?[A-Za-z\u2019'-]+\b",
    r"\bprofile\s*[:|]\s*",
    # Generic explainers.
    r"\bexplainer\b",
    r"\bwhat\s+to\s+know\b",
    r"\b(?:everything|things?)\s+you\s+need\s+to\s+know\b",
    r"\bhow\s+it\s+works\b",
    r"\bexplained\s*[:|]\s*",
    r"\bguide\s*[:|]\s*",
    r"\bin\s+charts\b",
    r"\bhow\s+(?:[A-Za-z\u2019'-]+\s+){1,4}(?:are|is)\s+"
    r"(?:tackling|coping|adapting|dealing|navigating)\b",
    # Soft consumer-data stories (polls / surveys).
    r"\b(?:poll|survey)\s+(?:finds?|shows?|reveals?|suggests?)\b",
    # Question-only explainer headlines: "What is a 'ceasefire'
    # supposed to achieve?", "Why is the Sun's corona millions of
    # degrees hotter...?".  The whole headline is a question with
    # no asserted event - the explainer format.  Hard-news
    # headlines assert; they do not end in "?".
    r"^\s*(?:what\s+(?:is|are|does|do)|why\s+(?:is|are|do|does|did)|"
    r"how\s+(?:do|does|is|are|did)|who\s+is)\b[^?!\n]*\?\s*$",
    # Profile pieces built on an emotional transformation: "The
    # Gambian women turning grief into song" ("...they have
    # transformed their experience of child loss through a
    # tradition of music").  A group channelling an emotion into
    # an art form is a feature, not a current event.
    r"\b(?:turn(?:ing|s|ed)?|transform(?:ing|s|ed)?|channel(?:ing|s|ed)?)"
    r"\s+(?:grief|loss|pain|trauma|heartbreak|tears?|sorrow)"
    r"\s+into\b",
    # Analysis / argumentative headline formats: "Why X is...",
    # "Can X save...", "X cannot save...".  Anchored at the
    # headline START (or on the explicit "cannot save" form) so
    # legitimate reporting that merely contains "why" mid-
    # sentence stays eligible.
    r"^why\s+(?:is|are|was|were|do|does|did|will|would|can|could|should)\b",
    r"^why\s+(?:the|a|an|[A-Za-z\u2019'-]+)\s+[A-Za-z\u2019'-]+\s+(?:is|are|was|were)\b",
    r"\b(?:can|could)\s+[A-Za-z\u2019'-]+\s+save\b",
    r"\b(?:cannot|can't|can\s+not)\s+(?:save|revive|rescue|fix)\b",
]

# Commemoration forms are unambiguous enough to check the body
# text too ("Cuba on Thursday marked the 100th anniversary of the
# birth of Fidel Castro").
FEATURE_BODY_PATTERNS = [
    r"\b(?:marks?|marked|marking|commemorates?|commemorated)\s+"
    r"(?:the\s+)?\d+(?:st|nd|rd|th)?\s+(?:anniversary|birthday|death)\b",
    r"\b(?:marks?|marked)\s+\d+\s+years\s+since\b",
]

_FEATURE_COMPILED = [
    re.compile(p, re.IGNORECASE) for p in FEATURE_PATTERNS
]

_FEATURE_BODY_COMPILED = [
    re.compile(p, re.IGNORECASE) for p in FEATURE_BODY_PATTERNS
]

# Anniversary / retrospective framing.  These are low-value
# commemorations ONLY when they report no current development
# (handled in editorial_eligibility); a genuine development that
# happens to reference an anniversary stays eligible.
ANNIVERSARY_RE = re.compile(
    r"\b(?:a|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"several|few|multiple|dozen)\s+"
    r"(?:years?|months?|weeks?|decades?)\s+after\b"
    r"|\b(?:marks?|marked|marking|commemorates?|commemorated)\s+"
    r"(?:the\s+)?(?:anniversary|centenary|centennial|birthday|death)\b"
    r"|\b(?:anniversary|retrospective|commemoration)\b",
    re.IGNORECASE,
)

# Current-development signals that override the anniversary rule:
# present-tense reporting verbs ("releases new findings",
# "announces", "says"), imminent-action forms ("is set to"), or
# a concrete current number ("5,000 still displaced").  A pure
# commemoration has none of these.
CURRENT_DEVELOPMENT_RE = re.compile(
    r"\b(?:releases?|announces?|issues?|reveals?|unveils?|"
    r"reports?|warns?|declares?|signs?|approves?|launches?|"
    r"opens?|closes?|finds?|returns?|wins?|strikes?|hits?|"
    r"kills?|raises?|cuts?|bans?|imposes?|arrests?|rescues?|"
    r"evacuates?|confirms?|rules?|sues?|fines?|charges?|"
    r"indicts?|convicts?|sentences?|rejects?|votes?|elects?|"
    r"says?|said)\b"
    r"|\b(?:is|are|has|have)\s+(?:set to|expected to|about to)\b"
    r"|\b\d+(?:[.,]\d+)?\s*(?:dead|killed|injured|percent|%|"
    r"million|billion|km|miles|people|arrested|displaced|"
    r"evacuated|missing)\b",
    re.IGNORECASE,
)


def _title(text):
    return (text or "").strip()


def editorial_eligibility(item, reasons_out=None):
    """Return True when the item is eligible current news.

    `item` must be a pipeline story dict with at least a title and a
    summary.  Returns False when the primary purpose of the item is
    clearly not current news.  When `reasons_out` is a list, the
    rejection reasons are appended to it.
    """
    title = _title(item.get("title"))
    summary = (item.get("summary") or "")
    combined = title + " " + summary

    if not title:
        return True

    lower = title.lower()

    # Review / opinion / how-to / sponsored / lifestyle markers.
    for name, patterns in _COMPILED:
        hit = any(p.search(title) for p in patterns) or any(
            p.search(combined) for p in patterns
        )
        if hit and name == "review":
            # Strong forms ("Review:", "hands-on", "our verdict",
            # "we tested") reject without a product.  A bare
            # "review" word needs a consumer product next to it,
            # so "court to review the ruling" stays news.
            strong_hit = any(
                p.search(title) or p.search(combined)
                for p in _STRONG_REVIEW_COMPILED
            )
            if not strong_hit and not _PRODUCT_RE.search(combined):
                continue
        if hit:
            # Narrowly scoped current-event override: how-to or
            # lifestyle phrasing attached to a major current
            # astronomical event is current news, not evergreen
            # junk.  The general filter is otherwise untouched.
            if (
                name in ("howto", "lifestyle")
                and MAJOR_ASTRONOMY_EVENT_RE.search(combined)
            ):
                continue
            if reasons_out is not None:
                reasons_out.append(name)
            return False

    # Routine weather coverage without an extreme event (an
    # eclipse weather forecast is not routine weather).
    if ROUTINE_WEATHER_RE.search(lower):
        if not _EXTREME_WEATHER_RE.search(combined):
            if MAJOR_ASTRONOMY_EVENT_RE.search(combined):
                pass
            else:
                if reasons_out is not None:
                    reasons_out.append("routine_weather")
                return False

    # Low-value feature / analysis headline formats.  Deliberately
    # narrower than the junk categories above: only specific format
    # phrases in the headline reject (plus unambiguous commemoration
    # phrasing in the body).  "If uncertain, keep the story" -
    # nothing here fires on a single ambiguous word.
    if any(p.search(title) for p in _FEATURE_COMPILED):
        if reasons_out is not None:
            reasons_out.append("feature")
        return False
    if any(
        p.search(title) or p.search(combined)
        for p in _FEATURE_BODY_COMPILED
    ):
        if reasons_out is not None:
            reasons_out.append("feature")
        return False

    # Anniversary / retrospective framing ("A year after the
    # protests...", "marks the anniversary of...", "retrospective").
    # Rejected only when the piece reports NO current development:
    # "One year after the disaster, regulators release new
    # findings" is current news and stays eligible, while a pure
    # commemoration or retrospective is a low-value feature.
    if ANNIVERSARY_RE.search(title):
        if not CURRENT_DEVELOPMENT_RE.search(combined):
            if reasons_out is not None:
                reasons_out.append("anniversary")
            return False

    return True


def is_editorial_junk(item):
    """Shortcut: True when the item should be filtered out."""
    return not editorial_eligibility(item)
