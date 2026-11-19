---
name: git-ship
description: Branch, commit, push, pull request and merge under remi - feature branch first, explicit staging, Conventional Commits without Claude attribution, sandbox-safe push and gh calls, a plain PR title and a fact-based PR body with no client data, keeping it current, and the reviewed-commit merge. Use when starting a task on main, or when committing, pushing, opening or editing a PR, or merging.
---

# Ship: branch → commit → push → PR → merge

Hooks block commits and pushes to protected branches, force pushes, `--no-verify`, `git add -A && git commit` in one command, secret files in the index, non-Conventional commit headers and Claude attribution. Don't work around a block: fix the cause.

## Sandbox rules for git and gh
- Run every `gh` command, `git push`, `git pull` and `git fetch` **on its own**: no `&&`, no pipe, no `cd x &&`. Chained, they run in the sandbox, can't reach credentials and look logged out. Check `gh auth status` alone before asking the user for `! gh auth login`.
- No `git push -u` (it writes `.git/config`); `gh pr create --head <branch>` doesn't need tracking.
- Never create a repository in the session (`git init`, clones). With no repository, tell the user to exit and rerun `remi` in that folder; the launcher creates it.

## New repository without commits or remote
Each command as its own call:
```bash
git commit --allow-empty -m "chore: initial commit"                       # only if HEAD has no commits
gh repo create <repo-name> --private --source . --remote origin --push    # only if there's no remote; never --public
git switch -c <type>/<short-kebab-slug>
```

## Branch
On `main`/`master`/`develop`, `git switch -c <type>/<short-kebab-slug>` (`feat`, `fix`, `refactor`, `chore`, `docs`, `test`, `perf`; add a ticket id if given: `feat/PAY-123-sepa-instant`). If the tree has unrelated changes, tell the user instead of mixing them in.

## Commit
- Stage explicit paths (`git add path/a.py tests/test_a.py`), check `git diff --cached --stat`, then commit in a separate command. The commit hook runs the secret scan, format, lint and tests on what is staged.
- `git commit -m "<type>(<scope>): <imperative, lowercase, no period, ≤ 72 chars>" -m "<why>"`. Always `-m` (an editor or `-F` can't be checked). Breaking change: `!` before the colon plus a `BREAKING CHANGE:` paragraph. Tickets as `Refs: PAY-123`.
- One logical change per commit. No client names, IBANs, account ids or secrets in messages.

## Push
- **If the branch already has a PR, check it first:** `gh pr view --json state`. `MERGED`: the push would never reach `main`, so switch to an updated `main`, branch again, cherry-pick your new commits, and open a new PR. `CLOSED`: ask the user.
- `git push origin HEAD`.
- Behind or conflicting with `main`: `git fetch origin main` (alone), `git rebase origin/main` (never `-i`), resolve, rerun tests, `git push --force-with-lease origin HEAD`. Files that now match `main` keep their review; a file you changed while resolving needs a new review.

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
- Create it with the body in a temp file: `gh pr create --base main --head <branch> --title "<title>" --body-file "$TMPDIR/pr-body.md"`. Never `--fill`; `--draft` only when asked.
- **Keep it current:** when new commits change what the PR does, rewrite the affected sections with `gh pr edit --body-file`. Describe the PR as it is now, not the review rounds.

## Merge
Only when the **latest** review said `VERDICT: APPROVE` and `REQUEST: MATCHES`. Pin it to the reviewed commit, each command alone:
```bash
git rev-parse HEAD
gh pr merge --merge --delete-branch --match-head-commit <full sha>
```
`--merge` keeps the Conventional commits on `main`. A fix after approval needs a new review; the merge gate says why it refuses. On a conflict, rebase (see Push) and retry; on any other refusal (checks, branch protection) leave the PR open and report why.

## Report
Branch, commit hashes, PR URL, and merged or why it was left open.
