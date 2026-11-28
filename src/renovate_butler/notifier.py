"""Post butler activity to a Discord channel through a webhook.

Two kinds of message:
- live alerts while the run is going: each Renovate PR merged, with whether CI on
  main then passed or failed; main already red; fix PR opened/merged; gave up;
- one summary at the end of every run.

The webhook URL contains a secret token, so it is never printed: errors are
reported by type and status code only. ``allowed_mentions`` is empty so text
from PR titles or CI logs can never ping @everyone or a role.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from .orchestrator import RepoResult

GREEN = 0x34C77B
AMBER = 0xF5A524
RED = 0xE5484D
USERNAME = "Renovate Butler"
MAX_DESCRIPTION = 4000
MAX_NOTE = 200

# CI outcome on main after merging a Renovate PR -> (alert title, result line, colour).
MERGE_OUTCOMES = {
    "green": ("Merged: main CI passed", "✅ CI on main passed.", GREEN),
    "red": ("Merged: main CI failed", "❌ CI on main failed. Trying to fix it.", RED),
    "no-ci": ("Merged: no CI on main", "⚪ No CI ran on main, so there is nothing to check.", AMBER),
    "timeout": ("Merged: main CI didn't finish", "⏳ CI on main didn't finish in time. Stopped merging here.", AMBER),
}


def _pr_link(repo: str, number: int) -> str:
    return f"[#{number}](https://github.com/{repo}/pull/{number})"


def _repo_link(repo: str) -> str:
    return f"[{repo.split('/', 1)[-1]}](https://github.com/{repo})"


def _usage_text(input_tokens: int, output_tokens: int, cost_usd: float) -> str:
    return f"{input_tokens + output_tokens:,} tokens ({input_tokens:,} in, {output_tokens:,} out) · ${cost_usd:.2f}"


def _fix_cost_line(event: dict) -> str:
    """Model usage for this fix so far, from the event's fields; empty when the event has none."""
    if "cost_usd" not in event:
        return ""
    calls = event.get("llm_calls", 0)
    line = "🪙 " + _usage_text(event.get("input_tokens", 0), event.get("output_tokens", 0), event["cost_usd"])
    line += f" over {calls} model call{'s' if calls != 1 else ''}"
    cause = event.get("cause_pr")
    if cause:
        line += f" · fixing the break from {_pr_link(event.get('repo', ''), cause)}"
    return "\n" + line


def run_url() -> str | None:
    """Link to the current GitHub Actions run, when running in Actions."""
    server, repo, run_id = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def _embed(title: str, description: str, color: int) -> dict:
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 1] + "…"
    return {
        "title": title[:256],
        "description": description,
        "color": color,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def summary_payload(results: Sequence[RepoResult], dry_run: bool, link: str | None = None) -> dict:
    green = sum(r.main_green for r in results)
    merged = sum(len(r.merged) for r in results)
    fixes = sum(len(r.fix_prs) for r in results)
    lines = []
    for r in results:
        parts = []
        if r.merged:
            parts.append("merged " + ", ".join(_pr_link(r.repo, n) for n in r.merged))
        if r.would_merge:
            parts.append("would merge " + ", ".join(_pr_link(r.repo, n) for n in r.would_merge))
        if r.fix_prs:
            parts.append("fix PRs " + ", ".join(_pr_link(r.repo, n) for n in r.fix_prs))
        if r.usage.calls:
            parts.append(f"fixing cost ${r.usage.cost_usd:.2f}")
        if not parts:
            parts.append(f"nothing to merge ({r.skipped} skipped)" if r.skipped else "no open PRs to merge")
        if not r.main_green:
            parts.append(f"**main not green**: {r.note[:MAX_NOTE] or 'see the run log'}")
        lines.append(f"**{_repo_link(r.repo)}** · " + " · ".join(parts))
    footer = f"{merged} merged · {fixes} fix PRs"
    total_in = sum(r.usage.input_tokens for r in results)
    total_out = sum(r.usage.output_tokens for r in results)
    total_cost = sum(r.usage.cost_usd for r in results)
    footer += f" · model use: {_usage_text(total_in, total_out, total_cost)}"
    if dry_run:
        footer = f"{sum(len(r.would_merge) for r in results)} would merge · dry run, nothing changed"
    if link:
        footer += f" · [View run]({link})"
    title = f"{'Dry run' if dry_run else 'Run'}: main is green in {green} of {len(results)} repos"
    color = GREEN if green == len(results) else RED
    return {
        "username": USERNAME,
        "allowed_mentions": {"parse": []},
        "embeds": [_embed(title, "\n".join(lines + ["", footer]), color)],
    }


def event_payload(event: dict, link: str | None = None) -> dict | None:
    """Turn a butler event into a live alert, or None for events that only go in the summary."""
    kind, repo = event.get("event"), event.get("repo", "")
    pr = event.get("pr")
    pr_ref = _pr_link(repo, pr) if pr else ""
    if kind == "merge_ci":
        outcome = event.get("outcome")
        what = f"{_repo_link(repo)}: merged {pr_ref} {event.get('title', '')}".rstrip()
        if outcome not in MERGE_OUTCOMES:
            return None
        title, result, color = MERGE_OUTCOMES[outcome]
        desc = f"{what}\n{result}\n🪙 0 tokens · $0.00 (merging doesn't use the model)"
    elif kind == "main_red":
        if pr:
            return None  # red after a Renovate merge: already reported by that PR's merge_ci alert
        title, desc, color = "main went red", f"{_repo_link(repo)}: main is failing. Trying to fix it.", RED
    elif kind == "fix_pr":
        title, desc, color = "Fix PR opened", f"{_repo_link(repo)}: opened {pr_ref} to fix main.", AMBER
        desc += _fix_cost_line(event)
    elif kind == "fix_merged":
        title, desc, color = "Fix merged", f"{_repo_link(repo)}: merged {pr_ref}. Watching main.", GREEN
        desc += _fix_cost_line(event)
    elif kind == "fix_gave_up":
        where = f" Last fix PR: {pr_ref}." if pr else ""
        title, desc, color = "Needs a human", f"{_repo_link(repo)}: {event.get('msg', '').strip()}.{where}", RED
        desc += _fix_cost_line(event)
    elif kind == "pr_fixed":
        title, color = "Renovate PR repaired", GREEN
        desc = f"{_repo_link(repo)}: pushed a fix to {pr_ref}; its CI is green. Merging it."
        desc += _fix_cost_line(event)
    elif kind == "pr_fix_gave_up":
        title, desc, color = "Needs a human", f"{_repo_link(repo)}: couldn't make {pr_ref}'s CI pass.", RED
        desc += _fix_cost_line(event)
    else:
        return None
    if link:
        desc += f"\n[View run]({link})"
    return {"username": USERNAME, "allowed_mentions": {"parse": []}, "embeds": [_embed(title, desc, color)]}


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str,
        echo: Callable[[str], None] = print,
        post: Callable[..., requests.Response] = requests.post,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.webhook_url = webhook_url
        self.echo, self._post, self.sleep = echo, post, sleep
        self.link = run_url()

    def send(self, payload: dict) -> bool:
        for attempt in range(2):
            try:
                resp = self._post(self.webhook_url, json=payload, timeout=15)
            except requests.RequestException as e:
                self.echo(f"  warning: Discord post failed ({type(e).__name__})")
                return False
            if resp.status_code == 429 and attempt == 0:
                try:
                    wait = float(resp.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                self.sleep(min(wait, 10))
                continue
            if resp.status_code >= 400:
                self.echo(f"  warning: Discord rejected the message (HTTP {resp.status_code})")
                return False
            return True
        self.echo("  warning: Discord kept rate-limiting; message dropped")
        return False

    def on_event(self, event: dict) -> None:
        payload = event_payload(event, self.link)
        if payload:
            self.send(payload)

    def summary(self, results: Sequence[RepoResult], dry_run: bool) -> None:
        self.send(summary_payload(results, dry_run, self.link))

    def crashed(self, error: BaseException) -> None:
        desc = f"The run stopped with {type(error).__name__} before finishing. Check the run log."
        if self.link:
            desc += f"\n[View run]({self.link})"
        self.send(
            {"username": USERNAME, "allowed_mentions": {"parse": []}, "embeds": [_embed("Run failed", desc, RED)]}
        )


def from_env(echo: Callable[[str], None] = print) -> DiscordNotifier | None:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return None
    if not url.startswith("https://discord.com/api/webhooks/") and not url.startswith(
        "https://discordapp.com/api/webhooks/"
    ):
        echo("  warning: DISCORD_WEBHOOK_URL does not look like a Discord webhook; not posting")
        return None
    return DiscordNotifier(url, echo)
