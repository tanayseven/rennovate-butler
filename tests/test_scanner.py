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


# ── PRs without a Merge Confidence badge ─────────────────────────────────────

ACTION_MAJOR = (
    "This PR contains the following updates:\n\n| Package | Type | Update | Change |\n|---|---|---|---|\n"
    "| [actions/checkout](https://redirect.github.com/actions/checkout) | action | major | `v6` → `v7` |\n\n---\n"
)
NODE_PATCH = ACTION_MAJOR.replace("actions/checkout", "node").replace(
    "action | major | `v6` → `v7`", "tools | patch | `24.22.0` → `24.22.3`"
)
UV_ZERO_MINOR = NODE_PATCH.replace("patch | `24.22.0` → `24.22.3`", "minor | `0.11.11` → `0.13.10`")
LOCK_FILES = (
    "| Update | Change |\n|---|---|\n| lockFileMaintenance | All locks refreshed |\n\n🔧 This PR updates lock files"
)
BADGELESS = MergeConfig(authors=["renovate[bot]"], badgeless_update_types=["patch", "minor", "lockFileMaintenance"])


@pytest.mark.parametrize("body", [NODE_PATCH, LOCK_FILES])
def test_badgeless_allowed_types_merge(body):
    v = evaluate(make_pr(1, body=body), "passing", BADGELESS, HIGH)
    assert v.merge and v.confidence == "none", v.reason


@pytest.mark.parametrize(
    "body, reason",
    [
        (ACTION_MAJOR, "no badge; update type ['major'] not allowed without one"),
        (UV_ZERO_MINOR, "no badge; update type ['major']"),  # 0.x minor counts as major
        ("no table at all", "no Merge Confidence badges or Renovate update table"),
    ],
)
def test_badgeless_skips(body, reason):
    v = evaluate(make_pr(1, body=body), "passing", BADGELESS, HIGH)
    assert not v.merge and reason in v.reason


def test_badgeless_off_by_default_and_ci_still_required():
    assert "no Merge Confidence" in evaluate(make_pr(1, body=NODE_PATCH), "passing", CFG, HIGH).reason
    assert evaluate(make_pr(1, body=NODE_PATCH), "none", BADGELESS, HIGH).reason == "CI none"


def test_badged_prs_still_use_confidence_rules():
    v = evaluate(make_pr(1, body=body_for("left-pad", "1.1.3", "1.1.4")), "passing", BADGELESS, HIGH)
    assert not v.merge and "confidence neutral < high" in v.reason


def test_blocked_only_by_ci():
    from renovate_butler.scanner import blocked_only_by_ci

    def red(**kw):
        return evaluate(make_pr(1, **kw), "failing", CFG, HIGH)

    assert blocked_only_by_ci(red(), CFG, HIGH)
    assert blocked_only_by_ci(red(mergeable=None, mergeable_state="unknown"), CFG, HIGH)
    assert not blocked_only_by_ci(red(author="someone"), CFG, HIGH)
    assert not blocked_only_by_ci(red(body=body_for("lodash", "3.0.0", "4.0.0")), CFG, HIGH)  # major not allowed
    assert not blocked_only_by_ci(evaluate(make_pr(1), "pending", CFG, HIGH), CFG, HIGH)
