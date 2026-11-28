# renovate-butler

Merges green, high-confidence Renovate PRs one at a time, watches `main` after each merge, and if `main` goes red, asks Claude Sonnet 5.5 (via OpenRouter) for a fix PR — retrying until `main` is green or the attempt cap is hit. Renovate PRs whose own CI is red get their failed jobs re-run once, and if they are still red, the same agent repairs them on their own branch before they are merged.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org)
[![uv](https://img.shields.io/badge/uv-0.11.30-DE5FE9?style=flat&logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![mise](https://img.shields.io/badge/mise-managed-1E1E2E?style=flat)](https://mise.jdx.dev)
[![Ruff](https://img.shields.io/badge/lint-Ruff-D7FF64?style=flat&logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)
[![pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?style=flat&logo=pytest&logoColor=white)](https://docs.pytest.org)
[![PyGithub](https://img.shields.io/badge/PyGithub-2.10-181717?style=flat&logo=github&logoColor=white)](https://pygithub.readthedocs.io)
[![Claude Sonnet 5.5](https://img.shields.io/badge/Claude-Sonnet%205.5-D97757?style=flat&logo=claude&logoColor=white)](https://www.anthropic.com/claude)
[![OpenRouter](https://img.shields.io/badge/via-OpenRouter-6566F1?style=flat)](https://openrouter.ai)
[![Discord](https://img.shields.io/badge/alerts-Discord-5865F2?style=flat&logo=discord&logoColor=white)](#discord)

[![renovate-butler](https://github.com/tanayseven/rennovate-butler/actions/workflows/renovate-butler.yml/badge.svg)](https://github.com/tanayseven/rennovate-butler/actions/workflows/renovate-butler.yml)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-2088FF?style=flat&logo=githubactions&logoColor=white)](https://github.com/tanayseven/rennovate-butler/actions/workflows/renovate-butler.yml)
[![Schedule](https://img.shields.io/badge/runs-daily%2020%3A00%20IST-2088FF?style=flat&logo=clockify&logoColor=white)](.github/workflows/renovate-butler.yml)

[![Renovate](https://img.shields.io/badge/Renovate-Mend-1A1F6C?style=flat&logo=renovate&logoColor=white)](https://developer.mend.io/github/tanayseven)
[![Merge Confidence](https://img.shields.io/badge/Merge%20Confidence-gated-2EA44F?style=flat)](#how-decisions-are-made)

## Renovate links

- [Mend developer portal for tanayseven](https://developer.mend.io/github/tanayseven): every repo's Renovate status, job logs and problems (e.g. "Unexpected downgrade detected").
- [Mend Renovate app installation](https://github.com/apps/renovate/installations/new): choose which repos Renovate can access.

## Setup

```sh
mise install          # python 3.12 + uv (pinned in mise.toml / mise.lock)
uv sync
cp butler.example.yaml butler.yaml   # list your repos
cp .env.example .env                 # OPENROUTER_API_KEY; GITHUB_TOKEN optional (falls back to `gh auth token`)
```

## Usage

```sh
uv run renovate-butler scan                 # report only: CI state, confidence, merge decision per PR
uv run renovate-butler --dry-run run        # full loop, but no merges / no fix PRs
uv run renovate-butler run                  # merge + watch main + heal
uv run renovate-butler fix --repo owner/r   # only heal a red main
```

Exit code is non-zero if any repo's `main` is not green at the end. Every action is appended to `butler.log.jsonl`.

## Adding a repo

1. Add the repo to the [Mend Renovate GitHub app](https://github.com/apps/renovate/installations/new) (pick your account, then add the repo under **Repository access**) and merge the onboarding PR Renovate opens there. The [Mend developer portal](https://developer.mend.io/github/tanayseven) shows each repo's Renovate status and logs.
2. Make sure the `BUTLER_GITHUB_TOKEN` PAT can reach the repo. A fine-grained PAT has to list it.
3. Add it to `butler.yaml` with [`scripts/add_repo.py`](scripts/add_repo.py):

   ```sh
   uv run scripts/add_repo.py owner/repo [owner/other ...]   # branch + commit + PR
   uv run scripts/add_repo.py owner/repo --no-pr             # only edit butler.yaml
   ```

   The PR's `renovate-butler` check runs the butler with `--dry-run` across every configured repo. It shows what would be merged, and it fails if the token can't reach a repo or any `main` is red.

In Claude Code, the [`add-repo` skill](.claude/skills/add-repo/SKILL.md) does the same: it runs the script, waits for the dry run and summarises the result.

## How decisions are made

A PR is merged only if **all** hold:
- author is in `merge.authors` (Renovate), not a draft, no `skip_labels`, no changes-requested review, no conflicts;
- every check run and commit status on the head SHA passed;
- the PR body has Mend Merge Confidence badges and the **weakest** one is ≥ `min_confidence`;
- every update's semver bump (from the badge's from/to versions; 0.x minor counts as major) is in `allowed_update_types`;
- GitHub reports it mergeable (`clean`/`unstable`/`has_hooks`).

GitHub works out mergeability lazily: the first read of a PR it hasn't checked recently answers `unknown` and starts the check. The butler re-reads each PR for a few seconds until it has an answer.

Mend only publishes Merge Confidence for package registries, so PRs for GitHub Actions, tools in `mise.toml`, Docker images and lock file maintenance carry no badge. For those, the butler reads the update type from the table Renovate puts at the top of the PR (`| Package | Type | Update | Change |`) and merges only if it is in `merge.badgeless_update_types` (here: patch, minor and `lockFileMaintenance`). A 0.x minor bump still counts as major. Every other rule above, green CI included, still applies. With `badgeless_update_types: []`, badge-less PRs are skipped.

Confidence badges are images (the word is rendered into a PNG), so `confidence.py` decodes the PNG and classifies by background colour (red=low, grey=neutral, green=high) and badge width (very high is wider).

## Healing loop

When `main` is red: collect the failing jobs' logs, the workflow YAML and the last commit's diff, then run a tool-use agent on a temporary clone. The agent has `list_dir`, `read_file`, `grep`, `apply_edit`, `write_file` and `finish`, all confined to the clone, plus `run_check` if `fix.allow_local_checks` is true (runs the workflow's own `run:` steps, minus any touching secrets, with secret-looking env vars stripped). The change is pushed to `butler/fix-main-<sha>-<ts>` and opened as a PR. If its CI fails, the next attempt continues on the same branch with the new logs. When green it is merged and `main` is watched again.

## Red Renovate PRs

A Renovate PR held back **only** by failing CI (it would merge if CI were green) is not just skipped:

1. **Re-run once.** Before merging the green PRs, the butler re-runs the failed jobs of the PR's failed GitHub Actions runs (`push`/`pull_request`, not scheduled or manual ones). Only first attempts are re-run, so each failure is retried once, even across butler runs. Flaky tests usually pass here.
2. **Repair.** After the green PRs are merged, it waits for the re-runs. If CI is still red (or the failure was already re-run), the healing agent gets the PR's failing logs, workflows, latest commit and description, and pushes a `fix(deps): …` commit to the Renovate branch itself. It retries up to `fix.max_attempts`, then gives up and alerts. Renovate stops rebasing a branch someone else has pushed to, which is fine for a PR that's about to be merged.
3. **Merge.** Once green, the PR goes through the same checks as any other and is merged, and `main` is watched as usual.

PRs skipped for any other reason (author, label, conflicts, confidence, update type) are left alone. A dry run only reports what it would do.

## Discord

Set `DISCORD_WEBHOOK_URL` (a channel webhook: Edit Channel → Integrations → Webhooks) and the butler posts:

- a summary after every run (green when every repo's `main` is green, red otherwise);
- one alert per merged Renovate PR, once CI on `main` has run on the merge: passed ✅, failed ❌ (the fix loop starts), no CI, or didn't finish in time;
- live alerts when `main` is already red at the start, a fix PR is opened or merged, a red Renovate PR is repaired or given up on, the butler gives up, or the run crashes.

Model cost is shown in the alerts: fix alerts carry the tokens (in/out) and dollar cost of that fix so far, as billed by OpenRouter, and name the Renovate PR whose merge broke `main`; merge alerts show $0, since merging never calls the model. The run summary adds the cost per repo and the total. The same numbers (`llm_calls`, `input_tokens`, `output_tokens`, `cost_usd`, `cause_pr`) are on the fix events in `butler.log.jsonl`.

Messages never ping anyone, and errors never print the webhook URL. Use `assets/logo.png` as the webhook's avatar. Unset, nothing is posted.

## Development

```sh
mise run test
mise run lint
```
