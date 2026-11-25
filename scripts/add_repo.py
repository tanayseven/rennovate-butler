"""Add repos to butler.yaml and open a PR; the PR's CI runs the butler as a dry run.

    uv run scripts/add_repo.py owner/repo [owner/other ...]
    uv run scripts/add_repo.py owner/repo --no-pr      # only edit butler.yaml

Needs `git` and an authenticated `gh`. The repo must already be in the Mend Renovate
app's installation (https://github.com/apps/renovate/installations/new) and the
BUTLER_GITHUB_TOKEN secret must be able to read and merge in it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "butler.yaml"
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
RENOVATE_CONFIGS = [
    "renovate.json",
    "renovate.json5",
    ".github/renovate.json",
    ".github/renovate.json5",
    ".gitlab/renovate.json",
    ".renovaterc",
    ".renovaterc.json",
    ".renovaterc.json5",
]


def add_repos(text: str, repos: list[str]) -> tuple[str, list[str]]:
    """Append repos to the top-level `repos:` list, keeping comments and layout. Returns (text, added)."""
    lines = text.splitlines(keepends=True)
    try:
        start = next(i for i, ln in enumerate(lines) if re.match(r"^repos:\s*(#.*)?$", ln))
    except StopIteration:
        raise ValueError("butler.yaml has no top-level 'repos:' list") from None

    items = []  # (line index, repo)
    indent = "  "
    for i in range(start + 1, len(lines)):
        m = re.match(r"^(\s+)-\s+([^\s#]+)", lines[i])
        if m:
            indent = m.group(1)
            items.append((i, m.group(2).strip("'\"")))
        elif lines[i].strip() and not lines[i].lstrip().startswith("#"):
            break

    existing = {r.lower() for _, r in items}
    added = []
    for r in repos:
        if r.lower() not in existing:
            existing.add(r.lower())
            added.append(r)

    at = (items[-1][0] if items else start) + 1
    new = [f"{indent}- {r}\n" for r in added]
    return "".join(lines[:at] + new + lines[at:]), added


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=check)


def check_repo(repo: str) -> None:
    """Fail if the repo is unreachable; warn if it has no Renovate config yet."""
    if sh("gh", "repo", "view", repo, "--json", "name", check=False).returncode != 0:
        sys.exit(f"error: cannot see {repo} with gh (typo, or no access?)")
    for path in RENOVATE_CONFIGS:
        if sh("gh", "api", f"repos/{repo}/contents/{path}", "--silent", check=False).returncode == 0:
            return
    print(
        f"warning: {repo} has no Renovate config yet. Add it to the Mend Renovate app "
        "(https://github.com/apps/renovate/installations/new) and merge its onboarding PR, "
        "or the butler will find nothing to merge.",
        file=sys.stderr,
    )


def open_pr(added: list[str], base: str) -> str:
    if sh("git", "status", "--porcelain", "--", "butler.yaml").stdout.strip():
        sys.exit("error: butler.yaml has uncommitted changes; commit or stash them first")
    start_ref = sh("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    branch = "add-repo/" + "-".join(r.split("/")[1] for r in added).lower()
    names = ", ".join(added)

    sh("git", "fetch", "origin", base)
    sh("git", "switch", "-c", branch, f"origin/{base}")
    try:
        CONFIG.write_text(add_repos(CONFIG.read_text(), added)[0])
        sh("git", "add", "butler.yaml")
        sh("git", "commit", "-m", f"config: add {names} to the butler")
        sh("git", "push", "-u", "origin", branch)
        body = (
            f"Adds {names} to `butler.yaml`.\n\n"
            "The `renovate-butler` check on this PR runs the butler as a **dry run** over every configured "
            "repo: it lists what would be merged and fails if the token can't reach a repo or a `main` is red.\n\n"
            "Before merging, check:\n"
            "- [ ] the repo is in the [Mend Renovate app installation](https://github.com/apps/renovate/installations/new)\n"
            "- [ ] `BUTLER_GITHUB_TOKEN` covers the repo (fine-grained PATs list repos explicitly)\n"
        )
        return sh(
            "gh", "pr", "create", "--base", base, "--head", branch, "--title", f"config: add {names}", "--body", body
        ).stdout.strip()
    finally:
        sh("git", "switch", start_ref, check=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="+", metavar="owner/repo")
    ap.add_argument("--no-pr", action="store_true", help="edit butler.yaml in place; no branch, commit or PR")
    ap.add_argument("--base", default="main", help="branch to open the PR against (default: main)")
    args = ap.parse_args()

    bad = [r for r in args.repos if not REPO_RE.match(r)]
    if bad:
        sys.exit(f"error: not owner/repo: {', '.join(bad)}")

    _, added = add_repos(CONFIG.read_text(), args.repos)
    for r in set(args.repos) - set(added):
        print(f"{r} is already in butler.yaml; skipping")
    if not added:
        return
    for r in added:
        check_repo(r)

    if args.no_pr:
        CONFIG.write_text(add_repos(CONFIG.read_text(), added)[0])
        print(f"added {', '.join(added)} to butler.yaml")
    else:
        print(open_pr(added, args.base))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: {' '.join(e.cmd)} failed:\n{e.stderr.strip()}")
