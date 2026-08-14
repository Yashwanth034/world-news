"""Article extraction for thin important Telegram stories.

Extractive-only enrichment: for an important story whose RSS
briefing carries fewer than two useful sentences, fetch the
original article, extract verbatim text with trafilatura, and
let the existing briefing pipeline filter it (filler, headline
paraphrase, duplication, conflict).  Results are cached per
story so each story is fetched at most once per TTL window.

Safety properties (never relaxed):
  - http/https only, no cookies, no credentials, no JS
  - private/loopback/link-local/ULA IPs are rejected, and every
    redirect hop is re-validated (DNS + allowlist + scheme)
  - bounded size (2 MB), bounded time (10 s connect / 20 s
    read / 30 s total), at most 3 redirects
  - domain allowlist derived from the configured feeds
  - robots.txt is honored via urllib.robotparser
  - paywalls and WAF-blocked pages are never bypassed
  - a failing story never fails the pipeline
"""
import ipaddress
import json
import re
import socket
import sqlite3
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
import trafilatura

from courlan import extract_domain
from src import source_reliability
from src.telegram_briefing import (
    JUST_IN,
    UPDATE,
    clean_sentence_text,
    is_filler,
    is_headline_paraphrase,
    split_sentences,
)

USER_AGENT = (
    "WorldNewsBot/1.0 "
    "(briefing enrichment; news RSS pipeline; contact: local)"
)

# Failure statuses stored with the shorter (negative) cache TTL.
NEGATIVE_STATUSES = {
    "http_error",
    "blocked",
    "non_article",
    "paywall",
    "no_text",
    "junk",
    "timeout",
    "too_large",
    "not_html",
    "network_error",
}

OK_STATUS = "ok"

PAYWALL_MARKERS = [
    "to continue reading",
    "subscribe to read",
    "subscriber-only",
    "subscribers only",
    "sign in to continue",
    "subscription required",
    "to read the full",
    "become a subscriber",
    "unlock this article",
]

# Feeds hosted on a sibling domain whose articles live on a
# different registrable domain (feeds.bbci.co.uk -> bbc.co.uk).
DOMAIN_ALIASES = {
    "bbci.co.uk": "bbc.co.uk",
}

# Aggregator hosts whose URLs are wrappers that only redirect
# to arbitrary publishers; never worth fetching.
HOST_BLOCKLIST = {
    "news.google.com",
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------

def non_article_url(url, non_article_segments):
    """Whether the URL points to a video / liveblog / listing
    page instead of a plain article (segment-based, so e.g.
    "/news/liveblog/..." is rejected while "/news/world" is not).
    """
    if not url:
        return True
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() in HOST_BLOCKLIST:
        return True
    path = parsed.path or ""
    segments = {
        s.lower()
        for s in path.split("/")
        if s and s != "news"
    }
    blocked = {
        s.strip().lower()
        for s in (non_article_segments or [])
        if isinstance(s, str) and s.strip()
    }
    return bool(segments & blocked)


def _registered_domain(url):
    if not url:
        return None
    try:
        return extract_domain(url)
    except Exception:
        netloc = urlparse(url).netloc.lower()
        if not netloc:
            return None
        parts = netloc.split(":")[0].split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else netloc


def feed_domain_allowlist(feeds):
    """Registrable domains of all configured feed URLs, e.g.
    feeds.bbci.co.uk -> bbci.co.uk (plus aliased article
    domains like bbc.co.uk).  None/empty input yields an
    empty allowlist (no fetches allowed)."""
    domains = set()
    for feed in (feeds or []):
        url = (feed or {}).get("url")
        domain = _registered_domain(url)
        if domain:
            domains.add(domain)
            alias = DOMAIN_ALIASES.get(domain)
            if alias:
                domains.add(alias)
    return domains


def domain_allowed(url, allowlist):
    if not url:
        return False
    domain = _registered_domain(url)
    if not domain or not allowlist:
        return False
    return domain in allowlist


def _public_ip(hostname):
    """First public IPv4/IPv6 address for the hostname, or None
    when the hostname resolves only to private/loopback/etc."""
    try:
        infos = socket.getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
    except OSError:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(
                info[4][0].split("%")[0]
            )
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback:
            continue
        if ip.is_link_local or ip.is_unspecified:
            continue
        if ip.is_multicast or ip.is_reserved:
            continue
        if ip.version == 4:
            if (
                ip in ipaddress.ip_network("100.64.0.0/10")
                or ip in ipaddress.ip_network("169.254.0.0/16")
            ):
                continue
        elif ip.is_site_local:
            continue
        return str(ip)
    return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

class _PaceLock:
    """Same-domain pacing (at least min_interval between two
    requests to the same host).  The global per-run fetch
    budget lives in enrich_thin_stories."""

    def __init__(self, min_interval):
        self._lock = threading.Lock()
        self._last = {}
        self._min_interval = float(min_interval or 0)

    def wait_for(self, hostname, now_func=time.monotonic):
        with self._lock:
            last = self._last.get(hostname)
            if last is not None:
                remaining = (
                    last + self._min_interval - now_func()
                )
                if remaining > 0:
                    time.sleep(remaining)
            self._last[hostname] = now_func()
            return True


class _RobotCache:
    def __init__(self, timeout=10):
        self._cache = {}
        self._timeout = timeout

    def allowed(self, hostname, path):
        if hostname not in self._cache:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(
                f"https://{hostname}/robots.txt"
            )
            try:
                parser.read()
            except Exception:
                # Fail-safe: cannot read robots.txt -> do not fetch.
                self._cache[hostname] = None
            else:
                self._cache[hostname] = parser
        parser = self._cache[hostname]
        if parser is None:
            return False
        return parser.can_fetch(USER_AGENT, path)


def _validate_url(url, allowlist):
    """Static URL checks before any network activity."""
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    if not domain_allowed(url, allowlist):
        return False
    if _public_ip(hostname) is None:
        return False
    return True


def _looks_like_html(head):
    head = (head or "").lstrip()[:2048].lower()
    if "<!doctype html" in head or "<html" in head:
        return True
    return "<" in head and not head.lstrip().startswith("{")


def _is_paywall(text, markers):
    if not text:
        return False
    lowered = text.lower()
    return any(m in lowered for m in markers)


class _DeadlineExceeded(Exception):
    """Internal: the bounded worker thread did not finish in time."""


def _run_with_deadline(fn, timeout):
    """Run fn in a daemon thread and wait at most `timeout` seconds.

    Returns (True, value) on success.  On timeout the worker is
    ABANDONED - the daemon thread keeps running (its socket leaks
    until the process exits) and (False, None) is returned, so a
    server that stalls inside a socket read can never hang the
    pipeline no matter what the HTTP library's own timeouts do.
    Worker exceptions are re-raised in the caller thread.  Never
    blocks longer than `timeout` seconds."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported
            box["error"] = exc

    thread = threading.Thread(
        target=target,
        daemon=True,
        name="wn-fetch-deadline",
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("value")


def _network_fetch(
    url, cfg, allowlist, robots, pace,
    total_timeout, connect_timeout, read_timeout,
    max_bytes, max_redirects, payload,
):
    """The network section of fetch_article: robots.txt check,
    redirect loop, body streaming and extraction.  Runs inside a
    deadline-bounded worker thread (see _run_with_deadline) so no
    socket read can stall the pipeline.  Returns (status, payload);
    never raises - worker exceptions propagate through the bounded
    runner to fetch_article's handler."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not robots.allowed(hostname, parsed.path or "/"):
        return ("blocked", payload)

    deadline = time.monotonic() + total_timeout
    current = url
    redirect_count = 0

    while True:
        if redirect_count > max_redirects:
            return ("http_error", payload)
        if time.monotonic() > deadline:
            return ("timeout", payload)
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return ("blocked", payload)
        hostname = (parsed.hostname or "").lower()
        if not _validate_url(current, allowlist):
            return ("blocked", payload)
        if not robots.allowed(hostname, parsed.path or "/"):
            return ("blocked", payload)
        if pace is not None:
            if not pace.wait_for(hostname):
                return ("blocked", payload)

        remaining = max(
            deadline - time.monotonic(), 1
        )
        resp = requests.get(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "*/*;q=0.8"
                ),
            },
            timeout=(
                connect_timeout,
                min(
                    read_timeout,
                    remaining,
                ),
            ),
            allow_redirects=False,
            stream=True,
            cookies=None,
        )
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    return ("http_error", payload)
                current = (
                    location
                    if location.startswith("http")
                    else urljoin(current, location)
                )
                redirect_count += 1
                continue
            if resp.status_code != 200:
                return ("http_error", payload)
            content_type = (
                resp.headers.get("Content-Type") or ""
            ).lower()
            if content_type and (
                "text/html" not in content_type
                and "application/xhtml+xml" not in content_type
            ):
                return ("not_html", payload)
            length_header = resp.headers.get(
                "Content-Length"
            )
            if (
                length_header
                and int(length_header) > max_bytes
            ):
                return ("too_large", payload)
            chunks = []
            size = 0
            for chunk in resp.iter_content(65536):
                if time.monotonic() > deadline:
                    return ("timeout", payload)
                size += len(chunk)
                if size > max_bytes:
                    return ("too_large", payload)
                chunks.append(chunk)
            body = b"".join(chunks)
            if not _looks_like_html(body[:2048].decode(
                "utf-8", "ignore"
            )):
                return ("not_html", payload)
        finally:
            resp.close()
        break

    html = body.decode("utf-8", "replace")
    text = trafilatura.extract(
        html,
        url=current,
        output_format="txt",
        include_comments=False,
        include_tables=False,
        include_links=False,
        deduplicate=True,
    )

    if not text or not text.strip():
        return ("no_text", payload)

    if len(text.strip()) < 200 or _is_paywall(
        text,
        cfg.get("paywall_markers") or PAYWALL_MARKERS,
    ):
        return ("paywall", payload)

    payload["text"] = text.strip()
    payload["title"] = None
    try:
        meta = trafilatura.extract_metadata(
            html, url=current
        )
        if meta is not None and getattr(meta, "title", None):
            payload["title"] = meta.title.strip()
    except Exception:
        pass
    payload["url"] = current
    return (OK_STATUS, payload)


def fetch_article(url, cfg, allowlist, robots=None, pace=None):
    """Fetch and extract one article.  Returns
    (status, payload) where payload carries text/title on
    success.  Never raises and NEVER blocks longer than
    total_timeout_seconds: the network section (robots.txt,
    redirects, body streaming) runs inside a deadline-bounded
    daemon thread, so a server that stalls mid-request returns
    ("timeout", {}) instead of hanging the pipeline."""
    payload = {}
    try:
        if not url:
            return ("non_article", payload)
        if non_article_url(
            url, cfg.get("non_article_segments")
        ):
            return ("non_article", payload)
        if not _validate_url(url, allowlist):
            return ("blocked", payload)

        total_timeout = float(
            cfg.get("total_timeout_seconds", 30)
        )
        connect_timeout = float(
            cfg.get("connect_timeout_seconds", 10)
        )
        read_timeout = float(
            cfg.get("read_timeout_seconds", 20)
        )
        max_bytes = int(cfg.get("max_bytes", 2 * 1024 * 1024))
        max_redirects = int(cfg.get("max_redirects", 3))

        if robots is None:
            robots = _RobotCache()

        ok, result = _run_with_deadline(
            lambda: _network_fetch(
                url, cfg, allowlist, robots, pace,
                total_timeout, connect_timeout, read_timeout,
                max_bytes, max_redirects, payload,
            ),
            total_timeout,
        )
        if not ok:
            return ("timeout", payload)
        return result
    except requests.Timeout:
        return ("timeout", payload)
    except requests.ConnectionError:
        return ("network_error", payload)
    except Exception:
        return ("network_error", payload)


# ---------------------------------------------------------------------------
# Sentence extraction (verbatim only)
# ---------------------------------------------------------------------------

JUNK_LINE_MARKERS = [
    "recommended stories",
    "list of",
    "related topics",
    "related stories",
    "more top stories",
    "most popular",
    "trending now",
    "you might also like",
    "you may also like",
    "get in touch",
    "- published",
    "read more",
    "continue reading",
    "more on this story",
    "advertisement",
    "advertorial",
    "sponsored",
    "promoted",
    "sign up",
    "subscribe",
    "newsletter",
    "download our app",
    "download the app",
    "get the app",
    "our app",
    "follow us",
    "share this",
    "share on",
    "watch:",
    "video:",
    "image caption",
    "photo caption",
    "caption:",
    "author:",
    "published:",
    "updated:",
    "about us",
    "contact us",
    "privacy policy",
    "terms of",
    "cookie",
    "accept all",
    "buy now",
    "shop now",
    "add to cart",
    "in stock",
    "our verdict",
    "hands-on",
    "review:",
    "specifications",
    "dimensions:",
    "warranty",
    "this article was amended",
]

_JUNK_MATCH_RE = re.compile(
    "|".join(
        re.escape(m) for m in JUNK_LINE_MARKERS
    ),
    re.IGNORECASE,
)


def article_junk_ratio(text):
    """Fraction of non-empty extracted lines that look like
    navigation, promotion, captions, product metadata or
    subscription boilerplate.  0.0 for clean text, 1.0 for a
    page that is entirely chrome."""
    lines = [
        ln.strip()
        for ln in (text or "").split("\n")
        if ln.strip()
    ]
    if not lines:
        return 0.0
    hits = sum(
        1 for ln in lines if _JUNK_MATCH_RE.search(ln)
    )
    return hits / len(lines)

_SENT_END_RE = re.compile(r"[.!?…][\"'\u201d\u2019)\]]*$")

# A recommended-story navigation item: "- list 1 of 3Headline ...".
# The marker is fused to the next item's headline with no space, so
# the marker alone is never enough - the WHOLE line is navigation
# and is dropped, whatever follows the marker.
_LIST_ITEM_RE = re.compile(
    r"^\s*[-•*]?\s*list\s+\d+\s+of\s+\d+",
    re.IGNORECASE,
)


def article_sentences(
    text, headline, max_sentences=12
):
    """Verbatim sentences from extracted text, cleaned and
    filler-filtered.

    The extracted text is one paragraph per line; lines that
    are not complete sentences (the headline, section
    headers, nav labels, topic tags, recommendation blocks)
    are skipped, and each remaining paragraph is split into
    its sentences.  Headline-paraphrase filtering is also
    applied here and again downstream by the briefing
    pipeline (aggregate_sentences)."""
    out = []
    seen = set()
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(
            marker in lowered
            for marker in JUNK_LINE_MARKERS
        ):
            continue
        if _LIST_ITEM_RE.match(line):
            continue
        if not _SENT_END_RE.search(line):
            continue
        for sentence in split_sentences(line):
            sentence = clean_sentence_text(sentence)
            if not sentence:
                continue
            if is_filler(sentence):
                continue
            if is_headline_paraphrase(
                sentence, headline
            ):
                continue
            key = re.sub(
                r"[^a-z0-9 ]", " ", sentence.lower()
            ).strip()
            if key in seen:
                continue
            seen.add(key)
            out.append(sentence)
            if max_sentences and len(out) >= max_sentences:
                return out
    return out


def count_useful(summary, title):
    """Number of useful explanatory sentences in a summary
    (the thinness measure used by the enrichment gate)."""
    seen = set()
    useful = 0
    for sentence in split_sentences(summary or ""):
        sentence = clean_sentence_text(sentence)
        if not sentence:
            continue
        if is_filler(sentence):
            continue
        if is_headline_paraphrase(sentence, title or ""):
            continue
        key = re.sub(
            r"[^a-z0-9 ]", " ", sentence.lower()
        ).strip()
        if key in seen:
            continue
        seen.add(key)
        useful += 1
    return useful


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class ArticleCache:
    """SQLite-backed cache of extraction results.

    Positive results are cached for cache_ttl_hours_ok hours,
    failures for cache_ttl_hours_error hours.  Raw HTML is
    never stored.

    The cache is a best-effort side store: a lock conflict with
    the main pipeline connection ("database is locked") degrades
    to a cache miss / no-op instead of stalling or failing the
    run, so enrichment never blocks on the cache."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        try:
            # isolation_level=None = autocommit: every statement
            # is its own transaction, so a blocked COMMIT can
            # never leave a RESERVED/EXCLUSIVE lock behind that
            # would deadlock the main pipeline connection (which
            # commits once per run, after the decide loop).
            self._conn = sqlite3.connect(
                self.db_path,
                timeout=2.0,
                isolation_level=None,
            )
            self._conn.execute(
                "PRAGMA busy_timeout=2000"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS article_cache ("
                " story_id TEXT PRIMARY KEY,"
                " url TEXT NOT NULL,"
                " status TEXT NOT NULL,"
                " text TEXT,"
                " sentences_json TEXT,"
                " title TEXT,"
                " fetched_at TEXT NOT NULL,"
                " ttl_hours INTEGER NOT NULL)"
            )
        except sqlite3.Error:
            # Locked/read-only DB: the cache is optional.
            self._conn = None

    def _open_ok(self):
        return self._conn is not None

    def get(self, story_id, now=None):
        if not self._open_ok():
            return None
        try:
            now = now or datetime.now(timezone.utc)
            row = self._conn.execute(
                "SELECT story_id, url, status, text, "
                "sentences_json, title, fetched_at, ttl_hours "
                "FROM article_cache WHERE story_id=?",
                (story_id,),
            ).fetchone()
            if row is None:
                return None
            entry = {
                "story_id": row[0],
                "url": row[1],
                "status": row[2],
                "text": row[3],
                "sentences_json": row[4],
                "title": row[5],
                "fetched_at": row[6],
                "ttl_hours": row[7],
            }
            fetched = datetime.fromisoformat(entry["fetched_at"])
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            age = now - fetched
            if age.total_seconds() > float(entry["ttl_hours"]) * 3600:
                return None
            return entry
        except sqlite3.Error:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            return None

    def set(self, story_id, url, status, text=None,
            sentences=None, title=None, ttl_ok=48,
            ttl_error=24, now=None):
        if not self._open_ok():
            return
        try:
            now = now or datetime.now(timezone.utc)
            ttl = (
                ttl_ok if status == OK_STATUS else ttl_error
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO article_cache ("
                "story_id, url, status, text, sentences_json, "
                "title, fetched_at, ttl_hours) VALUES (?,?,?,?,?,"
                "?,?,?)",
                (
                    story_id,
                    url,
                    status,
                    text,
                    json.dumps(sentences or [], ensure_ascii=False)
                    if sentences is not None else None,
                    title,
                    now.isoformat(),
                    ttl,
                ),
            )
        except sqlite3.Error:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            # Locked or unavailable: never fail the pipeline for
            # a cache write.
            return

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None


def cache_sentences(entry):
    if not entry or not entry.get("sentences_json"):
        return []
    try:
        return json.loads(entry["sentences_json"]) or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Mass-casualty signal
#
# A narrow, high-precision signal used only by the enrichment
# gate.  A thin HIGH-priority story scoring 60-64 may be fetched
# when it carries strong casualty evidence in a serious context.
#
# A single generic word ("death", "die", "bodies") is never
# sufficient.  The signal requires a mass-casualty phrase
# ("mass grave", "death toll", "killings"), a casualty count of
# two or more ("25 killed", "30 bodies", "dozens dead"), or
# plural victims with a casualty word ("people killed",
# "remains of children and women").
# ---------------------------------------------------------------------------

MASS_CASUALTY_PHRASES = re.compile(
    r"\b(?:mass\s+graves?|death\s+tolls?|massacre|killings)\b",
    re.IGNORECASE,
)

_CASUALTY_VICTIMS = (
    r"(?:people|civilians|children|women|men|villagers|residents|"
    r"soldiers|troops|officers|hostages|families|workers|miners|"
    r"pilots|passengers|students|staff)"
)

CASUALTY_COUNT_RE = re.compile(
    r"(?:(?:more\s+than|at\s+least|nearly|about|around|over|"
    r"up\s+to|almost|roughly|some)\s+)?"
    r"(\d[\d,]*|dozens|scores|hundreds|thousands|multiple|several)\s*"
    r"(?:" + _CASUALTY_VICTIMS + r"\s+)?"
    r"(killed|dead|died|deaths|fatalities|bodies|remains|casualties)\b",
    re.IGNORECASE,
)

PLURAL_CASUALTY_RE = re.compile(
    r"\b" + _CASUALTY_VICTIMS + r"\s+"
    r"(killed|dead|died|deaths|fatalities|bodies)\b",
    re.IGNORECASE,
)

BODIES_CONTEXT_RE = re.compile(
    r"\b(?:bodies|remains)\b"
    r"(?=[^.!?]{0,100}\b(?:grave|graves|exhumed|buried|unearthed|"
    r"recovered|found|children|women|men|civilians|villagers|"
    r"soldiers|troops|victims|killed|dead)\b)",
    re.IGNORECASE,
)

_NO_CASUALTY = re.compile(
    r"\b(?:no|zero|without|no\s+reported|no\s+known|denied|"
    r"no\s+confirmed)\s+"
    r"(?:bodies|remains|killings|fatalities|deaths|dead|killed|"
    r"casualties)\b",
    re.IGNORECASE,
)


def _casualty_count(value):
    """Smallest implied casualty count for a count token.
    Word counts map to conservative minimums; digits parse
    directly."""
    digits = re.sub(r"[^\d]", "", value or "")
    if digits:
        return int(digits)
    return {
        "several": 3,
        "multiple": 2,
        "dozens": 24,
        "scores": 40,
        "hundreds": 200,
        "thousands": 2000,
    }.get((value or "").lower(), 0)


def has_mass_casualty(text):
    """True when text carries strong, context-laden casualty
    evidence (a mass grave, a casualty count of two or more, or
    plural victims paired with a casualty word).  Explicit
    denials ("no fatalities", "zero bodies") never qualify, and
    a bare "death" or "die" is never enough."""
    text = text or ""
    if not text.strip():
        return False
    text = _NO_CASUALTY.sub(" ", text)
    if MASS_CASUALTY_PHRASES.search(text):
        return True
    for match in CASUALTY_COUNT_RE.finditer(text):
        if _casualty_count(match.group(1)) >= 2:
            return True
    if PLURAL_CASUALTY_RE.search(text):
        return True
    if BODIES_CONTEXT_RE.search(text):
        return True
    return False


# ---------------------------------------------------------------------------
# Enrichment driver
# ---------------------------------------------------------------------------

def enrich_thin_story_before_event_memory(
    item, cfg, now_dt, cache=None, fetcher=None,
    pace=None, robots=None, max_fetches=5,
):
    """C1: fetch the full article for a THIN story BEFORE event
    memory, so a weak RSS summary cannot anchor a weak standalone
    event identity (the cause of same-event splits - e.g. the
    Uttarakhand tunnel collapse posting twice because the Al
    Jazeera one-liner and the SCMP report never merged).

    Conditions (all must hold):
    - the story has a real article URL (not a non-article URL,
      domain allowlisted from the configured feeds)
    - its own RSS text carries fewer than 2 useful sentences
      (the same thinness definition the post-queue enrichment
      phase uses, computed without grouping)
    - the per-run pre-decide fetch budget is not exhausted

    On success the verbatim article sentences are attached as
    item["article_sentences"] AND folded into item["summary"], so
    decide() builds the event identity from real facts (killed /
    tunnel / location / named project) instead of the thin RSS
    one-liner.  The post-queue enrichment phase then sees a
    non-thin story (its briefing includes the article sentences)
    and skips it, and the DB-backed cache prevents any re-fetch.

    Returns (item, stats).  Additive only: on any failure the
    item is returned unchanged and the outcome is cached, so the
    pipeline never fails and never re-fetches the same URL.
    """
    stats = {
        "checked": 0,
        "thin": 0,
        "eligible": 0,
        "non_article": 0,
        "domain_blocked": 0,
        "cache_hit": 0,
        "fetched": 0,
        "ok": 0,
        "expanded": 0,
        "not_expanded": 0,
        "budget_exhausted": 0,
    }
    art_cfg = cfg.get("article_extraction") or {}
    if not art_cfg.get("enabled", True):
        return item, stats

    stats["checked"] += 1
    url = item.get("url")
    headline = item.get("title") or ""
    if not url:
        return item, stats

    # Thinness: the story's OWN RSS text carries fewer than two
    # useful explanatory sentences (same filter the briefing
    # pipeline applies, without grouping).
    from src.telegram_briefing import count_meaningful_sentences
    if count_meaningful_sentences(
        item.get("summary") or "", headline
    ) >= 2:
        return item, stats
    stats["thin"] += 1

    if non_article_url(url, art_cfg.get("non_article_segments")):
        stats["non_article"] += 1
        return item, stats
    allowlist = feed_domain_allowlist(cfg.get("feeds"))
    if not domain_allowed(url, allowlist):
        stats["domain_blocked"] += 1
        return item, stats

    fetcher = fetcher or fetch_article
    robots = robots or _RobotCache()
    pace = pace or _PaceLock(
        art_cfg.get("min_domain_interval_seconds", 1),
    )

    if cache is not None:
        entry = cache.get(item.get("story_id") or item.get("id"), now=now_dt)
        if entry is not None:
            stats["cache_hit"] += 1
            sentences = cache_sentences(entry)
            if entry.get("status") == OK_STATUS and len(sentences) >= 2:
                item["article_sentences"] = sentences
                item["summary"] = " ".join(sentences)
                stats["expanded"] += 1
            return item, stats

    if max_fetches <= 0:
        stats["budget_exhausted"] += 1
        return item, stats
    stats["fetched"] += 1
    try:
        status, payload = fetcher(
            url, art_cfg, allowlist, robots=robots, pace=pace
        )
    except Exception:
        status, payload = ("network_error", {})
    stats[status] = stats.get(status, 0) + 1

    sentences = []
    if status == OK_STATUS:
        if article_junk_ratio(payload.get("text", "")) > 0.5:
            status = "junk"
        else:
            sentences = article_sentences(
                payload.get("text", ""),
                headline,
                int(art_cfg.get("max_article_sentences", 12)),
            )
    if cache is not None:
        cache.set(
            item.get("story_id") or item.get("id"),
            url,
            status,
            text=payload.get("text") if status == OK_STATUS else None,
            sentences=sentences if status == OK_STATUS else None,
            title=payload.get("title") if status == OK_STATUS else None,
            ttl_ok=int(art_cfg.get("cache_ttl_hours_ok", 48)),
            ttl_error=int(art_cfg.get("cache_ttl_hours_error", 24)),
            now=now_dt,
        )

    if status == OK_STATUS and len(sentences) >= 2:
        item["article_sentences"] = sentences
        item["summary"] = " ".join(sentences)
        stats["expanded"] += 1
    else:
        stats["not_expanded"] += 1
    return item, stats


def enrich_thin_stories(
    candidates, cfg, now_dt, cache=None, fetcher=None
):
    """Fetch original articles for important thin stories and
    attach verbatim sentences as candidate["article_sentences"].
    The briefing pipeline then decides what actually renders.

    Groups and primaries are resolved exactly like
    build_telegram_stories, so sentences always land on the
    story that renders the post for the event.

    Returns (enriched_candidates, stats).  cfg is the full
    pipeline config (CONFIG); reads "article_extraction" and
    "feeds".  cache/fetcher are injectable for tests.
    """
    stats = {
        "enabled": bool(
            (cfg.get("article_extraction") or {})
            .get("enabled", True)
        ),
        "candidates": len(candidates),
        "eligible": 0,
        "thin": 0,
        "important": 0,
        "mass_casualty": 0,
        "non_article": 0,
        "domain_blocked": 0,
        "cache_hits": 0,
        "fetched": 0,
        "ok": 0,
        "http_error": 0,
        "blocked": 0,
        "paywall": 0,
        "no_text": 0,
        "junk": 0,
        "timeout": 0,
        "too_large": 0,
        "not_html": 0,
        "network_error": 0,
        "expanded": 0,
        "not_expanded": 0,
        "budget_exhausted": 0,
    }
    art_cfg = cfg.get("article_extraction") or {}
    if not art_cfg.get("enabled", True):
        return candidates, stats

    from src.telegram_briefing import (
        build_briefing,
        group_items,
        public_label,
        URGENT_CATEGORIES,
    )

    telegram_cfg = cfg.get("telegram") or {}
    just_in_minutes = int(
        telegram_cfg.get("just_in_freshness_minutes", 15)
    )
    allowlist = feed_domain_allowlist(cfg.get("feeds"))

    # Resolve groups and primaries exactly like
    # build_telegram_stories, so the enriched candidate is
    # the story that actually renders the post.
    eligible = []
    for group in group_items(candidates):
        primary = sorted(
            group,
            key=lambda x: (
                x.get("score", 0)
                or x.get("priority_score", 0)
                or 0,
                int(bool(x.get("primary_source"))),
                -int(x.get("tier", 4)),
            ),
            reverse=True,
        )[0]

        # Importance gate: IMMEDIATE, JUST IN, score >= 65 or
        # an update.  Additionally, a thin HIGH-priority story
        # scoring 60-64 may qualify when it carries strong
        # mass-casualty evidence in a serious/conflict/disaster/
        # major-event category (the Sudan mass-grave case), as
        # long as the thinness gate below still holds.
        #
        # Reputable-source carve-out: a thin HIGH-priority story
        # (score >= 60) from a reputable source (tier 1-2, an
        # official source, or a primary/wire source) qualifies
        # for article enrichment even below the 65 gate, so a
        # thin RSS summary is recovered from the full article
        # instead of being rejected outright.  The thinness
        # gate, non-article URL gate, domain allowlist and
        # per-run fetch budget below still apply unchanged, and
        # enrichment only ever ADDS verbatim article sentences
        # that must survive the same briefing/summarizer gates.
        label = public_label(
            primary, just_in_minutes, now_dt
        )
        score = primary.get("score") or 0
        mass_casualty = (
            60 <= score <= 64
            and primary.get("priority_level") == "HIGH"
            and primary.get("category") in URGENT_CATEGORIES
            and has_mass_casualty(
                f"{primary.get('title', '')} "
                f"{primary.get('summary', '')}"
            )
        )
        reputable_source = (
            source_reliability.get_tier(primary) in (1, 2)
            or bool(primary.get("primary_source"))
        )
        important = (
            primary.get("priority_level") == "IMMEDIATE"
            or label == JUST_IN
            or score >= 65
            or primary.get("event_status") == "UPDATE"
            or mass_casualty
            or (
                reputable_source
                and score >= 60
                and primary.get("priority_level") == "HIGH"
            )
        )
        if not important:
            continue
        if mass_casualty:
            stats["mass_casualty"] += 1
        stats["important"] += 1

        # Thinness gate: fewer than two useful sentences in
        # the event briefing (the same content the message
        # renders, without any article expansion).
        briefing = build_briefing(
            primary,
            group,
            just_in_minutes,
            now_dt,
        )
        if len(briefing["sentences"]) >= 2:
            continue
        stats["thin"] += 1
        eligible.append(primary)

    fetcher = fetcher or fetch_article
    max_fetches = int(art_cfg.get("max_fetches_per_run", 15))
    pace = _PaceLock(
        art_cfg.get("min_domain_interval_seconds", 1),
    )
    robots = _RobotCache()

    expanded = []
    stats["eligible"] = len(eligible)
    fetched_count = 0

    for cand in eligible:
        url = cand.get("url")
        headline = cand.get("title") or ""
        if non_article_url(
            url, art_cfg.get("non_article_segments")
        ):
            stats["non_article"] += 1
            continue
        if not domain_allowed(url, allowlist):
            stats["domain_blocked"] += 1
            continue

        entry = (
            cache.get(cand.get("story_id"), now=now_dt)
            if cache
            else None
        )
        if entry is not None:
            stats["cache_hits"] += 1
            if entry["status"] == OK_STATUS:
                sentences = cache_sentences(entry)
                if len(sentences) >= 2:
                    cand["article_sentences"] = sentences
                    stats["expanded"] += 1
                    expanded.append(cand)
                else:
                    stats["not_expanded"] += 1
            elif entry["status"] == "non_article":
                stats["non_article"] += 1
            else:
                stats[entry["status"]] = stats.get(
                    entry["status"], 0
                ) + 1
            continue

        if fetched_count >= max_fetches:
            stats["budget_exhausted"] += 1
            continue
        fetched_count += 1

        try:
            status, payload = fetcher(
                url, art_cfg, allowlist, robots=robots, pace=pace
            )
        except Exception:
            status, payload = ("network_error", {})
        stats["fetched"] += 1
        stats[status] = stats.get(status, 0) + 1

        sentences = []
        if status == OK_STATUS:
            # Post-extraction cleanliness gate: a page whose
            # extracted text is mostly chrome (menus, promos,
            # captions, product metadata) is junk, never
            # article enrichment, and is cached negatively.
            if article_junk_ratio(
                payload.get("text", "")
            ) > 0.5:
                status = "junk"
            else:
                sentences = article_sentences(
                    payload.get("text", ""),
                    headline,
                    int(art_cfg.get("max_article_sentences", 12)),
                )
        if cache is not None:
            cache.set(
                cand.get("story_id"),
                url,
                status,
                text=payload.get("text") if status == OK_STATUS
                else None,
                sentences=sentences if status == OK_STATUS
                else None,
                title=payload.get("title") if status == OK_STATUS
                else None,
                ttl_ok=int(
                    art_cfg.get("cache_ttl_hours_ok", 48)
                ),
                ttl_error=int(
                    art_cfg.get("cache_ttl_hours_error", 24)
                ),
                now=now_dt,
            )

        if status == OK_STATUS and len(sentences) >= 2:
            cand["article_sentences"] = sentences
            stats["expanded"] += 1
            expanded.append(cand)
        else:
            stats["not_expanded"] += 1

    return candidates, stats
