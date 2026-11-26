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
