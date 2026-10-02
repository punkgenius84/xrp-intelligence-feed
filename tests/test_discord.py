from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

import main
from discovery.dispatch import DiscoveryDispatchError, DiscoveryRegistryError
from sources.registry import SourceRegistryError
from discord.publisher import (DEFAULT_MAX_POSTS, DiscordSettings, format_message, publish,
                               settings_from_env)
from discord.webhook import DiscordError, DiscordWebhook, is_valid_webhook_url
from models import NewsItem
from storage.database import JsonState

TOKEN = "SECRET-token_abc123"
URL = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"
BASE = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status=204, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeHook:
    def __init__(self, fail_on=()):
        self.sent = []
        self.fail_on = set(fail_on)

    def send(self, content):
        index = len(self.sent)
        self.sent.append(content)
        if index in self.fail_on:
            raise DiscordError("Discord returned HTTP 500")


def item(title="FinCEN issues digital asset guidance", score=50, minutes=0, url=None, **changes):
    return NewsItem(title=title, url=url or f"https://example.test/{title.replace(' ', '-')}",
                    source="FinCEN", published_at=BASE + timedelta(minutes=minutes),
                    relevance_score=score, detected_entities=["FinCEN", "XRP"],
                    score_reasons=["digital asset signal", "regulatory action", "third reason"],
                    **changes)


# ---- webhook client ----------------------------------------------------------------------

def test_send_posts_json_with_mentions_suppressed_and_content_truncated():
    session = FakeSession(FakeResponse(204))
    DiscordWebhook(URL, session=session).send("@everyone " + "x" * 5000)
    url, kwargs = session.calls[0]
    assert url == URL
    assert kwargs["timeout"] == 20
    assert kwargs["json"]["allowed_mentions"] == {"parse": []}
    assert len(kwargs["json"]["content"]) == 2000


@pytest.mark.parametrize("value", [
    "", "http://discord.com/api/webhooks/1/abc", "https://evil.example/api/webhooks/1/abc",
    "https://discord.com.evil.example/api/webhooks/1/abc", "https://discord.com/api/webhooks/x/abc",
    "https://discord.com/api/webhooks/1/abc?redirect=https://evil.example",
])
def test_invalid_webhook_urls_are_rejected_without_any_request(value):
    session = FakeSession()
    assert is_valid_webhook_url(value) is False
    with pytest.raises(DiscordError):
        DiscordWebhook(value, session=session).send("hello")
    assert session.calls == []


@pytest.mark.parametrize("value", [
    URL, URL + "/", "https://discordapp.com/api/webhooks/1/abc-DEF_1",
    "https://canary.discord.com/api/v10/webhooks/1/abc",
])
def test_valid_webhook_urls_are_accepted(value):
    assert is_valid_webhook_url(value) is True


def test_http_error_message_never_contains_webhook_url_or_token():
    with pytest.raises(DiscordError) as caught:
        DiscordWebhook(URL, session=FakeSession(FakeResponse(404))).send("hello")
    assert "404" in str(caught.value)
    assert TOKEN not in str(caught.value) and "webhooks" not in str(caught.value)


def test_network_error_is_sanitized_and_not_chained():
    boom = requests.ConnectionError(f"Max retries exceeded with url: {URL}")
    with pytest.raises(DiscordError) as caught:
        DiscordWebhook(URL, session=FakeSession(boom)).send("hello")
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_rate_limit_is_retried_once_after_the_requested_wait():
    slept = []
    session = FakeSession(FakeResponse(429, {"retry_after": 1.5}), FakeResponse(204))
    DiscordWebhook(URL, session=session, sleep=slept.append).send("hello")
    assert slept == [1.5] and len(session.calls) == 2


def test_rate_limit_with_long_or_unknown_wait_is_not_retried():
    for response in (FakeResponse(429, {"retry_after": 600}), FakeResponse(429)):
        session = FakeSession(response)
        with pytest.raises(DiscordError, match="429"):
            DiscordWebhook(URL, session=session, sleep=lambda _: None).send("hello")
        assert len(session.calls) == 1


def test_second_rate_limit_after_retry_fails():
    session = FakeSession(FakeResponse(429, {"retry_after": 1}), FakeResponse(429, {"retry_after": 1}))
    with pytest.raises(DiscordError, match="429"):
        DiscordWebhook(URL, session=session, sleep=lambda _: None).send("hello")
    assert len(session.calls) == 2


def test_missing_webhook_raises_clear_error():
    with pytest.raises(DiscordError, match="not configured"):
        DiscordWebhook("", session=FakeSession()).send("hello")


# ---- settings ----------------------------------------------------------------------------

def test_settings_defaults_when_nothing_is_set():
    assert settings_from_env({}) == DiscordSettings("", DEFAULT_MAX_POSTS, False)


def test_settings_read_and_trim_values():
    settings = settings_from_env({"DISCORD_WEBHOOK_URL": f"  {URL}\n", "DISCORD_MAX_POSTS": " 3 ",
                                  "DISCORD_DRY_RUN": "TRUE"})
    assert settings == DiscordSettings(URL, 3, True)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_dry_run_truthy_values(value):
    assert settings_from_env({"DISCORD_DRY_RUN": value}).dry_run is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off"])
def test_dry_run_falsy_values_including_empty_from_scheduled_runs(value):
    assert settings_from_env({"DISCORD_DRY_RUN": value}).dry_run is False


@pytest.mark.parametrize("env", [
    {"DISCORD_MAX_POSTS": "0"}, {"DISCORD_MAX_POSTS": "26"}, {"DISCORD_MAX_POSTS": "five"},
    {"DISCORD_WEBHOOK_URL": "https://evil.example/hook"},
])
def test_invalid_settings_raise_without_echoing_the_value(env):
    with pytest.raises(ValueError) as caught:
        settings_from_env(env)
    assert "evil.example" not in str(caught.value)


def test_empty_secret_means_not_configured_not_an_error():
    assert settings_from_env({"DISCORD_WEBHOOK_URL": ""}).webhook_url == ""


# ---- message format ----------------------------------------------------------------------

def test_message_leads_with_source_date_title_reason_and_score_last():
    lines = format_message(item(url="https://www.fincen.gov/news/news-releases/x")).split("\n")
    assert lines[0] == "**Source:** FinCEN · **Date:** 2026-09-27"
    assert lines[1] == "**FinCEN issues digital asset guidance**"
    assert lines[2] == "**Why this fired:** digital asset signal"
    assert lines[3] == "**Entities:** FinCEN, XRP"
    assert lines[4] == "**Score:** 50"
    assert lines[-1] == "https://www.fincen.gov/news/news-releases/x"
    assert len(lines) == 6


def test_message_escapes_markdown_and_is_bounded():
    message = format_message(item(title="**bold** _x_ `code` | " + "long " * 200))
    assert message.split("\n")[1].startswith("**\\*\\*bold\\*\\* \\_x\\_ \\`code\\` \\|")
    assert len(message) < 2000
    assert "…" in message.split("\n")[1]


# ---- publishing --------------------------------------------------------------------------

def live(max_posts=5):
    return DiscordSettings(URL, max_posts, False)


def test_cap_keeps_highest_scores_and_posts_oldest_first():
    items = [item("Low score story here", 40, 0), item("Top score story here", 90, 5),
             item("Mid score story here", 60, 10), item("Also mid story here", 60, 2)]
    hook, lines = FakeHook(), []
    report = publish(items, live(3), webhook=hook, sleep=lambda _: None, out=lines.append)
    order = [message.split("\n")[1] for message in hook.sent]
    # Kept: Top (90) and both 60s, dropped: Low (40). Posted by publication time: minutes 2, 5, 10.
    assert order == ["**Also mid story here**", "**Top score story here**", "**Mid score story here**"]
    assert (report.posted, report.not_selected, report.failed) == (3, 1, 0)
    assert "over the 3-post cap" in lines[-1]


def test_posts_are_spaced_but_not_after_the_last_one():
    slept = []
    publish([item("First story here", 50, 0), item("Second story here", 50, 1)], live(),
            webhook=FakeHook(), sleep=slept.append, pause=1.0, out=lambda _: None)
    assert slept == [1.0]


def test_dry_run_prints_messages_and_never_contacts_discord():
    hook, lines = FakeHook(), []
    settings = DiscordSettings(URL, 5, True)
    report = publish([item()], settings, webhook=hook, out=lines.append)
    assert hook.sent == [] and report.posted == 0 and report.dry_run is True
    assert lines[0].startswith("[dry run] would post to Discord:")
    assert "would post 1 of 1" in lines[-1]
    assert TOKEN not in "\n".join(lines)


def test_dry_run_needs_no_webhook():
    lines = []
    publish([item()], DiscordSettings("", 5, True), out=lines.append)
    assert any("[dry run]" in line for line in lines)


def test_unconfigured_webhook_skips_posting_without_failing():
    lines = []
    report = publish([item()], DiscordSettings("", 5, False), out=lines.append)
    assert report.skipped_unconfigured is True and report.posted == 0
    assert "not set" in lines[0]


def test_nothing_relevant_means_no_output_and_no_requests():
    hook, lines = FakeHook(), []
    report = publish([], live(), webhook=hook, out=lines.append)
    assert hook.sent == [] and lines == [] and report.eligible == 0


def test_a_single_failure_does_not_block_later_posts():
    hook, lines = FakeHook(fail_on={0}), []
    items = [item("First story here", 50, 0), item("Second story here", 50, 1),
             item("Third story here", 50, 2)]
    report = publish(items, live(), webhook=hook, sleep=lambda _: None, out=lines.append)
    assert (report.posted, report.failed) == (2, 1)
    assert len(report.posted_keys) == 2 and len(report.failed_keys) == 1
    assert any(line.startswith("::warning::Discord post failed") for line in lines)


def test_two_consecutive_failures_stop_and_count_the_rest_as_failed():
    hook, lines = FakeHook(fail_on={0, 1, 2, 3}), []
    items = [item(f"Story number {n} here", 50, n) for n in range(4)]
    report = publish(items, live(), webhook=hook, sleep=lambda _: None, out=lines.append)
    assert len(hook.sent) == 2
    assert (report.posted, report.failed) == (0, 4)
    assert any("stopping after 2 consecutive failures" in line for line in lines)


def test_failure_lines_never_contain_the_webhook_token():
    session = FakeSession(FakeResponse(500), FakeResponse(500))
    lines = []
    publish([item("First story here", 50, 0), item("Second story here", 50, 1)], live(),
            webhook=DiscordWebhook(URL, session=session), sleep=lambda _: None, out=lines.append)
    assert TOKEN not in "\n".join(lines)


# ---- main() wiring -----------------------------------------------------------------------

def test_main_publishes_only_items_at_or_above_publish_score(monkeypatch):
    relevant, weak = item("Relevant story here", 50), item("Weak story here", 10)
    captured = {}
    monkeypatch.setattr(main, "run_pipeline", lambda **kwargs: main.PipelineResult(fresh=[relevant, weak]))
    monkeypatch.setattr(main, "publish", lambda items, settings: captured.update(items=items, settings=settings))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    monkeypatch.delenv("DISCORD_DRY_RUN", raising=False)
    main.main()
    assert captured["items"] == [relevant]
    assert captured["settings"].webhook_url == URL


def test_main_prints_discovery_health_and_still_publishes(monkeypatch, capsys):
    # Regression: the discovery print loop once rebound `result`, so the first discovery source
    # raised AttributeError (DiscoveryResult has no .health) before publish() was reached.
    relevant = item("Relevant story here", 50)
    discovery = main.DiscoveryResult("sec-edgar", "sec_edgar", "ok")
    health = {"sec-edgar": {"consecutive_failures": 2, "consecutive_empty": 1}}
    captured = {}
    monkeypatch.setattr(main, "run_pipeline", lambda **kwargs: main.PipelineResult(
        fresh=[relevant], discovery_results=[discovery], health=health))
    monkeypatch.setattr(main, "publish", lambda items, settings: captured.update(items=items))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    monkeypatch.delenv("DISCORD_DRY_RUN", raising=False)
    main.main()
    out = capsys.readouterr().out
    assert "Discovery source sec-edgar: ok" in out
    assert "Health: failures=2 empty=1" in out
    assert captured["items"] == [relevant]


def test_main_rejects_bad_discord_settings_before_collecting_anything(monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("pipeline must not run with invalid Discord settings")

    monkeypatch.setattr(main, "run_pipeline", must_not_run)
    monkeypatch.setenv("DISCORD_MAX_POSTS", "banana")
    with pytest.raises(SystemExit, match="Discord configuration error"):
        main.main()


@pytest.mark.parametrize("error, message", [
    (SourceRegistryError("bad source entry"), "Source configuration error: bad source entry"),
    (DiscoveryDispatchError("no such method"), "Discovery configuration error: no such method"),
    (DiscoveryRegistryError("bad discovery entry"), "Discovery configuration error: bad discovery entry"),
])
def test_main_turns_config_errors_into_clean_exits(monkeypatch, error, message):
    def boom():
        raise error

    monkeypatch.setattr(main, "run_pipeline", boom)
    monkeypatch.delenv("DISCORD_DRY_RUN", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        main.main()
    assert str(excinfo.value) == message


def test_dry_run_hands_the_pipeline_read_only_state(monkeypatch, tmp_path):
    captured = {}

    def fake_pipeline(**kwargs):
        captured.update(kwargs)
        return main.PipelineResult()

    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(main, "publish", lambda items, settings: None)
    monkeypatch.setenv("DISCORD_DRY_RUN", "true")
    main.main()
    assert isinstance(captured["state"], main._NoSaveState)
    assert isinstance(captured["discovery_state"], main._NoSaveState)


def test_no_save_state_reads_but_never_writes(tmp_path):
    path = tmp_path / "seen.json"
    real = JsonState(str(path))
    real.save({"a"})
    wrapped = main._NoSaveState(JsonState(str(path)))
    assert wrapped.load() == {"a"}
    wrapped.save({"a", "b"})
    assert real.load() == {"a"}
    missing = main._NoSaveState(JsonState(str(tmp_path / "none.json")))
    missing.save({"x"})
    assert not (tmp_path / "none.json").exists()


# ---- workflow ----------------------------------------------------------------------------

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "news_feed.yml"


def test_workflow_defaults_manual_runs_to_dry_run_and_skips_state_save_when_dry():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "dry_run:" in text and "default: true" in text
    assert "DISCORD_DRY_RUN: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || false }}" in text
    assert "state/outbox.json" in text
    assert "if: success() && (github.event_name == 'schedule' || !inputs.dry_run)" in text


def test_workflow_takes_webhook_from_secrets_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_INTELLIGENCE_DISCORD_WEBHOOK }}" in text
    assert "discord.com/api/webhooks" not in text
    assert "schedule:" in text
