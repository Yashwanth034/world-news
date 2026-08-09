"""Optional live-send tests for the Telegram publisher.

These only run when the environment opts in explicitly:

    TELEGRAM_LIVE=1 .venv/bin/python -m pytest \
        src/test_telegram_send.py -q

Without TELEGRAM_LIVE the tests are skipped. Use --force
publishing on a personal channel for the real end-to-end
check.
"""
import os
import sys

import pytest

from src.telegram_publisher import (
    TelegramPublisher,
    TelegramPublisherError,
)

LIVE = os.environ.get("TELEGRAM_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="set TELEGRAM_LIVE=1 to run live-send tests",
)

TEST_TEXT = "opencode telegram integration test"
TEST_HTML = "<b>opencode</b> telegram <i>integration</i> test"


def publisher():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    channel = os.environ.get(
        "TELEGRAM_CHANNEL_ID",
        "",
    )
    return (
        TelegramPublisher(token=token),
        channel,
    )


def test_token_rejected():
    bot, _ = publisher()
    bad = TelegramPublisher(token="invalid-token-123")
    with pytest.raises(TelegramPublisherError):
        bad.get_me()


def test_plain_text_send():
    bot, channel = publisher()
    assert channel, "TELEGRAM_CHANNEL_ID required"
    result = bot.send_message(
        channel,
        {"text": TEST_TEXT, "parse_mode": None},
    )
    assert result.get("message_id")


def test_html_send():
    bot, channel = publisher()
    assert channel, "TELEGRAM_CHANNEL_ID required"
    result = bot.send_message(
        channel,
        {"text": TEST_HTML, "parse_mode": "HTML"},
    )
    assert result.get("message_id")


def test_dry_run_sends_nothing():
    bot, channel = publisher()
    assert channel, "TELEGRAM_CHANNEL_ID required"
    result = bot.send_message(
        channel,
        {"text": TEST_TEXT, "parse_mode": None},
        dry_run=True,
    )
    assert result["dry_run"]
    assert "message_id" not in result


def test_end_to_end_build_and_send():
    import json
    from pathlib import Path

    from src.telegram_formatter import build_message

    bot, channel = publisher()
    assert channel, "TELEGRAM_CHANNEL_ID required"

    root = Path(__file__).resolve().parent.parent
    cfg = json.loads(
        (root / "config.json").read_text()
    )

    queue = json.loads(
        (
            root
            / cfg.get("telegram", {}).get(
                "telegram_queue_file",
                "data/telegram_queue.json",
            )
        ).read_text()
    )

    items = [
        x
        for x in queue.get("stories", [])
        if x.get("title")
    ]

    if not items:
        print(
            "SKIP: no stories in telegram queue file",
            file=sys.stderr,
        )
        pytest.skip(
            "telegram queue file empty"
        )

    item = items[0]

    message = build_message(
        item,
        cfg.get("telegram", {}),
    )
    assert message is not None

    result = bot.send_message(
        channel,
        message,
    )
    assert result.get("message_id")
