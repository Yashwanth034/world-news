import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
PINNED_ACTION = re.compile(
    r"uses:\s+actions/(?:checkout|setup-python|cache(?:/restore|/save)?)@"
    r"([0-9a-f]{40})(?:\s+#.*)?"
)


def _text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _assert_hardened(text):
    assert "contents: write" not in text
    assert "contents: read" in text
    assert "git push" not in text
    assert "git pull" not in text
    assert "git commit" not in text
    assert "secrets." not in text.split("jobs:", 1)[0]

    action_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("uses: actions/")
    ]
    assert action_lines
    assert all(PINNED_ACTION.fullmatch(line) for line in action_lines)


def test_production_workflow_uses_cache_backed_state_without_main_push():
    text = _text("telegram.yml")
    _assert_hardened(text)
    assert "actions/cache/restore@" in text
    assert "actions/cache/save@" in text
    assert "data/news.db" in text
    assert "data/telegram_queue.json" in text
    assert "data/telegram_state.json" in text


def test_manual_workflow_does_not_bypass_branch_protection():
    text = _text("telegram-test-one.yml")
    _assert_hardened(text)
    assert "actions/cache/restore@" in text
    assert "actions/cache/save@" in text
