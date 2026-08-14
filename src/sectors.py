"""Sector taxonomy for coverage auditing.

The taxonomy is a two-level tree: a small set of top-level sectors
(geopolitics, economy, companies, technology, health, science,
climate, agriculture, transport, infrastructure, law, sports,
culture) each containing focused sub-sectors.  It is used by the
coverage audit tool to classify every collected article into a
sector, so coverage can be measured per sector and weak sectors can
be identified.

This module is pure data + classification.  It does NOT gate
publishing: the editorial filter and the priority/scoring system are
unchanged.  No hard sector quotas exist anywhere.

Sub-sector keyword lists are deliberately word-boundary matched, and
terms were chosen to separate genuine sub-topics (e.g. "oil" belongs
to energy/oil-gas while "stock" belongs to markets).
"""

import re

# Top-level sector -> sub-sectors.
SECTOR_TREE = {
    "geopolitics": [
        "world", "politics", "elections", "diplomacy",
        "conflict", "defense",
    ],
    "economy": [
        "economy", "finance", "banking", "markets",
        "currencies", "commodities", "trade",
    ],
    "companies": [
        "companies", "industry", "manufacturing", "energy",
        "oil_gas", "electricity", "nuclear", "renewables",
    ],
    "technology": [
        "technology", "ai", "semiconductors", "software",
        "hardware", "cloud", "cybersecurity", "telecom", "internet",
    ],
    "health": [
        "health", "medicine", "pharma", "biotech",
        "public_health", "outbreaks",
    ],
    "science": [
        "science", "space", "astronomy", "physics",
        "biology", "chemistry", "earth_science",
    ],
    "climate": [
        "climate", "weather", "floods", "storms",
        "earthquakes", "volcanoes", "tsunamis", "wildfires",
        "disasters",
    ],
    "agriculture": [
        "agriculture", "food", "water", "fisheries",
        "animal_health",
    ],
    "transport": [
        "transport", "aviation", "shipping", "rail",
        "automotive", "logistics", "supply_chains",
    ],
    "infrastructure": [
        "infrastructure", "power_grids", "dams", "roads",
        "bridges", "construction",
    ],
    "law": [
        "law", "courts", "regulation", "corruption",
        "human_rights", "labor", "employment", "education",
    ],
    "sports": ["sports"],
    "culture": ["culture", "entertainment"],
}

# Sub-sector -> identifying terms (word-boundary matched).
SECTOR_TERMS = {
    "world": ["geopolitic", "superpower", "sphere of influence",
              "world order", "great power", "cold war", "dispute",
              "disputed", "g7", "g20", "hegemon", "sancion"],
    "politics": ["president", "prime minister", "government",
                 "parliament", "senate", "congress", "minister",
                 "vote", "coalition", "legislation", "bill",
                 "policy", "candidate", "party", "political"],
    "elections": ["election", "electoral", "ballot", "voters",
                  "runoff", "referendum", "polling day", "snap election"],
    "diplomacy": ["diplomatic", "diplomacy", "ambassador", "summit",
                  "treaty", "embassy", "negotiation", "relations between",
                  "normalis", "normaliz", "visit", "peace accord",
                  "peace deal", "peace talks", "peace process"],
    "conflict": ["war", "airstrike", "missile", "invasion", "ceasefire",
                 "coup", "troops", "battle", "offensive", "shelling",
                 "artillery", "drone strike", "combat", "insurgent",
                 "militia", "bombing", "siege", "guerrilla", "insurgency"],
    "defense": ["defense", "defence", "army", "navy", "air force",
                "military budget", "weapons", "arms deal", "nuclear weapon",
                "missile defense", "military exercise", "nato", "arsenal",
                "drone", "spy", "espionage", "fighter jet", "gunship",
                "warplane", "intelligence agency", "military drills"],
    "economy": ["economy", "economic", "gdp", "recession", "inflation",
                "unemployment", "fiscal", "stimulus", "austerity",
                "monetary", "economic growth", "economic crisis"],
    "finance": ["finance", "financial", "lending", "credit", "debt",
                "mortgage", "loan", "default", "sovereign debt",
                "financial crisis", "insurance", "wealth fund",
                "sovereign fund", "investor", "asset"],
    "banking": ["bank", "banking", "central bank", "fed", "ecb",
                "reserve", "interest rate", "rate hike", "liquidity",
                "deposit", "lender"],
    "markets": ["stock", "stocks", "shares", "bonds", "yields",
                "equities", "wall street", "nasdaq", "s&p",
                "ftse", "nikkei", "index", "rally", "sell-off",
                "stock market"],
    "currencies": ["currency", "currencies", "dollar", "euro", "yen",
                   "pound", "yuan", "rupee", "exchange rate", "forex",
                   "devaluation", "peg"],
    "commodities": ["commodit", "oil price", "gold price", "silver",
                    "copper", "wheat", "crude", "brent", "iron ore",
                    "natural gas price", "mining", "gold", "mineral",
                    "rare earth"],
    "trade": ["trade", "tariff", "import", "export", "customs", "wto",
              "trade deal", "trade war", "sanctions", "quota",
              "trade agreement", "trade surplus", "export controls",
              "trade barriers"],
    "companies": ["company", "companies", "corporate", "firm", "ceo",
                  "merger", "acquisition", "takeover", "earnings",
                  "profit", "layoffs", "ipo", "shareholder"],
    "industry": ["industry", "industrial", "factory", "plant",
                 "production", "output"],
    "manufacturing": ["manufactur", "factory", "automaker", "steel",
                      "assembly", "shipbuilding"],
    "energy": ["energy", "power", "electricity", "grid", "utility",
               "energy crisis", "energy policy", "energy price"],
    "oil_gas": ["oil", "gas", "petroleum", "opec", "crude", "brent",
                "refinery", "pipeline", "lng", "fracking", "drilling"],
    "electricity": ["electricity", "power grid", "blackout", "power cut",
                    "electricity price", "energy market"],
    "nuclear": ["nuclear power", "nuclear plant", "nuclear reactor",
                "iaea", "nuclear energy", "atomic"],
    "renewables": ["renewable", "solar", "wind power", "wind farm",
                   "green energy", "clean energy", "battery storage",
                   "hydrogen", "green transition"],
    "technology": ["technology", "tech", "startup", "digital", "robot",
                    "automation", "software"],
    "ai": ["ai", "artificial intelligence", "machine learning", "llm",
           "chatbot", "generative", "openai", "anthropic", "deepmind",
           "large language model", "neural"],
    "semiconductors": ["semiconductor", "chip", "chips", "foundry",
                       "tsmc", "intel", "nvidia", "amd", "microchip", "fab"],
    "software": ["software", "app", "operating system", "open source",
                 "update", "developer"],
    "hardware": ["hardware", "device", "gadget", "laptop", "smartphone",
                 "server", "pc", "console"],
    "cloud": ["cloud", "aws", "azure", "google cloud", "data center",
              "datacenter"],
    "cybersecurity": ["cyber", "hack", "breach", "ransomware", "malware",
                      "phishing", "cisa", "vulnerability", "exploit",
                      "zero-day", "cve", "cyberattack", "cyber attack"],
    "telecom": ["telecom", "5g", "6g", "mobile network", "telecommunications",
                "broadband", "fiber", "fibre"],
    "internet": ["internet", "online", "web", "social media", "platform",
                 "streaming"],
    "health": ["health", "hospital", "medical", "healthcare", "nhs",
               "doctor", "patient"],
    "medicine": ["medicine", "drug", "treatment", "therapy", "clinical trial",
                 "drug approval", "fda", "plasma", "blood donation"],
    "pharma": ["pharmaceutical", "pharma", "drugmaker", "vaccine",
               "drug price"],
    "biotech": ["biotech", "biotechnology", "gene", "genetic", "crispr",
                "cell therapy", "mrna"],
    "public_health": ["public health", "who", "cdc", "health officials",
                      "health warning", "screening", "health service"],
    "outbreaks": ["outbreak", "epidemic", "pandemic", "virus",
                  "infectious", "disease", "measles", "cholera", "ebola",
                  "covid", "mpox", "h5n1", "bird flu", "polio"],
    "science": ["science", "research", "study", "scientist", "discovery",
                "journal", "findings"],
    "space": ["space", "nasa", "esa", "jaxa", "isro", "rocket", "satellite",
              "astronaut", "orbit", "launch", "spacecraft", "moon", "mars",
              "space station"],
    "astronomy": ["astronomy", "telescope", "galaxy", "planet", "comet",
                  "asteroid", "eclipse", "black hole", "exoplanet",
                  "nebula", "supernova", "constellation", "interstellar"],
    "physics": ["physics", "particle", "quantum", "cern", "atom",
                "fusion", "accelerator"],
    "biology": ["biology", "species", "dna", "evolution", "ecosystem",
                "genome", "wildlife", "biodiversity", "endangered",
                "vulture", "moth", "bear"],
    "chemistry": ["chemistry", "chemical", "molecule", "compound",
                  "reaction", "material"],
    "earth_science": ["geology", "geological", "earth science", "tectonic",
                      "fossil", "mineral", "geophysics"],
    "climate": ["climate", "climate change", "global warming", "emissions",
                "carbon", "co2", "paris agreement", "cop", "net zero"],
    "weather": ["weather", "forecast", "temperature", "heatwave",
                "heat wave", "cold snap", "frost"],
    "floods": ["flood", "flooding", "inundat"],
    "storms": ["storm", "cyclone", "typhoon", "hurricane", "tornado",
               "monsoon", "blizzard", "thunderstorm", "tropical storm"],
    "earthquakes": ["earthquake", "quake", "aftershock", "magnitude",
                    "tremor", "usgs", "seismic"],
    "volcanoes": ["volcano", "eruption", "magma", "lava", "ash cloud"],
    "tsunamis": ["tsunami", "tidal wave"],
    "wildfires": ["wildfire", "bushfire", "forest fire", "blaze"],
    "disasters": ["disaster", "evacuation", "emergency", "death toll",
                  "rescue", "relief", "devastat"],
    "agriculture": ["agriculture", "farm", "farming", "crop", "harvest",
                    "grain", "fertilizer", "agrarian"],
    "food": ["food", "food security", "hunger", "famine", "malnutrition",
             "food price", "staple"],
    "water": ["water", "drought", "water shortage", "groundwater",
              "reservoir", "water crisis", "river"],
    "fisheries": ["fishery", "fishing", "fish stock", "aquaculture",
                  "coastal waters", "fishermen"],
    "animal_health": ["animal health", "livestock", "veterinary",
                      "foot-and-mouth", "african swine fever", "cattle",
                      "swine fever"],
    "transport": ["transport", "transit", "commuter"],
    "aviation": ["aviation", "airline", "flight", "airport", "plane",
                 "aircraft", "icao", "air traffic", "pilot"],
    "shipping": ["shipping", "port", "freight", "cargo", "vessel",
                 "tanker", "imo", "container", "maritime", "sea route",
                 "strait", "canal", "waterway"],
    "rail": ["rail", "train", "railway", "metro", "subway"],
    "automotive": ["automotive", "car", "ev", "electric vehicle",
                   "automaker", "tesla", "toyota", "volkswagen",
                   "car industry", "auto industry", "auto"],
    "logistics": ["logistics", "warehouse", "delivery", "courier",
                  "shipping delays"],
    "supply_chains": ["supply chain", "shortage", "bottleneck"],
    "infrastructure": ["infrastructure", "public works", "infrastructural"],
    "power_grids": ["power grid", "grid", "electricity network",
                    "transmission", "power outage"],
    "dams": ["dam", "hydropower", "reservoir"],
    "roads": ["road", "highway", "motorway", "traffic"],
    "bridges": ["bridge", "overpass", "viaduct"],
    "construction": ["construction", "building", "contractor",
                     "real estate", "housing", "developer"],
    "law": ["law", "legal", "lawsuit", "sue", "litigation", "lawyers"],
    "courts": ["court", "judge", "trial", "verdict", "sentence",
               "prosecutor", "indictment", "supreme court", "appeal"],
    "regulation": ["regulation", "regulator", "fine", "fined", "antitrust",
                   "compliance", "licensing", "watchdog", "probe",
                   "penalty"],
    "corruption": ["corruption", "bribery", "embezzlement", "fraud",
                   "money laundering", "cocaine", "heroin", "narcotics",
                   "drug trafficking", "smuggling"],
    "human_rights": ["human rights", "torture", "censorship",
                     "freedom of speech", "prisoner", "asylum", "refugee",
                     "persecution", "rights group", "immigration",
                     "immigrant", "migrant", "deportation", "disability",
                     "disabled"],
    "labor": ["labor", "labour", "workers", "union", "strike", "wages",
              "minimum wage", "workforce"],
    "employment": ["employment", "jobs", "job market", "unemployment",
                   "hiring", "layoff", "welfare", "social assistance",
                   "social security", "benefits"],
    "education": ["education", "school", "university", "students",
                  "teacher", "college", "curriculum", "academic"],
    "sports": ["football", "soccer", "cricket", "tennis", "basketball",
               "baseball", "golf", "formula 1", "f1", "olympics",
               "athlete", "championship", "tournament", "league",
               "world cup", "match", "grand prix", "hockey", "defeats",
               "test series", "test match", "semifinal", "quarterfinal",
               "playoff", "qualify", "defending champion", "beat"],
    "culture": ["culture", "museum", "heritage", "art", "artist",
                "literature", "film", "music", "theatre", "festival",
                "pride", "monument", "archaeolog"],
    "entertainment": ["entertainment", "celebrity", "hollywood", "tv",
                      "streaming", "box office", "award", "song",
                      "movie"],
}

# Terms are matched with an optional plural suffix ("tariffs"
# matches "tariff", "missiles" matches "missile").  Terms that are
# already inflected ("shares", "stocks", "flooding", "commodit")
# are unaffected: the suffix group is optional and the word
# boundary still requires a real word.
_TERM_RES = {
    sub: {
        term: re.compile(
            r"\b" + re.escape(term) + r"(?:s|es)?\b",
            re.IGNORECASE,
        )
        for term in terms
    }
    for sub, terms in SECTOR_TERMS.items()
}

_SUB_TO_TOP = {
    sub: top
    for top, subs in SECTOR_TREE.items()
    for sub in subs
}

# Source topic labels that are genuine topics (used as a fallback).
_KNOWN_TOPIC_LABELS = set(SECTOR_TREE.keys()) | set(_SUB_TO_TOP.keys())

# Geographic/source labels that are never topics.
_GEO_LABELS = {
    "world", "africa", "india", "japan", "china", "south-korea",
    "southeast-asia", "europe", "middle-east", "latin-america",
    "canada", "australia", "pacific", "south-asia", "east-asia",
    "oceania", "north-america", "south-america", "central-asia",
}


def sub_sectors(top=None):
    if top is None:
        return [s for subs in SECTOR_TREE.values() for s in subs]
    return list(SECTOR_TREE.get(top, []))


def top_sectors():
    return list(SECTOR_TREE.keys())


def classify_sector(title, summary, source_category=None):
    """Classify an article into (top_sector, sub_sector).

    Scoring counts word-boundary occurrences of each sub-sector's
    terms in title+summary.  Ties are broken deterministically by
    definition order; a genuine source topic label among the tied
    sub-sectors wins the tie.  The source's own topic label is used
    as a fallback only when the article carries no topic signal at
    all (and that label is a real topic, not a geographic label).

    Returns ("other", "general") when nothing matches.
    """
    lower = ((title or "") + " " + (summary or "")).lower()
    raw = (source_category or "").lower().strip()

    scores = {}
    for sub, res in _TERM_RES.items():
        total = 0
        for rx in res.values():
            total += len(rx.findall(lower))
        scores[sub] = total

    best_score = max(scores.values())

    if best_score == 0:
        if raw in _SUB_TO_TOP and raw not in _GEO_LABELS:
            return _SUB_TO_TOP[raw], raw
        if raw in SECTOR_TREE and raw not in _GEO_LABELS:
            return raw, raw
        return "other", "general"

    tied = [sub for sub, score in scores.items() if score == best_score]

    if raw in tied and raw not in _GEO_LABELS:
        best_sub = raw
    else:
        best_sub = tied[0]

    return _SUB_TO_TOP[best_sub], best_sub
