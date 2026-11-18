from renovate_butler.config import Config, MergeConfig
from renovate_butler.fixer import FixResult
from renovate_butler.orchestrator import Butler

from .conftest import FakeForge, badge_fetcher, make_pr

FETCH = badge_fetcher({"lodash": "high"})


class FakeFixer:
    def __init__(self, forge, changed=True):
        self.forge = forge
        self.changed = changed
        self.calls = []

    def fix(self, repo, base, branch, continue_branch, context, workflows):
        self.calls.append({"branch": branch, "continue": continue_branch, "context": context})
        if continue_branch and self.changed:
            self.forge.push(branch)
        return FixResult(branch, self.changed, f"fix {len(self.calls)}", "explained")


def cfg(**kw):
    c = Config(repos=["o/r"], merge=MergeConfig(authors=["renovate[bot]"]), **kw)
    return c


def butler(forge, fixer, c, no_sleep):
    sleep, now = no_sleep
    return Butler(forge, fixer, c, log=lambda *a, **k: None, sleep=sleep, now=now, fetch=FETCH)


def test_merges_then_heals_red_main(no_sleep):
    forge = FakeForge([make_pr(1), make_pr(2)])
    forge.set_ci("merge2", "pending", "failing")  # main goes red after PR 2
    fixer = FakeFixer(forge)
    c = cfg()

    # First fix push fails CI, the second passes.
    orig_open = forge.open_pr

    def open_pr(*a, **k):
        pr = orig_open(*a, **k)
        forge.set_ci(pr.head_sha, "failing")
        return pr

    forge.open_pr = open_pr
    res = butler(forge, fixer, c, no_sleep).process_repo("o/r")

    assert res.main_green, res.note
    assert res.merged == [1, 2]
    assert res.fix_prs == [901]
    assert forge.merged == [1, 2, 901]
    assert [call["continue"] for call in fixer.calls] == [False, True]
    assert "AssertionError: boom" in fixer.calls[0]["context"]
    assert "Previous attempts" in fixer.calls[1]["context"]


def test_gives_up_after_max_attempts(no_sleep):
    forge = FakeForge([make_pr(1)])
    forge.set_ci("merge1", "failing")
    fixer = FakeFixer(forge)
    orig_open = forge.open_pr

    def open_pr(*a, **k):
        pr = orig_open(*a, **k)
        return pr

    forge.open_pr = open_pr
    # Every head on the fix branch fails.
    forge.ci_state = lambda repo, sha: "failing" if ("@" in sha or sha == "merge1") else "passing"
    c = cfg()
    c.fix.max_attempts = 2
    res = butler(forge, fixer, c, no_sleep).process_repo("o/r")

    assert not res.main_green
    assert "gave up after 2" in res.note
    assert forge.merged == [1]
    assert forge.comments and forge.comments[0][0] == 901


def test_fixer_that_changes_nothing_opens_no_pr(no_sleep):
    forge = FakeForge([make_pr(1)])
    forge.set_ci("merge1", "failing")
    c = cfg()
    res = butler(forge, FakeFixer(forge, changed=False), c, no_sleep).process_repo("o/r")
    assert not res.main_green
    assert forge.opened == []


def test_heals_preexisting_red_main_before_merging(no_sleep):
    forge = FakeForge([make_pr(1)], main_head="red0")
    forge.set_ci("red0", "failing")
    fixer = FakeFixer(forge)
    res = butler(forge, fixer, cfg(), no_sleep).process_repo("o/r")
    assert res.main_green
    assert forge.merged == [901, 1]  # fix first, then the Renovate PR


def test_dry_run_merges_nothing(no_sleep):
    forge = FakeForge([make_pr(1)])
    c = cfg(dry_run=True)
    res = butler(forge, None, c, no_sleep).process_repo("o/r")
    assert forge.merged == []
    assert res.main_green


def test_skips_pr_that_changed_since_scan(no_sleep):
    forge = FakeForge([make_pr(1)])
    orig = forge.get_pr
    forge.get_pr = lambda repo, n: orig(repo, n).__class__(
        **{**orig(repo, n).__dict__, "mergeable": False, "mergeable_state": "dirty"}
    )
    res = butler(forge, None, cfg(), no_sleep).process_repo("o/r")
    assert forge.merged == []
    assert res.main_green


def test_main_timeout_stops(no_sleep):
    forge = FakeForge([make_pr(1)])
    forge.set_ci("main0", "pending")
    res = butler(forge, None, cfg(), no_sleep).process_repo("o/r")
    assert not res.main_green and "did not settle" in res.note
    assert forge.merged == []
