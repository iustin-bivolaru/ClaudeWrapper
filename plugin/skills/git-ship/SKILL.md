---
name: git-ship
description: Branch, commit and push workflow - create a feature branch before editing, stage explicit paths, conventional commit messages, push the branch (never protected branches, never force). Use when starting a task on main or when committing/pushing work.
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
- If the hook refuses the commit: read its output, fix the cause (delegate to a worker if non-trivial), re-stage, commit again. Never bypass it.

## 3. Push
```bash
git push -u origin HEAD
```
If the remote has diverged on your own branch: `git pull --rebase origin <branch>`, rerun tests, push again (`--force-with-lease` only on your own feature branch, and only after a rebase you did).

## 4. Report
Branch name, commit hash(es) (`git log --oneline -n 3`), and the PR link if `gh` is available (`gh pr create --fill --draft` only if the user asked for a PR).
