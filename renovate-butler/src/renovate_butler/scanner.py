"""Classify open PRs: CI state + merge eligibility."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import requests

from . import confidence as conf
from .config import MergeConfig
from .github_client import CIState, Forge, PRInfo


@dataclass
class Verdict:
    pr: PRInfo
    ci: CIState
    confidence: str
    update_types: set[str]
    merge: bool
    reason: str


def fetch_badge(url: str) -> str:
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


def evaluate(
    pr: PRInfo,
    ci: CIState,
    cfg: MergeConfig,
    fetch: Callable[[str], str] = fetch_badge,
) -> Verdict:
    def v(merge: bool, reason: str, level: str = "-", types: set[str] | None = None) -> Verdict:
        return Verdict(pr, ci, level, types or set(), merge, reason)

    if pr.author not in cfg.authors:
        return v(False, f"author {pr.author} is not a Renovate bot")
    if pr.draft:
        return v(False, "draft")
    hit = set(pr.labels) & set(cfg.skip_labels)
    if hit:
        return v(False, f"label {sorted(hit)}")
    if pr.changes_requested:
        return v(False, "changes requested")
    if pr.mergeable is False or pr.mergeable_state == "dirty":
        return v(False, "merge conflicts")
    if ci != "passing":
        return v(False, f"CI {ci}")

    c = conf.parse(pr.body, fetch)
    level, types = c.level, c.update_types
    if not c.updates:
        return v(False, "no Merge Confidence badges in PR body")
    if not conf.meets(level, cfg.min_confidence):
        return v(False, f"confidence {level} < {cfg.min_confidence}", level, types)
    bad = types - set(cfg.allowed_update_types)
    if bad:
        return v(False, f"update type {sorted(bad)} not allowed", level, types)
    if pr.mergeable is None or pr.mergeable_state in ("unknown", "blocked", "behind"):
        # GitHub hasn't computed mergeability, or branch protection blocks it.
        return v(False, f"mergeable_state={pr.mergeable_state}", level, types)
    return v(True, "ok", level, types)


def scan(forge: Forge, repo: str, cfg: MergeConfig, fetch: Callable[[str], str] = fetch_badge) -> list[Verdict]:
    out = []
    for pr in forge.open_prs(repo):
        ci = forge.ci_state(repo, pr.head_sha)
        out.append(evaluate(pr, ci, cfg, fetch))
    return out


def format_report(verdicts: list[Verdict]) -> str:
    if not verdicts:
        return "  (no open PRs)"
    rows = [("PR", "CI", "CONF", "TYPE", "ACTION", "TITLE")]
    for v in verdicts:
        rows.append(
            (
                f"#{v.pr.number}",
                v.ci,
                v.confidence,
                ",".join(sorted(v.update_types)) or "-",
                "MERGE" if v.merge else f"skip: {v.reason}",
                v.pr.title[:60],
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    lines = []
    for r in rows:
        lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[-1])
    return "\n".join(lines)
