from urllib.parse import urlparse

KNOWN_TIERS = {
    "feeds.bbci.co.uk":2,
    "www.aljazeera.com":2,
    "rss.dw.com":2,
    "earthquake.usgs.gov":1,
    "www.nasa.gov":1,
    "www.jpl.nasa.gov":1,
    "www.sec.gov":1,
    "oceanservice.noaa.gov":1,
    "www.cisa.gov":1,
    "www.who.int":1,
    "www.esa.int":1,
    "indianexpress.com":2,
    "www.arabnews.com":2,
    "www.africanews.com":2
}
TIER_SCORE={1:40,2:25,3:12,4:0}

def domain(url):
    return urlparse(url).netloc.lower().removeprefix("www.")

def get_tier(item):
    if item.get("tier") in (1,2,3,4):
        return item["tier"]
    return KNOWN_TIERS.get(domain(item.get("url","")),4)

def is_discovery(item):
    return bool(item.get("discovery")) or domain(item.get("url",""))=="news.google.com"

def reliability_bonus(item):
    if is_discovery(item):
        return 0
    return TIER_SCORE[get_tier(item)]
