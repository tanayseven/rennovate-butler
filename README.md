# renovate-butler

Merges green, high-confidence Renovate PRs one at a time, watches `main` after each merge, and if `main` goes red, asks Claude Sonnet 5.5 (via OpenRouter) for a fix PR — retrying until `main` is green or the attempt cap is hit.

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

1. Add the repo to the [Mend Renovate GitHub app](https://github.com/apps/renovate/installations/new) (pick your account, then add the repo under **Repository access**) and merge the onboarding PR Renovate opens there. The [Mend developer portal](https://developer.mend.io/) shows each repo's Renovate status and logs.
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

PRs with no confidence badges (e.g. GitHub Actions, Docker, lock file maintenance) are skipped.

Confidence badges are images (the word is rendered into a PNG), so `confidence.py` decodes the PNG and classifies by background colour (red=low, grey=neutral, green=high) and badge width (very high is wider).

## Healing loop

When `main` is red: collect the failing jobs' logs, the workflow YAML and the last commit's diff, then run a tool-use agent on a temporary clone. The agent has `list_dir`, `read_file`, `grep`, `apply_edit`, `write_file` and `finish`, all confined to the clone, plus `run_check` if `fix.allow_local_checks` is true (runs the workflow's own `run:` steps, minus any touching secrets, with secret-looking env vars stripped). The change is pushed to `butler/fix-main-<sha>-<ts>` and opened as a PR. If its CI fails, the next attempt continues on the same branch with the new logs. When green it is merged and `main` is watched again.

## Discord

Set `DISCORD_WEBHOOK_URL` (a channel webhook: Edit Channel → Integrations → Webhooks) and the butler posts:

- a summary after every run (green when every repo's `main` is green, red otherwise);
- one alert per merged Renovate PR, once CI on `main` has run on the merge: passed ✅, failed ❌ (the fix loop starts), no CI, or didn't finish in time;
- live alerts when `main` is already red at the start, a fix PR is opened or merged, the butler gives up, or the run crashes.

Model cost is shown in the alerts: fix alerts carry the tokens (in/out) and dollar cost of that fix so far, as billed by OpenRouter, and name the Renovate PR whose merge broke `main`; merge alerts show $0, since merging never calls the model. The run summary adds the cost per repo and the total. The same numbers (`llm_calls`, `input_tokens`, `output_tokens`, `cost_usd`, `cause_pr`) are on the fix events in `butler.log.jsonl`.

Messages never ping anyone, and errors never print the webhook URL. Use `assets/logo.png` as the webhook's avatar. Unset, nothing is posted.

## Development

```sh
mise run test
mise run lint
```
