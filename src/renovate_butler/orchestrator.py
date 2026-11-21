"""The main loop: merge confident green PRs one at a time, keep main green."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import scanner
from .config import Config
from .fixer import Fixer
from .github_client import Forge, PRInfo
from .watcher import wait_for_ci

MAX_LOG_CHARS = 40_000
MAX_PATCH_CHARS = 20_000


def describe_error(e: Exception) -> str:
    status = getattr(e, "status", None)
    if status == 404:
        return "not found (check the owner/repo name and that your token can access it)"
    if status in (401, 403):
        return f"GitHub refused access ({status}); check token scopes / repo permissions"
    return f"{type(e).__name__}: {e}"


class EventLog:
    """Prints human-readable lines and appends structured events to a JSONL file."""

    def __init__(
        self,
        path: str | None,
        echo: Callable[[str], None] = print,
        listeners: list[Callable[[dict], None]] | None = None,
    ):
        self.path = Path(path) if path else None
        self.echo = echo
        self.listeners = listeners or []

    def __call__(self, msg: str, **event) -> None:
        self.echo(msg)
        if not event:
            return
        rec = {"ts": datetime.now(UTC).isoformat(), "msg": msg, **event}
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        for listener in self.listeners:
            try:
                listener(rec)
            except Exception as e:  # noqa: BLE001 - a notifier must never break the run
                self.echo(f"  warning: event listener failed: {e}")


@dataclass
class RepoResult:
    repo: str
    merged: list[int] = field(default_factory=list)
    would_merge: list[int] = field(default_factory=list)
    skipped: int = 0
    fix_prs: list[int] = field(default_factory=list)
    main_green: bool = False
    note: str = ""


class Butler:
    def __init__(
        self,
        forge: Forge,
        fixer: Fixer | None,
        cfg: Config,
        log: Callable[..., None] = print,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        fetch: Callable[[str], str] = scanner.fetch_badge,
    ):
        self.forge, self.fixer, self.cfg = forge, fixer, cfg
        self.log, self.sleep, self.now, self.fetch = log, sleep, now, fetch

    def _wait(self, repo: str, sha: str):
        return wait_for_ci(self.forge, repo, sha, self.cfg.poll, self.log, self.sleep, self.now)

    # ------------------------------------------------------------------ top level

    def run(self) -> list[RepoResult]:
        results = []
        for repo in self.cfg.repos:
            try:
                results.append(self.process_repo(repo))
            except Exception as e:  # noqa: BLE001 - one broken repo must not stop the others
                note = describe_error(e)
                self.log(f"  error: {note}", event="repo_error", repo=repo, error=repr(e))
                results.append(RepoResult(repo, note=f"error: {note}"))
        return results

    def process_repo(self, repo: str) -> RepoResult:
        res = RepoResult(repo)
        main = self.cfg.main_branch
        self.log(f"== {repo}")

        head = self.forge.branch_head(repo, main)
        outcome = self._wait(repo, head)
        if outcome == "timeout":
            res.note = "main CI did not settle; not merging anything"
            self.log(f"  {res.note}", event="main_timeout", repo=repo, sha=head)
            return res
        if outcome == "red":
            self.log(
                f"  {main} is already red at {head[:7]}; healing before merging", event="main_red", repo=repo, sha=head
            )
            if not self.heal(repo, res):
                return res

        verdicts = scanner.scan(self.forge, repo, self.cfg.merge, self.fetch)
        self.log(scanner.format_report(verdicts))
        res.skipped = sum(not v.merge for v in verdicts)

        for v in verdicts:
            if not v.merge:
                continue
            # Re-check: earlier merges may have changed this PR's state.
            pr = self.forge.get_pr(repo, v.pr.number)
            fresh = scanner.evaluate(pr, self.forge.ci_state(repo, pr.head_sha), self.cfg.merge, self.fetch)
            if not fresh.merge:
                res.skipped += 1
                self.log(f"  #{pr.number}: no longer mergeable ({fresh.reason})", event="skip", repo=repo, pr=pr.number)
                continue
            if self.cfg.dry_run:
                res.would_merge.append(pr.number)
                self.log(f"  [dry-run] would merge #{pr.number} {pr.title}", event="dry_merge", repo=repo, pr=pr.number)
                continue
            merge_sha = self.forge.merge(repo, pr.number, self.cfg.merge.method, pr.head_sha)
            res.merged.append(pr.number)
            self.log(
                f"  merged #{pr.number} ({fresh.confidence}, {','.join(sorted(fresh.update_types))}) -> {merge_sha[:7]}",
                event="merged",
                repo=repo,
                pr=pr.number,
                sha=merge_sha,
                confidence=fresh.confidence,
            )
            outcome = self._wait(repo, merge_sha)
            if outcome == "timeout":
                res.note = f"main CI did not settle after merging #{pr.number}; stopping"
                self.log(f"  {res.note}", event="main_timeout", repo=repo, sha=merge_sha)
                return res
            if outcome == "red":
                self.log(
                    f"  {main} went red after #{pr.number}", event="main_red", repo=repo, sha=merge_sha, pr=pr.number
                )
                if not self.heal(repo, res):
                    return res

        res.main_green = True
        self.log(f"  {main} is green", event="main_green", repo=repo)
        return res

    # ------------------------------------------------------------------ healing

    def heal(self, repo: str, res: RepoResult) -> bool:
        main = self.cfg.main_branch
        if self.cfg.dry_run:
            self.log("  [dry-run] would ask the fixer for a fix PR", event="dry_fix", repo=repo)
            res.note = "main is red (dry-run, no fix attempted)"
            return False
        if self.fixer is None:
            res.note = "main is red and no fixer is configured"
            return False

        red_sha = self.forge.branch_head(repo, main)
        fix_pr: PRInfo | None = None
        branch = ""
        history: list[str] = []

        for attempt in range(1, self.cfg.fix.max_attempts + 1):
            target = fix_pr.head_sha if fix_pr else red_sha
            if not fix_pr:
                stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
                branch = f"butler/fix-{main}-{red_sha[:7]}-{stamp}"
            context, workflows = self._context(repo, red_sha, target, fix_pr, history)
            self.log(
                f"  fix attempt {attempt}/{self.cfg.fix.max_attempts} on {branch}",
                event="fix_attempt",
                repo=repo,
                attempt=attempt,
            )
            result = self.fixer.fix(repo, main, branch, fix_pr is not None, context, workflows)
            if not result.changed:
                history.append(f"Attempt {attempt}: the agent made no changes ({result.summary}).")
                self.log(f"  attempt {attempt}: no changes produced", event="fix_empty", repo=repo)
                continue

            if fix_pr is None:
                body = (
                    f"`{main}` is failing at {red_sha[:7]}. This PR was generated by renovate-butler.\n\n"
                    f"**Summary:** {result.summary}\n\n{result.explanation}\n"
                )
                fix_pr = self.forge.open_pr(
                    repo, branch, main, f"fix({main}): {result.summary}", body, [self.cfg.fix.label]
                )
                res.fix_prs.append(fix_pr.number)
                self.log(
                    f"  opened fix PR #{fix_pr.number} {fix_pr.url}",
                    event="fix_pr",
                    repo=repo,
                    pr=fix_pr.number,
                    url=fix_pr.url,
                )
            fix_pr = self._refresh_after_push(repo, fix_pr, target)

            outcome = self._wait(repo, fix_pr.head_sha)
            if outcome == "timeout":
                res.note = f"CI on fix PR #{fix_pr.number} did not settle"
                return False
            if outcome == "red":
                history.append(f"Attempt {attempt}: pushed '{result.summary}' but the PR's CI still failed.")
                self.log(f"  fix PR #{fix_pr.number} CI is red", event="fix_red", repo=repo, pr=fix_pr.number)
                continue

            fix_pr = self._wait_mergeable(repo, fix_pr)
            if fix_pr.mergeable is False:
                res.note = f"fix PR #{fix_pr.number} has conflicts"
                return False
            merge_sha = self.forge.merge(repo, fix_pr.number, self.cfg.merge.method, fix_pr.head_sha)
            self.log(
                f"  merged fix PR #{fix_pr.number} -> {merge_sha[:7]}",
                event="fix_merged",
                repo=repo,
                pr=fix_pr.number,
                sha=merge_sha,
            )
            main_outcome = self._wait(repo, merge_sha)
            if main_outcome in ("green", "no-ci"):
                return True
            if main_outcome == "timeout":
                res.note = "main CI did not settle after merging the fix"
                return False
            # Still red on main (e.g. a different failure): start a new incident.
            history.append(f"Attempt {attempt}: fix PR #{fix_pr.number} merged, but {main} is still red.")
            red_sha, fix_pr = merge_sha, None

        res.note = f"gave up after {self.cfg.fix.max_attempts} fix attempts"
        self.log(
            f"  {res.note}",
            event="fix_gave_up",
            repo=repo,
            pr=fix_pr.number if fix_pr else None,
            url=fix_pr.url if fix_pr else None,
        )
        if fix_pr:
            self.forge.comment(
                repo,
                fix_pr.number,
                f"renovate-butler gave up after {self.cfg.fix.max_attempts} attempts. A human needs to look.\n\n"
                + "\n".join(f"- {h}" for h in history),
            )
        return False

    def _refresh_after_push(self, repo: str, pr: PRInfo, old_head: str) -> PRInfo:
        """GitHub takes a moment to see a pushed commit on the PR."""
        for _ in range(20):
            pr = self.forge.get_pr(repo, pr.number)
            if pr.head_sha != old_head:
                return pr
            self.sleep(3)
        return pr

    def _wait_mergeable(self, repo: str, pr: PRInfo) -> PRInfo:
        for _ in range(20):
            pr = self.forge.get_pr(repo, pr.number)
            if pr.mergeable is not None:
                return pr
            self.sleep(3)
        return pr

    def _context(
        self, repo: str, red_sha: str, target: str, fix_pr: PRInfo | None, history: list[str]
    ) -> tuple[str, list[str]]:
        runs = self.forge.failed_runs(repo, target)
        logs = "\n\n".join(f"## workflow: {r.name} ({r.url})\n{self.forge.run_logs(repo, r.run_id)}" for r in runs)
        workflows = [
            w for w in {self.forge.file_at(repo, r.workflow_path, target) for r in runs if r.workflow_path} if w
        ]
        message, patch = self.forge.commit_patch(repo, red_sha)
        parts = [
            f"Repository: {repo}\nBranch: {self.cfg.main_branch} (failing at {red_sha[:7]})",
            f"Most recent commit on {self.cfg.main_branch} (likely culprit):\n<untrusted>\n{message}\n\n{patch[:MAX_PATCH_CHARS]}\n</untrusted>",
            f"Failing CI logs{' for the fix PR' if fix_pr else ''}:\n<untrusted>\n{logs[-MAX_LOG_CHARS:] or '(no logs found)'}\n</untrusted>",
        ]
        if workflows:
            parts.append(
                "Workflow definitions:\n<untrusted>\n" + "\n---\n".join(workflows)[:MAX_PATCH_CHARS] + "\n</untrusted>"
            )
        if history:
            parts.append("Previous attempts:\n" + "\n".join(f"- {h}" for h in history))
        if fix_pr:
            parts.append(f"You are continuing on branch {fix_pr.head_ref}; your earlier commits are already there.")
        return "\n\n".join(parts), workflows
