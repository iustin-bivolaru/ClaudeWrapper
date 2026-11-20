---
name: git-ship
description: Branch and ship under remi - feature branch first, then `remi ship` for commit, push and PR, and `remi merge` only when the user asks. Covers the text you write - a Conventional Commit header without Claude attribution, a plain PR title and a fact-based PR body with no client data, kept current - plus sandbox-safe gh calls and rebasing. Use when starting a task on main, or when shipping, opening or editing a PR, or merging.
---

# Ship: branch → `remi ship`

You write the text (commit message, PR title, PR body); `remi ship` does the mechanics in one command: stage, commit gate, commit, push, open or update the PR. It never merges; `remi merge` does, only when the user asks. Hooks and `remi ship` block protected branches, force pushes, `--no-verify`, secret files, non-Conventional commit headers and Claude attribution. Don't work around a block: fix the cause.

## Sandbox rules for git and gh
- Run every `gh` command, `git push`, `git pull` and `git fetch` **on its own**: no `&&`, no pipe, no `cd x &&`. Chained, they run in the sandbox, can't reach credentials and look logged out. Check `gh auth status` alone before asking the user for `! gh auth login`.
- No `git push -u` (it writes `.git/config`).
- Never create a repository with `git init` or a clone. For a new project run `remi new <folder>` as the whole command (folder, repository and private GitHub repo, outside the sandbox), then `cd <folder>`.

## New repository without commits or remote
Each command as its own call:
```bash
git commit --allow-empty -m "chore: initial commit"                       # only if HEAD has no commits
gh repo create <repo-name> --private --source . --remote origin --push    # only if there's no remote; never --public
git switch -c <type>/<short-kebab-slug>
```

## Branch
On `main`/`master`/`develop`, `git switch -c <type>/<short-kebab-slug>` (`feat`, `fix`, `refactor`, `chore`, `docs`, `test`, `perf`; add a ticket id if given: `feat/PAY-123-sepa-instant`). If the tree has unrelated changes, tell the user instead of mixing them in.

## Ship
1. Write the PR body (see Pull request) with the Write tool to `.remi-pr.md` at the repository root. It is never committed, and `remi merge` deletes it (it stays while the PR is open).
2. Run, as the whole command (no `cd x &&` in front; no `$`, backticks, `;`, `&`, `|`, `<`, `>` anywhere, because it runs outside the sandbox):
   ```bash
   remi ship -m "<type>(<scope>): <imperative, lowercase, no period, ≤ 72 chars>" -m "<why>" --title "<plain PR title>"
   ```
3. Read its report: the commit, the PR URL, and whether the review allows a merge (`left open: the review approved…`) or why not. If it refuses, fix the cause it names and run it again.

What it does, so you don't repeat it: stages what you staged (else everything not git-ignored), refuses secret files, runs the commit gate (request check sent, secret scan, format, lint, tests), commits, refuses a branch whose PR was merged or closed, `git push origin HEAD`, `gh pr create` (or `gh pr edit` with the new title and body). No merge.

**Commit message:** one logical change; `!` before the colon plus a `BREAKING CHANGE:` paragraph for a breaking change; tickets as `Refs: PAY-123`. No client names, IBANs, account ids or secrets.

**Merged PR:** if `remi ship` says the branch's PR is already merged, switch to an updated `main`, branch again, cherry-pick your new commits, and ship again. `CLOSED`: ask the user.

**Behind or conflicting with `main`:** `git fetch origin main` (alone), `git rebase origin/main` (never `-i`), resolve, rerun tests, `git push --force-with-lease origin HEAD` (alone), then `remi ship` again. Files that now match `main` keep their review; a file you changed while resolving needs a new review.

## Pull request
**Gather facts first, don't write from memory:** `git log --reverse --format='- \`%h\` %s' origin/main..HEAD`, `git diff --name-only origin/main...HEAD`, and look for migrations, new `.env.example` keys, changed routes or error codes, permissions, prompts and dependencies. Quote only checks that actually ran (worker reports, gates, the review).

**Title:** a plain imperative sentence about the outcome, ≤ 60 chars, never a `type(scope):` header or a file list: `Add SEPA instant payments to transfers`, `Fix rounding in long account statements`. Add `(breaking)` or a ticket id at the end when relevant.

**Body:** keep only the sections that apply, short enough to read in two minutes.
```markdown
## What
<the outcome for users or callers, 1–3 sentences, plus key changes>
## Why
<the problem; Refs: PAY-123>
## How it was verified
- `uv run pytest -q`: 214 passed
- Review: <verdict; findings fixed or disputed>. Request check: <MATCHES, or gaps>
## Risk and rollout
- Risk: low | medium | high, and why. Migration: <revision, reversible?>. Config: new keys `FOO__BAR` (set before deploying). Rollback: <how>
## Commits
- `abc1234` feat(x): …
```
- Facts over adjectives: name endpoints, tables, settings and error codes.
- No real client data (use fakes like `RO00TEST0000000000000000`), no secrets, tokens or internal hostnames. Call out changes to auth, permissions, money movement, PII or sensitive logging.
- No Claude attribution.
- **Keep it current:** when new commits change what the PR does, rewrite `.remi-pr.md` and run `remi ship` again; it updates the PR's title and body. Describe the PR as it is now, not the review rounds.

## Merge
Only when the user's message asks for it ("merge", "merge it"): run `remi merge` as the whole command. It merges the current branch's open PR with `--match-head-commit --merge --delete-branch`, only when the user's latest message asks for a merge and the **latest** review said `VERDICT: APPROVE` and `REQUEST: MATCHES` on exactly HEAD. Never merge on your own and never run `gh pr merge` yourself. A fix after the approval needs a new review. If `remi merge` refuses because of the verdict, tell the user why; they can say "merge anyway" to overrule the verdict (never the reviewed-code check).

## Report
Branch, commit hashes, PR URL, and whether the PR can be merged once the user says "merge", or why not.
