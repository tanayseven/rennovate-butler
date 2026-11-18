"""Command line entry point."""

from __future__ import annotations

import sys

import click

from . import config as config_mod
from . import scanner
from .github_client import GitHubClient
from .orchestrator import Butler, EventLog, RepoResult


def _setup(config_path: str, dry_run: bool):
    cfg = config_mod.load(config_path)
    cfg.dry_run = cfg.dry_run or dry_run
    token = config_mod.github_token()
    return cfg, token, GitHubClient(token)


def _fixer(cfg, token, log):
    from .fixer import OpenRouterFixer

    fx = OpenRouterFixer(
        token=token,
        api_key=config_mod.openrouter_key(),
        model=cfg.fix.model,
        max_turns=cfg.fix.max_tool_turns,
        allow_checks=cfg.fix.allow_local_checks,
        log=log,
    )
    fx.check_model()
    return fx


def _summary(results: list[RepoResult]) -> int:
    click.echo("\n== summary")
    ok = True
    for r in results:
        ok &= r.main_green
        merged = ", ".join(f"#{n}" for n in r.merged) or "none"
        fixes = ", ".join(f"#{n}" for n in r.fix_prs) or "none"
        status = "green" if r.main_green else "NOT green"
        click.echo(f"  {r.repo}: main {status}; merged {merged}; fix PRs {fixes}{'; ' + r.note if r.note else ''}")
    return 0 if ok else 1


@click.group()
@click.option("--config", "config_path", default="butler.yaml", show_default=True, type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Report decisions without merging or opening PRs.")
@click.pass_context
def main(ctx: click.Context, config_path: str, dry_run: bool) -> None:
    """Merge green, high-confidence Renovate PRs and keep main green."""
    ctx.obj = {"config": config_path, "dry_run": dry_run}


@main.command()
@click.pass_context
def scan(ctx: click.Context) -> None:
    """Report CI state and merge decision for every open PR."""
    cfg, _, gh = _setup(ctx.obj["config"], True)
    for repo in cfg.repos:
        click.echo(f"== {repo}")
        click.echo(scanner.format_report(scanner.scan(gh, repo, cfg.merge)))


@main.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Merge confident green PRs one by one; heal main whenever it goes red."""
    cfg, token, gh = _setup(ctx.obj["config"], ctx.obj["dry_run"])
    log = EventLog(cfg.log_file, click.echo)
    fixer = None if cfg.dry_run else _fixer(cfg, token, click.echo)
    results = Butler(gh, fixer, cfg, log).run()
    sys.exit(_summary(results))


@main.command()
@click.option("--repo", required=True, help="owner/repo whose main branch is red.")
@click.pass_context
def fix(ctx: click.Context, repo: str) -> None:
    """Run the heal loop on one repo without merging any Renovate PRs."""
    cfg, token, gh = _setup(ctx.obj["config"], ctx.obj["dry_run"])
    log = EventLog(cfg.log_file, click.echo)
    fixer = None if cfg.dry_run else _fixer(cfg, token, click.echo)
    butler = Butler(gh, fixer, cfg, log)
    res = RepoResult(repo)
    head = gh.branch_head(repo, cfg.main_branch)
    state = gh.ci_state(repo, head)
    if state != "failing":
        click.echo(f"{repo} {cfg.main_branch} CI is {state}; nothing to fix.")
        return
    res.main_green = butler.heal(repo, res)
    sys.exit(_summary([res]))


if __name__ == "__main__":
    main()
