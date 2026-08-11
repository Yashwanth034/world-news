# WorldNews Telegram

Automated news collection and Telegram publishing. A pipeline gathers stories from
42 RSS feeds, enriches them into source-grounded briefings, and publishes them to a
Telegram channel — optionally with an image or video attachment.

## Features

- **News gathering** — 42 RSS feeds across world, finance, technology, science,
  space, health, disaster, cybersecurity, and regional categories.
- **Event grouping** — stories about the same event are merged into a single post;
  conflicting or near-duplicate facts are dropped, never reconciled.
- **Source-grounded summaries** — each post carries 2–8 explanatory sentences
  composed from verbatim source text, verified against the source, and
  quality-checked. No text is invented.
- **Automatic Telegram publishing** — scheduling, throttling (hourly/daily caps,
  minimum gap), retries, and rate-limit handling.
- **Media attachment** — one image (preferred) or video is attached when suitable
  media is available; the caption is always the exact existing message text.

## How it works

1. **Collect** — `src/main.py` fetches all configured feeds, applies deduplication,
   language detection, classification, scoring, optional translation, and a quality
   gate, then writes a Telegram candidate queue (`data/telegram_queue.json`).
2. **Enrich** — thin important stories are enriched with article text, candidates
   are grouped into events, and a 2–8 sentence source-grounded summary is composed
   for each event.
3. **Publish** — `src/telegram_run.py` schedules due stories (breaking posts
   immediately, others on a randomized delay) and sends them through the Telegram
   Bot API, respecting posting caps and minimum gaps, with retries and bounded
   handling of rate limits. Before sending each post, the scheduler attempts to
   attach suitable article media.

Message format:

```
🚨 BREAKING          (or ⚡ JUST IN / 📰 NEWS / 🔄 UPDATE)

**Headline**

2–8 explanatory sentences

📰 Source: <source name>
🔗 Read the full report
```

## Media attachment

For each post, the scheduler tries to attach **one** image (preferred) or video from
the article page. Only public, page-linked media is fetched; logos, icons, ads, and
tiny thumbnails are rejected.

- The **caption is always the exact same text** as the text-only version of the post.
- The post falls back to **text-only** when: no media is found, the media is
  unsuitable or fails to download, the caption exceeds Telegram's 1024-character
  media caption limit, or Telegram rejects the media send (the message is then
  resent as text-only).
- A media failure never blocks, retries, or changes the story text.

## GitHub Actions automation

- **`telegram.yml`** — the production workflow. Runs every 15 minutes: restores the
  persistent dedup/event-memory database from the Actions cache, collects fresh
  news, publishes Telegram posts, and commits the queue/state files back to `main`.
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

Real publishing happens only with `TELEGRAM_PUBLISH=1` or `--force`; `--yes` skips
the confirmation prompt.

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

Optional live-send tests (opt-in):

```bash
TELEGRAM_LIVE=1 .venv/bin/python -m pytest src/test_telegram_send.py -q
```

## Limitations

- **At-least-once delivery.** State is committed only after publishing; if a run
  fails between a successful send and the state commit, a story may be re-published
  on the next run.
- **Media is best-effort.** Posts without a suitable image/video, or with captions
  over 1024 characters, are published text-only.
- **Single channel.** Posts go to one channel configured at run time.
- **Translation relies on third-party endpoints** whose availability is not
  guaranteed.
