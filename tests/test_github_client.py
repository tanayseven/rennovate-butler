from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from renovate_butler.github_client import ignored_suites, latest_check_runs

T0 = datetime(2026, 11, 26, 4, 0)


def run(event: str, suite: int):
    return SimpleNamespace(event=event, check_suite_id=suite)


def check(name: str, suite: int, conclusion: str = "success", minutes: int = 0):
    return SimpleNamespace(
        name=name,
        check_suite_id=suite,
        conclusion=conclusion,
        status="completed",
        started_at=T0 + timedelta(minutes=minutes),
    )


def test_ignores_scheduled_and_manual_runs():
    # tech-events-tracker main on 2026-11-26: the push build passed, a manual URL check
    # passed, then a later manual URL check failed because the search engine blocked it.
    runs = [run("push", 1), run("workflow_dispatch", 2), run("workflow_dispatch", 3), run("schedule", 4)]
    checks = [
        check("build", 1),
        check("verify", 2, minutes=10),
        check("verify", 3, "failure", minutes=20),
        check("nightly", 4, "failure"),
    ]
    latest = latest_check_runs(checks, ignored_suites(runs))
    assert [(c.name, c.conclusion) for c in latest] == [("build", "success")]


def test_keeps_push_pull_request_and_non_actions_checks():
    runs = [run("push", 1), run("pull_request", 2)]
    checks = [check("test", 1), check("lint", 2), check("codecov", 99)]  # 99: a third-party app, no workflow run
    assert {c.name for c in latest_check_runs(checks, ignored_suites(runs))} == {"test", "lint", "codecov"}


def test_rerun_supersedes_earlier_run_of_same_name():
    checks = [check("test", 1, "failure", minutes=0), check("test", 1, "success", minutes=5)]
    assert [c.conclusion for c in latest_check_runs(checks, set())] == ["success"]


def test_dependency_graph_runs_are_ignored():
    assert ignored_suites([run("dynamic", 7), run("push", 8)]) == {7}


def test_waits_for_github_to_compute_mergeability(monkeypatch):
    # diwali 2026-11-27: the first read of a green PR said mergeable_state=unknown, the next
    # one seconds later said clean, and the butler had skipped the PR on the first answer.
    from renovate_butler import github_client

    monkeypatch.setattr(github_client.time, "sleep", lambda s: None)
    answers = iter([(None, "unknown"), (True, "unknown"), (True, "clean")])
    reads = []

    class Repo:
        def get_pull(self, number):
            mergeable, state = next(answers)
            reads.append(number)
            return SimpleNamespace(mergeable=mergeable, mergeable_state=state)

    pr = github_client.GitHubClient._settled_pull(Repo(), 7)
    assert (pr.mergeable, pr.mergeable_state) == (True, "clean")
    assert reads == [7, 7, 7]


def test_reruns_only_first_attempts_of_failed_push_and_pr_runs():
    from renovate_butler.github_client import GitHubClient

    def wr(id, event="pull_request", conclusion="failure", attempt=1, status="completed"):
        return SimpleNamespace(
            id=id,
            event=event,
            conclusion=conclusion,
            run_attempt=attempt,
            status=status,
            rerun_failed_jobs=lambda: True,
        )

    runs = [
        wr(1),
        wr(2, attempt=2),
        wr(3, conclusion="success"),
        wr(4, event="schedule"),
        wr(5, status="in_progress"),
        wr(6, conclusion="cancelled"),
    ]
    client = GitHubClient.__new__(GitHubClient)
    client._repo = lambda repo: SimpleNamespace(get_workflow_runs=lambda head_sha: runs)
    assert client.rerun_failed("o/r", "sha") == [1, 6]
