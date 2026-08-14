"""Geographic taxonomy and event-geography classification.

The audit must track EVENT geography separately from source/publisher
geography: an article from a British publisher about an earthquake in
Japan belongs to East Asia, not Europe.  classify_event_region()
scans an article's title+summary for country/place mentions and maps
the first (strongest) mention to the region tree below.

The tree follows a UN-style breakdown:

    Africa        North / West / East / Central / Southern
    Asia          South / East / Southeast / Central / West (Middle East)
    Europe
    North America US / Canada / Mexico / Central America / Caribbean
    South America
    Oceania       Australia / New Zealand / Pacific Islands

This is a heuristic for coverage measurement only: it never gates
publishing.  Region assignment for a story that names no place is
"unknown" and reported as such.
"""

import re

# (top_region, sub_region) -> place keywords.  Sub-region order
# within each top region matters: matches are scanned in definition
# order and the FIRST match wins.
REGION_TREE = {
    "Africa": {
        "North Africa": ["algeria", "algerian", "egypt", "egyptian",
                         "libya", "libyan", "morocco", "moroccan",
                         "tunisia", "tunisian", "western sahara", "sudan",
                         "sudanese", "khartoum", "cairo", "alexandria",
                         "casablanca", "tripoli", "algiers", "tunis",
                         "rabat"],
        "West Africa": ["benin", "burkina faso", "burkinabe", "cape verde",
                        "cabo verde", "ivory coast", "cote d'ivoire",
                        "gambia", "ghana", "ghanian", "guinea",
                        "guinea-bissau", "liberia", "liberian", "mali",
                        "mauritania", "niger", "nigeria", "nigerian",
                        "senegal", "senegalese", "sierra leone", "togo",
                        "abidjan", "accra", "dakar", "lagos", "abuja"],
        "East Africa": ["burundi", "comoros", "djibouti", "eritrea",
                        "ethiopia", "ethiopian", "kenya", "kenyan",
                        "madagascar", "malawi", "malawian", "mauritius",
                        "mozambique", "rwanda", "rwandan", "seychelles",
                        "somalia", "somaliland", "south sudan", "tanzania",
                        "tanzanian", "uganda", "ugandan", "zambia",
                        "zambian", "zimbabwe", "zimbabwean", "addis ababa",
                        "nairobi", "mombasa", "kampala", "dar es salaam",
                        "kigali", "mogadishu", "lusaka", "harare",
                        "maputo", "antananarivo"],
        "Central Africa": ["angola", "cameroon", "cameroonian",
                           "central african republic", "chad", "chadian",
                           "congo", "dr congo", "drc", "democratic republic",
                           "equatorial guinea", "gabon", "sao tome",
                           "kinshasa", "yaounde", "bangui", "luanda",
                           "brazzaville", "douala"],
        "Southern Africa": ["botswana", "eswatini", "swaziland", "lesotho",
                            "namibia", "namibian", "south africa",
                            "south african", "johannesburg", "pretoria",
                            "cape town", "durban", "windhoek", "gaborone"],
    },
    "Asia": {
        "South Asia": ["afghanistan", "afghan", "bangladesh",
                       "bangladeshi", "bhutan", "india", "indian",
                       "maldives", "nepal", "nepali", "pakistan",
                       "pakistani", "sri lanka", "sri lankan", "kabul",
                       "kandahar", "delhi", "new delhi", "mumbai",
                       "bengaluru", "chennai", "kolkata", "islamabad",
                       "karachi", "lahore", "dhaka", "kathmandu",
                       "colombo"],
        "East Asia": ["china", "chinese", "japan", "japanese", "mongolia",
                      "north korea", "north korean", "south korea",
                      "south korean", "korean", "taiwan", "taiwanese",
                      "hong kong", "macau", "beijing", "shanghai",
                      "shenzhen", "tokyo", "osaka", "kyoto", "seoul",
                      "pyongyang", "taipei", "ulan bator"],
        "Southeast Asia": ["brunei", "cambodia", "cambodian", "timor-leste",
                           "east timor", "indonesia", "indonesian",
                           "laos", "lao", "malaysia", "malaysian",
                           "myanmar", "burma", "philippines", "philippine",
                           "singapore", "singaporean", "thailand",
                           "thai", "vietnam", "vietnamese", "jakarta",
                           "bali", "manila", "bangkok", "hanoi",
                           "ho chi minh", "kuala lumpur", "phnom penh",
                           "yangon"],
        "Central Asia": ["kazakhstan", "kazakh", "kyrgyzstan", "kyrgyz",
                         "tajikistan", "tajik", "turkmenistan",
                         "turkmen", "uzbekistan", "uzbek", "astana",
                         "tashkent", "almaty", "bishkek", "ashgabat"],
        "West Asia / Middle East": ["bahrain", "iran", "iranian", "iraq",
                                    "iraqi", "israel", "israeli",
                                    "jordan", "jordanian", "kuwait",
                                    "kuwaiti", "lebanon", "lebanese",
                                    "oman", "omani", "palestine",
                                    "palestinian", "gaza", "west bank",
                                    "qatar", "qatari", "saudi arabia",
                                    "saudi", "syria", "syrian", "turkey",
                                    "turkish", "turk", "uae", "emirates",
                                    "united arab emirates", "yemen",
                                    "yemeni", "cyprus", "cypriot",
                                    "tehran", "baghdad", "jerusalem",
                                    "tel aviv", "beirut", "damascus",
                                    "amman", "riyadh", "doha", "dubai",
                                    "abu dhabi", "muscat", "sanaa",
                                    "istanbul", "ankara", "kurdish",
                                    "kurdistan"],
    },
    "Europe": {
        "Europe": ["albania", "albanian", "andorra", "austria",
                   "austrian", "belarus", "belarusian", "belgium",
                   "belgian", "bosnia", "bulgaria", "bulgarian", "croatia",
                   "croatian", "czech", "denmark", "danish", "estonia",
                   "estonian", "finland", "finnish", "france", "french",
                   "germany", "german", "greece", "greek", "hungary",
                   "hungarian", "iceland", "icelandic", "ireland",
                   "irish", "italy", "italian", "kosovo", "latvia",
                   "latvian", "liechtenstein", "lithuania", "lithuanian",
                   "luxembourg", "malta", "moldova", "moldovan",
                   "monaco", "montenegro", "netherlands", "dutch",
                   "north macedonia", "norway", "norwegian", "poland",
                   "polish", "portugal", "portuguese", "romania",
                   "romanian", "russia", "russian", "san marino",
                   "serbia", "serbian", "slovakia", "slovak", "slovenia",
                   "slovenian", "spain", "spanish", "sweden", "swedish",
                   "switzerland", "swiss", "ukraine", "ukrainian",
                   "united kingdom", "britain", "british", "uk",
                   "england", "english", "scotland", "scottish", "wales",
                   "welsh", "northern ireland", "london", "paris",
                   "berlin", "munich", "rome", "milan", "madrid",
                   "barcelona", "lisbon", "amsterdam", "brussels",
                   "vienna", "warsaw", "prague", "budapest", "stockholm",
                   "oslo", "copenhagen", "helsinki", "dublin", "athens",
                   "kyiv", "kiev", "moscow", "st petersburg", "belgrade",
                   "bucharest", "sofia", "zagreb", "geneva", "zurich",
                   "minsk", "vilnius", "riga", "tallinn", "luxembourg city",
                   "the hague", "marseille", "lyon", "hamburg", "cologne",
                   "naples", "turku"],
    },
    "North America": {
        "United States": ["united states", "usa", "u.s.", "american",
                          "washington", "white house", "new york",
                          "los angeles", "chicago", "houston", "miami",
                          "san francisco", "seattle", "boston",
                          "atlanta", "philadelphia", "denver", "phoenix",
                          "detroit", "minneapolis", "portland",
                          "san diego", "dallas", "austin", "nashville",
                          "memphis", "new orleans", "st. louis",
                          "cleveland", "pittsburgh", "baltimore",
                          "honolulu", "anchorage", "texas", "california",
                          "florida", "alaska", "hawaii"],
        "Canada": ["canada", "canadian", "toronto", "montreal",
                   "vancouver", "ottawa", "calgary", "edmonton",
                   "winnipeg", "quebec", "halifax"],
        "Mexico": ["mexico", "mexican", "mexico city", "tijuana",
                   "cancun", "guadalajara", "monterrey"],
        "Central America": ["belize", "costa rica", "costa rican",
                            "el salvador", "salvadoran", "guatemala",
                            "guatemalan", "honduras", "honduran",
                            "nicaragua", "nicaraguan", "panama",
                            "panamanian", "panama city", "san jose",
                            "tegucigalpa"],
        "Caribbean": ["antigua", "bahamas", "barbados", "cuba", "cuban",
                      "dominica", "dominican republic", "dominican",
                      "grenada", "haiti", "haitian", "jamaica",
                      "jamaican", "st kitts", "saint kitts", "st lucia",
                      "saint lucia", "st vincent", "trinidad",
                      "trinidad and tobago", "puerto rico", "havana",
                      "kingston", "port-au-prince", "san juan",
                      "santo domingo", "nassau"],
    },
    "South America": {
        "South America": ["argentina", "argentine", "argentinian",
                          "bolivia", "bolivian", "brazil", "brazilian",
                          "chile", "chilean", "colombia", "colombian",
                          "ecuador", "ecuadorian", "guyana", "paraguay",
                          "paraguayan", "peru", "peruvian", "suriname",
                          "uruguay", "uruguayan", "venezuela",
                          "venezuelan", "sao paulo", "rio de janeiro",
                          "buenos aires", "santiago", "bogota", "lima",
                          "caracas", "quito", "la paz", "montevideo",
                          "asuncion", "brasilia", "medellin", "quito"],
    },
    "Oceania": {
        "Australia": ["australia", "australian", "sydney", "melbourne",
                      "brisbane", "perth", "adelaide", "canberra",
                      "gold coast", "new south wales", "queensland",
                      "victoria", "tasmania"],
        "New Zealand": ["new zealand", "new zealander", "auckland",
                        "wellington", "christchurch", "otago"],
        "Pacific Islands": ["fiji", "fijian", "papua new guinea", "png",
                            "solomon islands", "vanuatu", "samoa",
                            "tonga", "kiribati", "nauru", "palau",
                            "micronesia", "marshall islands", "tuvalu",
                            "new caledonia", "french polynesia", "guam",
                            "tahiti", "suva", "port moresby"],
    },
}

# Multi-word markers for the United States: a bare lowercase "us "
# is risky (it is a pronoun) and never matches, but an UPPERCASE
# standalone "US" word (US officials, US 2-1, US accuses) is almost
# always the United States.  "USSR"/"USMCA" are excluded by the
# trailing word boundary.  The sub-region keyword lists also carry
# "washington", "white house", and US state/city names.
_US_RE = re.compile(
    r"\b(?:U\.S\.|USA|United States)\b"
    r"|\bUS\b"
)

# America alone never implies the United States (Latin America,
# North America, South America are continents).
_AMERICA_EXCLUSIONS = re.compile(
    r"\b(?:latin|south|north|central)\s+america\b", re.IGNORECASE
)


def _compile():
    compiled = {}
    for top, subs in REGION_TREE.items():
        compiled[top] = {}
        for sub, terms in subs.items():
            compiled[top][sub] = [
                re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE)
                for t in terms
            ]
    return compiled


_COMPILED = _compile()


def classify_event_region(title, summary):
    """Map an article to (top_region, sub_region) from its text.

    The first matching place mention wins, scanned top-region by
    top-region then sub-region by sub-region in definition order.
    Returns (None, None) when no place is named (coverage counts it
    as "unknown").  Event geography is inferred from the article's
    OWN text, never from the publisher's location.
    """
    text = (title or "") + " " + (summary or "")
    if _AMERICA_EXCLUSIONS.search(text):
        text = _AMERICA_EXCLUSIONS.sub(" ", text)

    # The earliest place mention in the text wins: the subject of a
    # headline ("Canada pushes back on US tariffs") names its region
    # before a secondary mention, and a dateline is by definition
    # where the story is reported from.  The uppercase
    # "US"/"U.S."/"USA" abbreviation alone is a strong United
    # States signal (the keyword list never contains the ambiguous
    # lowercase pronoun "us").
    best = None
    for top, subs in _COMPILED.items():
        for sub, res in subs.items():
            m = _US_RE.search(text) if sub == "United States" else None
            if m is not None:
                if best is None or m.start() < best[0]:
                    best = (m.start(), top, sub)
            for rx in res:
                m = rx.search(text)
                if m is not None:
                    if best is None or m.start() < best[0]:
                        best = (m.start(), top, sub)
    if best is None:
        return None, None
    return best[1], best[2]


def all_regions():
    """Flat list of (top_region, sub_region) pairs."""
    return [
        (top, sub)
        for top, subs in REGION_TREE.items()
        for sub in subs
    ]


def top_regions():
    return list(REGION_TREE.keys())
