from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from renovate_butler.github_client import FailedRun, PRInfo

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def badge_fetcher(level_by_pkg: dict[str, str]):
    """Return a fetch() that serves a real badge SVG chosen by package name."""

    def fetch(url: str) -> str:
        for pkg, level in level_by_pkg.items():
            if f"/{pkg}/" in url:
                return fixture(f"badge_{level}.svg")
        raise AssertionError(f"unexpected badge url {url}")

    return fetch


def body_for(pkg: str, frm: str, to: str) -> str:
    return (
        "| Package | Change | Confidence |\n|---|---|---|\n"
        f"| {pkg} | `{frm}` -> `{to}` | ![confidence](https://developer.mend.io/api/mc/badges/confidence/npm/{pkg}/{frm}/{to}?slim=true) |"
    )


def make_pr(number: int, **kw) -> PRInfo:
    base = PRInfo(
        repo="o/r",
        number=number,
        title=f"update dep {number}",
        author="renovate[bot]",
        head_sha=f"head{number}",
        head_ref=f"renovate/{number}",
        base_ref="main",
        url=f"https://github.com/o/r/pull/{number}",
        body=body_for("lodash", "4.17.20", "4.17.21"),
        mergeable=True,
        mergeable_state="clean",
    )
    return replace(base, **kw)


class FakeForge:
    """In-memory GitHub. CI states per sha are scripted lists (popped in order)."""

    def __init__(self, prs: list[PRInfo], main_head: str = "main0"):
        self.prs = {p.number: p for p in prs}
        self.main_head = main_head
        self.ci: dict[str, list[str]] = {}
        self.merged: list[int] = []
        self.opened: list[PRInfo] = []
        self.comments: list[tuple[int, str]] = []
        self.merge_count = 0
        self.next_pr = 900
        self.reruns: list[str] = []  # head SHAs whose failed jobs were re-run
        self.rerunnable = True  # False: the failures were already re-run once

    def set_ci(self, sha: str, *states: str) -> None:
        self.ci[sha] = list(states)

    def open_prs(self, repo):
        return [p for p in self.prs.values() if p.number not in self.merged]

    def get_pr(self, repo, number):
        return self.prs[number]

    def ci_state(self, repo, sha):
        states = self.ci.get(sha, ["passing"])
        return states.pop(0) if len(states) > 1 else states[0]

    def merge(self, repo, number, method, sha):
        assert self.prs[number].head_sha == sha
        self.merge_count += 1
        self.merged.append(number)
        self.main_head = f"merge{self.merge_count}"
        return self.main_head

    def branch_head(self, repo, branch):
        return self.main_head

    def failed_runs(self, repo, sha):
        return [FailedRun(1, "CI", "https://example/run/1", ".github/workflows/ci.yml")]

    def rerun_failed(self, repo, sha):
        if not self.rerunnable or sha in self.reruns:
            return []
        self.reruns.append(sha)
        return [1]

    def run_logs(self, repo, run_id):
        return "E   AssertionError: boom"

    def file_at(self, repo, path, ref):
        return "jobs:\n  test:\n    steps:\n      - run: pytest\n"

    def commit_patch(self, repo, sha):
        return ("chore(deps): update lodash", "--- package.json\n-4.17.20\n+4.17.21")

    def open_pr(self, repo, head, base, title, body, labels):
        self.next_pr += 1
        pr = make_pr(self.next_pr, title=title, author="tanayseven", head_sha=f"{head}@1", head_ref=head, body=body)
        self.prs[pr.number] = pr
        self.opened.append(pr)
        return pr

    def comment(self, repo, number, body):
        self.comments.append((number, body))

    def push(self, branch: str) -> None:
        """Simulate a new commit on a fix branch."""
        for p in self.prs.values():
            if p.head_ref == branch:
                n = int(p.head_sha.rsplit("@", 1)[1]) + 1
                self.prs[p.number] = replace(p, head_sha=f"{branch}@{n}")


@pytest.fixture
def no_sleep():
    t = [0.0]

    def sleep(s):
        t[0] += s

    return sleep, lambda: t[0]
