import pytest

from renovate_butler.config import MergeConfig
from renovate_butler.scanner import evaluate, format_report, scan

from .conftest import FakeForge, badge_fetcher, body_for, make_pr

HIGH = badge_fetcher({"lodash": "high", "react": "low", "left-pad": "neutral"})
CFG = MergeConfig(authors=["renovate[bot]"])


def test_passing_high_patch_merges():
    v = evaluate(make_pr(1), "passing", CFG, HIGH)
    assert v.merge and v.reason == "ok" and v.confidence == "high"


@pytest.mark.parametrize(
    "pr_kw, ci, reason",
    [
        ({"author": "someone"}, "passing", "not a Renovate bot"),
        ({"draft": True}, "passing", "draft"),
        ({"labels": ["do-not-merge"]}, "passing", "label"),
        ({"changes_requested": True}, "passing", "changes requested"),
        ({"mergeable": False, "mergeable_state": "dirty"}, "passing", "conflicts"),
        ({}, "failing", "CI failing"),
        ({}, "pending", "CI pending"),
        ({}, "none", "CI none"),
        ({"body": "no table"}, "passing", "no Merge Confidence"),
        ({"body": body_for("left-pad", "1.1.3", "1.1.4")}, "passing", "confidence neutral < high"),
        ({"body": body_for("lodash", "3.0.0", "4.0.0")}, "passing", "update type ['major']"),
        ({"mergeable": None, "mergeable_state": "unknown"}, "passing", "mergeable_state=unknown"),
        ({"mergeable_state": "blocked"}, "passing", "mergeable_state=blocked"),
    ],
)
def test_skip_reasons(pr_kw, ci, reason):
    v = evaluate(make_pr(1, **pr_kw), ci, CFG, HIGH)
    assert not v.merge
    assert reason in v.reason


def test_scan_classifies_each_pr():
    forge = FakeForge([make_pr(1), make_pr(2), make_pr(3)])
    forge.set_ci("head2", "failing")
    forge.set_ci("head3", "pending")
    vs = scan(forge, "o/r", CFG, HIGH)
    assert [(v.pr.number, v.ci, v.merge) for v in vs] == [
        (1, "passing", True),
        (2, "failing", False),
        (3, "pending", False),
    ]
    report = format_report(vs)
    assert "MERGE" in report and "skip: CI failing" in report
