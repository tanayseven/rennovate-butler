from types import SimpleNamespace

import requests

from renovate_butler import notifier as n
from renovate_butler.orchestrator import EventLog, RepoResult

SECRET_URL = "https://discord.com/api/webhooks/123/SECRET-TOKEN"


def test_summary_all_green_live_run():
    results = [
        RepoResult("o/a", merged=[2, 5], main_green=True),
        RepoResult("o/b", skipped=3, main_green=True),
    ]
    p = n.summary_payload(results, dry_run=False, link="https://gh/run/1")
    e = p["embeds"][0]
    assert e["title"] == "Run: main is green in 2 of 2 repos"
    assert e["color"] == n.GREEN
    assert "merged [#2](https://github.com/o/a/pull/2), [#5](https://github.com/o/a/pull/5)" in e["description"]
    assert "nothing to merge (3 skipped)" in e["description"]
    assert (
        "2 merged · 0 fix PRs · model use: 0 tokens (0 in, 0 out) · $0.00 · [View run](https://gh/run/1)"
        in e["description"]
    )
    assert p["allowed_mentions"] == {"parse": []}


def test_summary_dry_run_and_red_repo():
    results = [
        RepoResult("o/a", would_merge=[8], main_green=True),
        RepoResult("o/b", fix_prs=[901], main_green=False, note="gave up after 3 fix attempts"),
    ]
    e = n.summary_payload(results, dry_run=True)["embeds"][0]
    assert e["title"] == "Dry run: main is green in 1 of 2 repos"
    assert e["color"] == n.RED
    assert "would merge [#8]" in e["description"]
    assert "fix PRs [#901]" in e["description"]
    assert "**main not green**: gave up after 3 fix attempts" in e["description"]
    assert "1 would merge · dry run, nothing changed" in e["description"]


def test_long_summary_is_truncated():
    results = [RepoResult(f"o/r{i}", merged=list(range(50)), main_green=True) for i in range(20)]
    desc = n.summary_payload(results, dry_run=False)["embeds"][0]["description"]
    assert len(desc) <= n.MAX_DESCRIPTION


def test_merge_ci_alert_per_outcome():
    ev = {"event": "merge_ci", "repo": "o/a", "pr": 5, "title": "update lodash to 4.17.21"}
    passed = n.event_payload({**ev, "outcome": "green"}, link="https://gh/run/1")["embeds"][0]
    assert passed["title"] == "Merged: main CI passed" and passed["color"] == n.GREEN
    assert passed["description"] == (
        "[a](https://github.com/o/a): merged [#5](https://github.com/o/a/pull/5) update lodash to 4.17.21\n"
        "✅ CI on main passed.\n🪙 0 tokens · $0.00 (merging doesn't use the model)\n[View run](https://gh/run/1)"
    )
    failed = n.event_payload({**ev, "outcome": "red"})["embeds"][0]
    assert failed["title"] == "Merged: main CI failed" and failed["color"] == n.RED
    assert "Trying to fix it" in failed["description"]
    assert n.event_payload({**ev, "outcome": "no-ci"})["embeds"][0]["color"] == n.AMBER
    assert n.event_payload({**ev, "outcome": "timeout"})["embeds"][0]["color"] == n.AMBER
    assert n.event_payload({**ev, "outcome": "weird"}) is None


def test_event_payloads():
    red = n.event_payload({"event": "main_red", "repo": "o/a", "pr": None})
    assert (
        red["embeds"][0]["color"] == n.RED and "main is failing. Trying to fix it." in red["embeds"][0]["description"]
    )
    # Red after a Renovate merge is covered by that merge's merge_ci alert; don't post it twice.
    assert n.event_payload({"event": "main_red", "repo": "o/a", "pr": 5}) is None
    opened = n.event_payload({"event": "fix_pr", "repo": "o/a", "pr": 901})
    assert opened["embeds"][0]["title"] == "Fix PR opened"
    gave_up = n.event_payload(
        {"event": "fix_gave_up", "repo": "o/a", "msg": "  gave up after 3 fix attempts", "pr": None}
    )
    assert gave_up["embeds"][0]["description"].startswith("[a](https://github.com/o/a): gave up after 3 fix attempts.")
    assert n.event_payload({"event": "merged", "repo": "o/a", "pr": 1}) is None


def _resp(status, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body or {})


def test_send_retries_once_on_rate_limit():
    calls, slept, echoed = [], [], []
    responses = [_resp(429, {"retry_after": 2.5}), _resp(204)]

    def post(url, json, timeout):
        calls.append(url)
        return responses.pop(0)

    d = n.DiscordNotifier(SECRET_URL, echoed.append, post, slept.append)
    assert d.send({"x": 1})
    assert len(calls) == 2 and slept == [2.5]


def test_errors_never_print_the_webhook_url():
    echoed = []

    def boom(url, json, timeout):
        raise requests.ConnectionError(f"cannot reach {url}")

    assert not n.DiscordNotifier(SECRET_URL, echoed.append, boom).send({})
    assert not n.DiscordNotifier(SECRET_URL, echoed.append, lambda *a, **k: _resp(401)).send({})
    assert echoed and all("SECRET" not in line for line in echoed)


def test_from_env(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert n.from_env() is None
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://evil.example/hook")
    echoed = []
    assert n.from_env(echoed.append) is None and "does not look like" in echoed[0]
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", SECRET_URL)
    assert n.from_env() is not None


def test_run_url(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/butler")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert n.run_url() == "https://github.com/o/butler/actions/runs/42"


def test_event_log_feeds_listeners_and_survives_their_errors(tmp_path):
    seen, echoed = [], []

    def broken(event):
        raise RuntimeError("discord down")

    log = EventLog(str(tmp_path / "log.jsonl"), echoed.append, [seen.append, broken])
    log("plain line")
    log("  opened fix PR", event="fix_pr", repo="o/a", pr=9)
    assert [e["event"] for e in seen] == ["fix_pr"]
    assert any("listener failed" in line for line in echoed)


def test_fix_alerts_show_tokens_cost_and_cause():
    usage = {"llm_calls": 14, "input_tokens": 182_340, "output_tokens": 6_120, "cost_usd": 0.6388, "cause_pr": 5}
    merged = n.event_payload({"event": "fix_merged", "repo": "o/a", "pr": 901, **usage})["embeds"][0]
    assert (
        "🪙 188,460 tokens (182,340 in, 6,120 out) · $0.64 over 14 model calls"
        " · fixing the break from [#5](https://github.com/o/a/pull/5)"
    ) in merged["description"]
    gave_up = n.event_payload(
        {"event": "fix_gave_up", "repo": "o/a", "msg": "gave up", "pr": None, **usage, "cause_pr": None}
    )["embeds"][0]
    assert "$0.64 over 14 model calls" in gave_up["description"] and "fixing the break" not in gave_up["description"]
    # Events from before usage tracking (no fields) still render, just without the cost line.
    assert "🪙" not in n.event_payload({"event": "fix_pr", "repo": "o/a", "pr": 901})["embeds"][0]["description"]


def test_summary_shows_fix_cost_per_repo_and_total():
    from renovate_butler.fixer import Usage

    results = [
        RepoResult("o/a", merged=[2], fix_prs=[901], main_green=True, usage=Usage(9, 100_000, 4_000, 0.36)),
        RepoResult("o/b", merged=[3], main_green=True, usage=Usage(3, 20_000, 1_000, 0.07)),
    ]
    desc = n.summary_payload(results, dry_run=False)["embeds"][0]["description"]
    assert "fixing cost $0.36" in desc and "fixing cost $0.07" in desc
    assert "model use: 125,000 tokens (120,000 in, 5,000 out) · $0.43" in desc
