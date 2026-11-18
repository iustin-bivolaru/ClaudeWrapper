---
name: git-ship
description: Branch, commit, push, pull request and merge with git and gh - bootstrap a new repository, feature branch, explicit staging, Conventional Commits, a readable PR, merge after an approving review. Use when starting a task on main, or when committing, pushing, opening or merging a PR.
---

# Ship a change

Run every `gh`, `git push/pull/fetch` and `git init` **as its own Bash call**: `cd` in a separate call first, no `&&`, `;` or pipe. Only a standalone command runs outside the sandbox with the user's credentials; chained, `gh` looks logged out. `remi` only starts when `gh` is logged in, so a failing `gh` is almost always a chained command.

## 0. New project
In a folder with no repository, set it up yourself, one command per call:
```bash
git init -b main
git commit --allow-empty -m "chore: initial commit"
gh repo create <repo-name> --private --source . --remote origin --push
git switch -c feat/<short-slug>
```
The guard allows exactly this empty commit on `main`. Repositories are always `--private`.

## 1. Branch
On `main`/`master`/`develop`, create a branch before any edit: `git switch -c <type>/<short-kebab-slug>` (`feat`, `fix`, `refactor`, `chore`, `docs`, `test`). If the tree has unrelated changes, tell the user instead of mixing them in.

## 2. Commit
Stage explicit paths, then commit in a separate call (the commit hook checks exactly what is staged):
```bash
git add src/app/fx/service.py tests/test_fx_service.py
git commit -m "feat(fx): add daily rate refresh" -m "<why>"
```
- Header: `<type>(<scope>): <imperative, lowercase, no period>`, ≤ 72 characters. Types: `feat fix refactor perf test docs build ci chore`.
- No client names, IBANs, account ids or secrets in messages. No Claude attribution.
- If the commit hook refuses, fix the cause and commit again. Never `--no-verify`.

## 3. Push
If the branch already has a PR, check it first: `gh pr view --json state`. If it's `MERGED`, branch again from the updated `main` and cherry-pick your new commits. Then `git push origin HEAD`.

## 4. Pull request
```bash
gh pr create --base main --head <branch> --title "<plain sentence>" --body-file "${TMPDIR:-/tmp}/pr-body.md"
```
- **Title**: a plain sentence about the outcome, not a commit header: `Add SEPA instant payments to transfers`.
- **Body**, only the sections that apply:
  - `## What`: the outcome and the key changes.
  - `## Why`: the need, and the ticket.
  - `## How it was verified`: commands that actually ran, with results, plus the review verdict and the request check.
  - `## Risk`: migrations, new config keys, auth, money or PII paths, breaking changes, rollback.
- Only facts you saw in this session. No client data, secrets or Claude attribution.
- When later commits change what the PR does, update the body (`gh pr edit --body-file`).

## 5. Merge
Merge only when the **latest** review said `VERDICT: APPROVE` and `REQUEST: MATCHES`, pinned to the reviewed commit:
```bash
git rev-parse HEAD
gh pr merge --merge --delete-branch --match-head-commit <full sha>
```
A hook refuses the merge otherwise, if anything changed after the review, and always with `--admin`. If GitHub refuses (checks, protection, conflict), leave the PR open and say why.
