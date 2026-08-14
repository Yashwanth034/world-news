# WorldNews

Automated worldwide-news collection and Telegram publishing. A pipeline gathers stories
from 51 verified RSS feeds (global agencies, regional publishers and specialized
primary sources), filters them editorially, groups same-event coverage, enriches
important thin stories with article text, composes a concise source-grounded 2–4
sentence summary per event, and publishes to a Telegram channel — optionally with an
image or video attachment.

Telegram is the only publishing destination. There is no X/Twitter integration and no
legacy X post/thread formatting anywhere in the pipeline.

## Features

- **News gathering** — 51 verified RSS feeds across world, politics, finance,
  technology, science, space, health, disaster, cybersecurity, conflict, environment
  and regional categories.
- **Website-ready data model** — every story and event is stored with structured
  sector / sub-sector / region / country / entities / timestamps / related-sources /
  verification metadata (see “Database schema” below). This powers coverage audits
  today and is the foundation for a future WorldNews website; it is never shown on
  Telegram.
- **Editorial eligibility filter** — product reviews, buying guides, opinion columns,
  personal essays, how-tos, sponsored/affiliate content, listicles, quizzes, recipes and
  routine (non-extreme) weather are rejected before they reach the queue. Real science,
  sports, technology, culture and environment news is not filtered out: only clearly
  non-news formats are.
- **Event grouping** — stories about the same event are merged into a single post;
  conflicting or near-duplicate facts are dropped, never reconciled. Same-event
  duplicates are suppressed while genuinely new developments still publish as updates.
- **Source-grounded summaries** — each post carries **2–4 explanatory sentences**
  (headline, source line and "Read the full report" never count). Sentences are
  selected by fact importance (what happened, where/when, how many, consequence),
  ordered into a natural narrative, verified verbatim against the source, checked for
  headline/body consistency, and quality-gated. No text is ever invented; a story with
  fewer than two genuinely useful sentences is rejected rather than padded.
- **Headline/body consistency** — a headline claiming a score, a win, deaths, or a
  confirmed/announced development is rejected when the source only supports a weaker
  claim (draw, loss, injuries, "reportedly").
- **Label discipline** — `🚨 BREAKING`, `⚡ JUST IN`, `🔄 UPDATE` and `📰 NEWS` are
  assigned from importance + freshness + verification, never from mere fetch recency.
- **Automatic Telegram publishing** — scheduling, throttling (hourly/daily caps,
  minimum gap), retries, and rate-limit handling.
- **Media attachment** — one image (preferred) or video is attached when suitable
  media is available; the caption is always the exact existing message text, and a
  media failure never blocks publishing.
- **Atomic state writes** — queue/state JSON files are written via temp file + fsync +
  atomic replace so an interrupted run can never leave a partially written file.

## How it works

1. **Collect** — `src/main.py` fetches all configured feeds, applies dedup, language
   detection, topic classification, the editorial eligibility filter, reliability
   scoring, optional translation, and priority, then writes a Telegram candidate queue
   (`data/telegram_queue.json`).
2. **Enrich** — thin important stories are enriched with cleaned article text,
   candidates are grouped into events, and a 2–4 sentence source-grounded summary is
   composed for each event (article text primary when available, RSS otherwise).
3. **Publish** — `src/telegram_run.py` schedules due stories (breaking posts
   immediately, others on a randomized delay) and sends them through the Telegram Bot
   API, respecting posting caps and minimum gaps, with retries and bounded handling of
   rate limits. Before sending each post, the scheduler attempts to attach suitable
   article media.

Message format:

```
WorldNews🌎: 🚨 BREAKING     (or ⚡ JUST IN / 🔄 UPDATE / 📰 NEWS)

**Headline**

2–4 concise explanatory sentences that clearly explain
what happened and why it matters.

📰 Source: <source name>
🔗 Read the full report
```

The body is capped at four sentences and never padded: a story that is clearly
explained in two sentences publishes two. No internal fields (scores, confidence,
event IDs, corroboration counts) are ever exposed in the message.

## Media attachment

For each post, the scheduler tries to attach **one** image (preferred) or video from
the article page. Only public, page-linked media is fetched; logos, publisher icons,
avatars, ads, tracking pixels, tiny thumbnails and unrelated recommendation images are
rejected. `twitter:image` is used as ordinary webpage metadata for locating article
images — it is not X publishing.

- The **caption is always the exact same text** as the text-only version of the post.
- The post falls back to **text-only** when: no media is found, the media is
  unsuitable or fails to download, the caption exceeds Telegram's 1024-character media
  caption limit, or Telegram rejects the media send (the message is then resent as
  text-only).
- A media failure never blocks, retries, or changes the story text.

## Configuration

`config.json` holds all pipeline settings. Key entries:

| Entry | Purpose |
| --- | --- |
| `feeds` | RSS sources; `"news": false` feeds are still collected and stored but never become Telegram posts; `"discovery": true` feeds require independent confirmation |
| `database` | SQLite file for dedup + event memory (`data/news.db`) |
| `queue_file` | **Internal** pipeline diagnostics queue (`data/queue.json`) — the Telegram scheduler never reads this file |
| `telegram.telegram_queue_file` | The Telegram output queue (`data/telegram_queue.json`) read by the scheduler |
| `telegram.telegram_state_file` | Published-post state (`data/telegram_state.json`) |
| `summarization` | `min_sentences: 2`, `max_sentences: 4` — the final body's explanatory-sentence limits |
| `article_extraction` | Fetch/cleanup budget for thin-story enrichment (`max_fetches_per_run`, timeouts, cache TTLs, junk/paywall markers) |
| `telegram` | Freshness window, hourly/daily caps, minimum gap, delays, message length targets (`target_message_chars: 950`, `max_message_chars: 1500`) |

There is exactly one Telegram queue (`data/telegram_queue.json`). The legacy
`data/queue.json` entry is now documented as the internal pipeline diagnostics queue.

## GitHub Actions automation

- **`telegram.yml`** — the production workflow. Runs every 15 minutes: restores the
  persistent dedup/event-memory database from the Actions cache, collects fresh news,
  publishes Telegram posts, and commits the queue/state files back to `main`. State is
  committed even when a publish attempt fails, narrowing the double-publish window.
- **`telegram-test-one.yml`** — a manual workflow that verifies the scheduler state
  is exactly one due post, then publishes exactly one message. Triggered only via
  `workflow_dispatch`.

## Setup

Requirements: Python 3.11.

```bash
git clone https://github.com/Yashwanth034/world-news-telegram.git
cd world-news-telegram
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then configure secrets (see below) and run:

```bash
.venv/bin/python -m src.main                 # collect news + build Telegram queue
.venv/bin/python -m src.telegram_run --dry-run   # preview, sends nothing
.venv/bin/python -m src.telegram_run --yes       # publish (requires TELEGRAM_PUBLISH=1 or --force)
```

Real publishing happens only with `TELEGRAM_PUBLISH=1` or `--force`; `--yes` confirms
the prompt (declining with "n" or Ctrl-C aborts).

## Environment variables / secrets

| Variable | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes (to publish) | Telegram bot token; never stored in files |
| `TELEGRAM_CHANNEL_ID` | Yes (to publish) | Channel to publish to |
| `TELEGRAM_PUBLISH` | — | `1` enables real publishing |
| `TELEGRAM_NO_RETRY` | — | `1` disables retries (one-shot mode) |
| `TRANSLATE_ENDPOINTS` | — | Comma-separated translation endpoints (defaults built in) |

For CI, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHANNEL_ID` as GitHub Actions
secrets. The bot token is read only from the environment and never from files.

## Testing

```bash
.venv/bin/python -m pytest src/test_*.py -q
```

The suite covers: editorial filtering (reviews/guides/opinion rejected, breaking news
accepted), summary length (2/3/4 accepted, 5+ reduced to 4, 1 rejected), headline/body
consistency, deduplication and same-event grouping, label classification
(BREAKING/JUST IN/UPDATE/NEWS), date freshness, media selection, and exact Telegram
message formatting.

Optional live-send tests (opt-in):

```bash
TELEGRAM_LIVE=1 .venv/bin/python -m pytest src/test_telegram_send.py -q
```

## Database schema

The SQLite database (`data/news.db`, configured by `database` in `config.json`) is the
single source of truth for dedup and event memory, and is designed to be queryable by a
future website. Schema ownership: `src/storage.py` owns the *storage* schema of both
tables; `src/event_memory.py` owns the *matching/identity* rules and is never changed by
schema work.

**`stories`** — one row per collected article:

`id, title, url, source, category, summary, score, confidence, event_id, event_status,
first_seen, sector, subsector, region, subregion, country, entities (JSON),
published_at, updated_at, event_time, last_seen, verification (JSON)`

**`events`** — one row per canonical event:

`event_id, canonical_title, category, first_seen, last_seen, major, queued_count,
canonical_summary, canonical_state (JSON), sector, subsector, region, subregion,
country, entities (JSON), event_time, last_development, related_sources (JSON),
verification (JSON)`

Semantics:

- `sector` / `subsector` come from the content-based taxonomy (`src/sectors.py`);
  `region` / `subregion` from `src/regions.py`. Event rows are anchored to the
  canonical (first) story — a later article about a sub-aspect of the same event never
  retags the event.
- `entities` are distinctive named entities (people, companies, institutions, named
  storms, ships, …), extracted by the same signal machinery event memory uses; generic
  words, weekdays, months and years are excluded.
- `event_time` is the best available timestamp for *when the event happened*;
  `last_development` is the latest **meaningful** development time. A duplicate
  article never advances `last_development`; only a genuine `UPDATE` (material
  development detected by event memory) does. `updated_at`/`last_seen` are record
  timestamps and are deliberately separate.
- `related_sources` accumulates the sources that reported an event; `verification`
  holds tier / primary-source / corroboration counts. Both are backend/audit metadata
  only — never rendered on Telegram.
- Indexes exist on `events(event_time, last_development, sector, region, country,
  major)` so future queries (“all cybersecurity events”, “all events in India”, “events
  updated in the last 24 hours”) run without scanning raw text.

### Migration procedure

Migrations are **in-place, additive and idempotent**: `src/storage.py`
(`init_schema`) creates both tables with the full column set on fresh databases and
adds missing columns (`ALTER TABLE … DEFAULT NULL`) to older databases. Running it
repeatedly is a no-op and existing rows are never deleted or rewritten. Historical rows
predating a field keep `NULL` (unknown) rather than a fabricated value. The pipeline
runs the migration automatically on every collection cycle; no manual step is needed.

### Fresh database procedure

Delete `data/news.db` (and the generated `data/queue.json`, `data/source_health.json`)
and run the pipeline once; the schema is recreated from scratch. Fresh databases are
also created automatically when the file does not exist.

## Event timeline concept

Each event carries `first_seen` (when the event entered memory), `event_time` (when it
happened) and `last_development` (latest meaningful development). Together they let a
future website render a per-event timeline ordered by real-world development time,
with the canonical title, accumulated entities, and every related source preserved
without losing the immutable event identity.

## Source-health history

Every run records per-source quality metrics into the same SQLite database
(`source_health` table): fetch attempts / successes / failures, articles fetched /
accepted / rejected / deduplicated / editorial-rejected / summarized, last success /
failure timestamps and a **safe error classification** (`HTTP_403`, `HTTP_404`,
`TIMEOUT`, `DNS_ERROR`, `PARSE_ERROR`, `CONNECTION_ERROR`, `OTHER` — raw error
bodies, URLs and headers are never stored).

History is **additive**: counters accumulate across runs (a source that fails once
and succeeds once shows `attempt_count=2, failure_count=1, success_count=1`) and
`last_success` / `last_failure` / `last_error` always reflect the most recent
occurrence. Metric definitions:

- `success_rate` = successful fetches / fetch attempts (per source, all runs)
- `failure_rate` = failed fetches / fetch attempts
- `useful_news_rate` = useful unique events / articles fetched (per run)
- `duplicate_rate` = duplicate articles attributable to the source / articles fetched

All rates guard against division by zero. The metrics are measurement-only: nothing
in collection, dedup, scoring or publishing reads them yet.

## Coverage audit

`python -m src.audit_source_coverage --config config.json [--live] [--db data/news.db]
[--json]` audits the source network. It reports source coverage, sector and regional
coverage, failure rate, publisher redundancy, overrepresented / underrepresented /
low-value / failing sources, **sector and regional source coverage** (how many sources
are configured vs successful vs useful per sector/region), and **unique-event
contribution** ranked per source.

With `--db` the audit also reads the persistent source-health history and renders a
SOURCE HEALTH HISTORY section (attempts, success/failure counts, rates, last error).
With `--json` it prints a stable machine-readable report (suitable for later
automation); `--out FILE` writes it to disk.

### Publisher concentration

The report includes source-concentration statistics, computed on **unique useful
events per source** (and separately on raw fetched volume):

- top-1 / top-3 / top-5 / top-10 source share — the fraction of all useful events
  contributed by the largest 1/3/5/10 sources
- **HHI** (Herfindahl-Hirschman Index) — sum of squared shares, on a 0–1 scale:
  below 0.15 is low concentration (healthy diversity), 0.15–0.25 moderate, above
  0.25 high (one or two sources dominate). The denominator is documented in the
  report (unique useful events per source).

A publisher that posts hundreds of articles can still contribute few unique events,
so both raw-volume and unique-event concentration are reported.

### Inspecting source quality

`sqlite3 data/news.db "SELECT source_id, attempt_count, success_count, failure_count,
articles_fetched, articles_accepted, summarized_count, last_error FROM source_health
ORDER BY failure_count DESC"` shows the full per-source history; the audit's
UNIQUE-EVENT CONTRIBUTION and SOURCE HEALTH HISTORY sections surface the same data
with rates. Low-volume specialized sources are not penalized: a source with 5
high-value events is valuable, not underrepresented — the audit only flags sources
that fetch real volume and produce zero useful events.

## Importance model

The queue is ranked by a transparent, evidence-based importance score
(`src/importance.py`), not a keyword sum.  Severity comes from the actual facts:

| Component | Max | Meaning |
|---|---|---|
| impact | 34 | Magnitude + casualties + context: M7.5 in a populated area ≫ M2 tremor; 1000+ dead ≫ 10 dead; plane crash / dam collapse / bank failure floor |
| urgency | 10 | Breaking/developing wording + age (<1h, <6h, <24h) |
| novelty | 10 | Event-level: NEW = 10, UPDATE = 8, DUPLICATE = 0 |
| scope | 8 | Local / National / Regional / Multi-country / Global, from content |
| reliability | 8 | Source tier + primary-source flag (trust, never importance by itself) |
| corroboration | 10 | Independent strong corroboration (syndication is not overcounted) |
| significance | 40 | Six bounded dimensions (human/economic/security/scientific/infrastructure/geopolitical), tiered strong=8/weak=4 terms + fact-driven human scale |
| coverage adjustment | +3 | Soft nudge for under-covered sectors; can win a tie, can never lift a 50-point story over a 90-point one |

Levels: **CRITICAL ≥ 85, HIGH ≥ 70, MEDIUM ≥ 50, LOW < 50.**  Importance is
**event-level**: the event's canonical material (title + accumulated summary) is
passed in, so a thin follow-up article inherits the event's severity facts ("M7.4
quake, 281 dead") and cannot demote a genuinely major event by accident.  Every
candidate carries an explainable `importance_breakdown` dict (debug/audit only,
never shown on Telegram).  No permanent sector priority exists: ranking comes from
evidence, and labels (BREAKING / JUST IN / UPDATE / NEWS) remain separate from the
importance level.  The legacy `priority_score` is still computed for the Telegram
scheduler's delay logic; `importance_score` drives queue order.

## Limitations

- **At-least-once delivery.** State is committed only after publishing; if a run
  fails between a successful send and the state commit, a story may be re-published
  on the next run. Known successful Telegram `message_id`s are stored and skipped to
  narrow this window.
- **Media is best-effort.** Posts without a suitable image/video, or with captions
  over 1024 characters, are published text-only.
- **Single channel.** Posts go to one channel configured at run time.
- **Translation relies on third-party endpoints** whose availability is not
  guaranteed.
