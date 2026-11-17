---
name: git-ship
description: Branch, commit, push and PR workflow - create a feature branch before editing, stage explicit paths, conventional commit messages without any Claude attribution, push the branch (never protected branches, never force), open a pull request with gh. Use when starting a task on main or when committing, pushing or opening a PR.
---

# Branch → commit → push

Hooks enforce this: commits on `main/master/develop/release/production`, pushes to them, force pushes, `--no-verify`, `git add -A && git commit` in one command, and secret files in the index are all blocked.

## 1. Start: get onto a feature branch
```bash
git status --short && git branch --show-current
git switch -c <type>/<short-kebab-slug>   # only if on a protected branch
```
Types: `feat`, `fix`, `refactor`, `chore`, `docs`, `test`, `perf`. Include a ticket id when the user gave one: `feat/PAY-123-sepa-instant`.
If the working tree already has unrelated changes, tell the user instead of mixing them in.

## 2. Commit (after the reviewer approved)
Stage **explicit paths** — never `git add -A`/`git add .` blindly:
```bash
git status --short
git add path/one.py path/two.py tests/test_two.py
git diff --cached --stat
```
Then commit in a **separate** command (the commit hook runs the secret scan, format check, lint and tests on exactly what is staged):
```bash
git commit -m "<type>(<scope>): <imperative summary, ≤ 72 chars>" -m "<why, and anything notable>"
```
- One logical change per commit; split if the diff mixes concerns.
- No client names, IBANs, account ids or secrets in messages.
- **No Claude attribution**: no `Co-Authored-By: Claude …` trailer, no "Generated with Claude Code" line. The user is the only author (a hook blocks it).
- If the hook refuses the commit: read its output, fix the cause (delegate to a worker if non-trivial), re-stage, commit again. Never bypass it.

## 3. Push
```bash
git push origin HEAD
```
- Run it **on its own**: no `&&`, no pipe (`| tail`), no `-u`. A bare `git push` runs outside the sandbox with your credentials. Chained or piped commands, and `-u` (which writes `.git/config`), get blocked by the sandbox.
- `gh pr create` uses `--head` explicitly, so the branch doesn't need upstream tracking.
If the remote has diverged on your own branch: `git pull --rebase origin <branch>`, rerun tests, push again (`--force-with-lease` only on your own feature branch, and only after a rebase you did).

## 4. Open the pull request
```bash
gh pr create --base <default-branch> --head "$(git branch --show-current)" \
  --title "<type>(<scope>): <summary>" \
  --body "## What
<bullet list of changes>

## Why
<motivation / ticket id>

## How it was verified
<tests run, review outcome>"
```
- Find the default branch with `gh repo view --json defaultBranchRef -q .defaultBranchRef.name`.
- If a PR for the branch already exists (`gh pr view`), just push: the PR updates itself.
- The body follows the same rules as commit messages: no client data, no secrets, **no Claude attribution**.
- Open it as `--draft` only if the user asks, or if work is knowingly unfinished.

## 5. Report
The branch name, the commit hash(es) (`git log --oneline -n 3`) and the PR URL.
