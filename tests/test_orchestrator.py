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


def test_bad_repo_is_reported_and_others_still_run(no_sleep):
    forge = FakeForge([make_pr(1)])
    orig = forge.branch_head

    class NotFound(Exception):
        status = 404

    def branch_head(repo, branch):
        if repo == "o/missing":
            raise NotFound("Not Found")
        return orig(repo, branch)

    forge.branch_head = branch_head
    c = cfg()
    c.repos = ["o/missing", "o/r"]
    results = butler(forge, None, c, no_sleep).run()
    assert [r.main_green for r in results] == [False, True]
    assert "not found" in results[0].note
    assert forge.merged == [1]


def test_result_counts_skipped_and_would_merge(no_sleep):
    forge = FakeForge([make_pr(1), make_pr(2, draft=True)])
    res = butler(forge, None, cfg(dry_run=True), no_sleep).process_repo("o/r")
    assert res.would_merge == [1]
    assert res.skipped == 1


def test_logs_main_ci_outcome_after_each_merge(no_sleep):
    forge = FakeForge([make_pr(1), make_pr(2)])
    forge.set_ci("merge2", "pending", "failing")  # main goes red after PR 2
    forge.open_pr = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fix PR expected"))
    events = []
    sleep, now = no_sleep
    b = Butler(
        forge,
        FakeFixer(forge, changed=False),
        cfg(),
        log=lambda m, **e: events.append(e),
        sleep=sleep,
        now=now,
        fetch=FETCH,
    )
    b.process_repo("o/r")
    merge_ci = [(e["pr"], e["outcome"], e["title"]) for e in events if e.get("event") == "merge_ci"]
    assert merge_ci == [(1, "green", "update dep 1"), (2, "red", "update dep 2")]


def test_fix_usage_accumulates_per_incident_and_is_attributed_to_the_merge(no_sleep):
    from renovate_butler.fixer import Usage

    forge = FakeForge([make_pr(1)])
    forge.ci_state = lambda repo, sha: "failing" if ("@" in sha or sha == "merge1") else "passing"
    fixer = FakeFixer(forge)
    orig_fix = fixer.fix

    def fix(*a, **k):
        r = orig_fix(*a, **k)
        r.usage = Usage(calls=5, input_tokens=10_000, output_tokens=500, cost_usd=0.04)
        return r

    fixer.fix = fix
    events = []
    sleep, now = no_sleep
    c = cfg()
    c.fix.max_attempts = 2
    b = Butler(forge, fixer, c, log=lambda m, **e: events.append(e), sleep=sleep, now=now, fetch=FETCH)
    res = b.process_repo("o/r")

    by_kind = {e["event"]: e for e in events if e.get("event")}
    assert by_kind["fix_pr"]["cost_usd"] == 0.04 and by_kind["fix_pr"]["cause_pr"] == 1
    gave_up = by_kind["fix_gave_up"]
    assert (gave_up["llm_calls"], gave_up["input_tokens"], gave_up["output_tokens"]) == (10, 20_000, 1_000)
    assert gave_up["cost_usd"] == 0.08
    assert (res.usage.calls, round(res.usage.cost_usd, 2)) == (10, 0.08)
