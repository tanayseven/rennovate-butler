"""Thin wrapper around PyGithub that returns plain dataclasses.

Everything above this module (scanner, watcher, orchestrator) talks to the
``Forge`` protocol, so tests can swap in an in-memory fake.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

import requests
from github import Auth, Github, GithubException

CIState = Literal["passing", "failing", "pending", "none"]

FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
# Workflow runs not caused by code landing on the commit: scheduled jobs, manual runs, API
# dispatches and GitHub's own dependency-graph runs. They share the commit's SHA, but a failing
# nightly data job (or a third party blocking it) says nothing about whether the code is broken,
# and the fixer can't repair it. So they never count towards a commit's CI state.
IGNORED_EVENTS = frozenset({"schedule", "workflow_dispatch", "repository_dispatch", "dynamic"})
LOG_TAIL_LINES = 400


@dataclass
class PRInfo:
    repo: str
    number: int
    title: str
    author: str
    head_sha: str
    head_ref: str
    base_ref: str
    url: str
    body: str = ""
    draft: bool = False
    labels: list[str] = field(default_factory=list)
    mergeable: bool | None = None
    mergeable_state: str = "unknown"
    changes_requested: bool = False
    created_at: datetime | None = None


@dataclass
class FailedRun:
    run_id: int
    name: str
    url: str
    workflow_path: str


def ignored_suites(workflow_runs: Iterable) -> set[int]:
    """Check-suite ids of the workflow runs whose trigger is in IGNORED_EVENTS."""
    return {run.check_suite_id for run in workflow_runs if run.event in IGNORED_EVENTS}


def latest_check_runs(check_runs: Iterable, ignored: set[int]) -> list:
    """The latest check run per name (re-runs supersede), minus runs from ignored check suites."""
    latest: dict[str, object] = {}
    for cr in check_runs:
        if cr.check_suite_id in ignored:
            continue
        prev = latest.get(cr.name)
        if prev is None or (cr.started_at and prev.started_at and cr.started_at > prev.started_at):
            latest[cr.name] = cr
    return list(latest.values())


class Forge(Protocol):
    def open_prs(self, repo: str) -> list[PRInfo]: ...
    def get_pr(self, repo: str, number: int) -> PRInfo: ...
    def ci_state(self, repo: str, sha: str) -> CIState: ...
    def merge(self, repo: str, number: int, method: str, sha: str) -> str: ...
    def branch_head(self, repo: str, branch: str) -> str: ...
    def failed_runs(self, repo: str, sha: str) -> list[FailedRun]: ...
    def run_logs(self, repo: str, run_id: int) -> str: ...
    def file_at(self, repo: str, path: str, ref: str) -> str: ...
    def commit_patch(self, repo: str, sha: str) -> tuple[str, str]: ...
    def open_pr(self, repo: str, head: str, base: str, title: str, body: str, labels: list[str]) -> PRInfo: ...
    def comment(self, repo: str, number: int, body: str) -> None: ...


class GitHubClient:
    def __init__(self, token: str):
        self.token = token
        self.gh = Github(auth=Auth.Token(token), per_page=100)

    def _repo(self, repo: str):
        return self.gh.get_repo(repo)

    @staticmethod
    def _to_info(repo: str, pr) -> PRInfo:
        reviews = list(pr.get_reviews())
        latest_by_user: dict[str, str] = {}
        for r in reviews:
            if r.user and r.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest_by_user[r.user.login] = r.state
        return PRInfo(
            repo=repo,
            number=pr.number,
            title=pr.title,
            author=pr.user.login if pr.user else "",
            head_sha=pr.head.sha,
            head_ref=pr.head.ref,
            base_ref=pr.base.ref,
            url=pr.html_url,
            body=pr.body or "",
            draft=bool(pr.draft),
            labels=[lb.name for lb in pr.labels],
            mergeable=pr.mergeable,
            mergeable_state=pr.mergeable_state or "unknown",
            changes_requested="CHANGES_REQUESTED" in latest_by_user.values(),
            created_at=pr.created_at,
        )

    def open_prs(self, repo: str) -> list[PRInfo]:
        r = self._repo(repo)
        # The list endpoint omits mergeable; fetch each PR individually.
        return [
            self._to_info(repo, r.get_pull(p.number))
            for p in r.get_pulls(state="open", sort="created", direction="asc")
        ]

    def get_pr(self, repo: str, number: int) -> PRInfo:
        r = self._repo(repo)
        pr = r.get_pull(number)
        # GitHub computes mergeability lazily; give it a few seconds.
        for _ in range(5):
            if pr.mergeable is not None:
                break
            time.sleep(2)
            pr = r.get_pull(number)
        return self._to_info(repo, pr)

    def ci_state(self, repo: str, sha: str) -> CIState:
        r = self._repo(repo)
        commit = r.get_commit(sha)
        ignored = ignored_suites(r.get_workflow_runs(head_sha=sha))
        seen_any = False
        pending = False
        for cr in latest_check_runs(commit.get_check_runs(), ignored):
            seen_any = True
            if cr.status != "completed":
                pending = True
            elif cr.conclusion in FAILED_CONCLUSIONS:
                return "failing"
        combined = commit.get_combined_status()
        if combined.total_count:
            seen_any = True
            if combined.state in ("failure", "error"):
                return "failing"
            if combined.state == "pending":
                pending = True
        if not seen_any:
            return "none"
        return "pending" if pending else "passing"

    def merge(self, repo: str, number: int, method: str, sha: str) -> str:
        pr = self._repo(repo).get_pull(number)
        status = pr.merge(merge_method=method, sha=sha)
        if not status.merged:
            raise RuntimeError(f"merge of {repo}#{number} refused: {status.message}")
        return status.sha

    def branch_head(self, repo: str, branch: str) -> str:
        return self._repo(repo).get_branch(branch).commit.sha

    def failed_runs(self, repo: str, sha: str) -> list[FailedRun]:
        out = []
        for run in self._repo(repo).get_workflow_runs(head_sha=sha):
            if run.event in IGNORED_EVENTS:
                continue
            if run.status == "completed" and run.conclusion in FAILED_CONCLUSIONS:
                out.append(FailedRun(run.id, run.name or "", run.html_url, run.path or ""))
        return out

    def run_logs(self, repo: str, run_id: int) -> str:
        run = self._repo(repo).get_workflow_run(run_id)
        chunks = []
        for job in run.jobs():
            if job.conclusion not in FAILED_CONCLUSIONS:
                continue
            failed_steps = [s.name for s in job.steps if s.conclusion in FAILED_CONCLUSIONS]
            try:
                resp = requests.get(job.logs_url(), timeout=60)
                resp.raise_for_status()
                lines = resp.text.splitlines()
            except (requests.RequestException, GithubException) as e:
                lines = [f"<could not download logs: {e}>"]
            # Strip the ISO timestamp GitHub prefixes to every line.
            lines = [ln[29:] if len(ln) > 29 and ln[4] == "-" and ln[28] == " " else ln for ln in lines]
            tail = "\n".join(lines[-LOG_TAIL_LINES:])
            chunks.append(f"### job: {job.name}\nfailed steps: {failed_steps}\n{tail}")
        return "\n\n".join(chunks)

    def file_at(self, repo: str, path: str, ref: str) -> str:
        try:
            return self._repo(repo).get_contents(path, ref=ref).decoded_content.decode()
        except GithubException:
            return ""

    def commit_patch(self, repo: str, sha: str) -> tuple[str, str]:
        """Return (commit message, unified-ish patch) for a commit."""
        c = self._repo(repo).get_commit(sha)
        parts = [f"--- {f.filename}\n{f.patch or '<binary or too large>'}" for f in c.files]
        return c.commit.message, "\n".join(parts)

    def open_pr(self, repo: str, head: str, base: str, title: str, body: str, labels: list[str]) -> PRInfo:
        r = self._repo(repo)
        pr = r.create_pull(base=base, head=head, title=title, body=body)
        if labels:
            try:
                pr.add_to_labels(*labels)
            except GithubException:
                pass  # label may not exist and we may lack rights to create it
        return self._to_info(repo, pr)

    def comment(self, repo: str, number: int, body: str) -> None:
        self._repo(repo).get_issue(number).create_comment(body)
