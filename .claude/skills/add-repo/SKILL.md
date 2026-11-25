---
name: add-repo
description: Add a GitHub repo to renovate-butler so its Renovate PRs get auto-merged. Use when the user wants the butler to look after another repo ("add owner/repo to the butler", "renovate this repo too").
---

# Add a repo to renovate-butler

The work is done by [`scripts/add_repo.py`](../../../scripts/add_repo.py). It appends the repo to `butler.yaml`, opens a PR, and the PR's `renovate-butler` check runs the butler as a dry run.

1. **Get the repo** as `owner/repo`. If the user gave a URL or a bare name, turn it into that form (bare names belong to `tanayseven`). Several repos can go in one PR.

2. **Run the script** from the repo root:

   ```sh
   uv run scripts/add_repo.py owner/repo [owner/other ...]
   ```

   It prints the PR URL. If it warns that the repo has no Renovate config, tell the user to add the repo to the [Mend Renovate app](https://github.com/apps/renovate/installations/new) and merge the onboarding PR Renovate opens there. The butler PR can still go ahead.

3. **Watch the dry run**:

   ```sh
   gh pr checks <pr-url> --watch
   ```

   When it finishes, read the run log (`gh run view <run-id> --log`) and tell the user what the butler would merge in the new repo.

   - If it failed with a 403/404 on the new repo, `BUTLER_GITHUB_TOKEN` can't reach it. A fine-grained PAT has to list the repo explicitly.
   - If it failed because a `main` is red, that is a real state of some repo, not a problem with this change. Say which repo.

4. **Don't merge the PR yourself.** Give the user the link and the dry-run result. They merge it.
