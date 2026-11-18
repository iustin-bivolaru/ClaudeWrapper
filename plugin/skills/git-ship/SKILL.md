---
name: git-ship
description: Branch, commit, push and PR workflow (PR text via writing-prs) - create a feature branch before editing, stage explicit paths, conventional commit messages without any Claude attribution, push the branch (never protected branches, never force), open a pull request with gh. Use when starting a task on main or when committing, pushing or opening a PR.
---

# Branch → commit → push

Hooks enforce this: commits on `main/master/develop/release/production`, pushes to them, force pushes, `--no-verify`, `git add -A && git commit` in one command, and secret files in the index are all blocked.

## 0. New project: bootstrap the repository
When the project folder has no git repository yet, set it up yourself; don't ask the user to. Run each command **as its own Bash call**, in this order:
```bash
mkdir -p <project-folder>        # if it doesn't exist
cd <project-folder>
git init -b main
git commit --allow-empty -m "chore: initial commit"
gh repo create <repo-name> --private --source . --remote origin --push   # only if the user wants a GitHub repo or wants it shipped
git switch -c <type>/<short-kebab-slug>
```
- `git init` and `gh` run outside the sandbox only when they stand alone. Chained (`cd x && git init`), the sandbox stops `git init` from writing `.git/config`, and `gh` can't read its token, so it looks logged out.
- The guard allows exactly this one commit on `main`: the empty first commit of a repository with no commits. `gh repo create --push` may publish `main` only while it holds just that commit. That gives the feature branch a base for its PR.
- Never `--public`: the guard refuses public repositories. Use `--private` (or `--internal` for an organisation).
- If `gh repo create` fails, run `gh auth status` alone. Ask the user for `! gh auth login` only if that fails too.

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

### Conventional Commits (enforced by a hook)
The header is `<type>(<scope>)!: <description>`. The guard refuses any commit message that doesn't match. PR titles are plain sentences instead (see `writing-prs`).

| Type | Use for |
|---|---|
| `feat` | a new capability for users or callers |
| `fix` | a bug fix |
| `refactor` | a code change that neither fixes a bug nor adds a feature |
| `perf` | a performance improvement |
| `test` | adding or fixing tests only |
| `docs` | documentation only |
| `build` | the build system, dependencies, packaging |
| `ci` | CI configuration |
| `chore` | maintenance that touches no `src` or test code |
| `style` | formatting only (rare: the formatter runs on every edit) |
| `revert` | reverts an earlier commit (name it in the body) |

- **Scope** is optional, lowercase, and names the area: `feat(payments): …`, `fix(fx-rates): …`, `chore(deps): …`.
- **Breaking change**: put `!` before the colon (`feat(api)!: drop the v1 statements endpoint`) and add a `BREAKING CHANGE: <what callers must do>` paragraph to the body.
- **Description**: imperative mood, lowercase start, no trailing period, ≤ 72 characters for the whole header (hard limit 100).
- **Body** (a second `-m`): explain *why*, not what the diff already shows. Reference tickets as `Refs: PAY-123`.
- Always pass the message with `-m`. An editor or `-F file` can't be checked, so the hook refuses them.
- When squash-merging, make the squash commit title a Conventional header too. GitHub suggests the plain PR title.

Examples:
```bash
git commit -m "feat(transfers): add SEPA instant payments" -m "Clients asked for same-second settlement. Refs: PAY-412"
git commit -m "fix(statements): use Decimal for running balances" -m "Float rounding drifted by a cent on long statements."
git commit -m "chore(deps): bump httpx to 0.28"
```

- One logical change per commit; split if the diff mixes concerns.
- No client names, IBANs, account ids or secrets in messages.
- **No Claude attribution**: no `Co-Authored-By: Claude …` trailer, no "Generated with Claude Code" line. The user is the only author (a hook blocks it).
- If the hook refuses the commit: read its output, fix the cause (delegate to a worker if non-trivial), re-stage, commit again. Never bypass it.

## 3. Push
```bash
git push origin HEAD
```
- Run it **on its own**: no `&&`, no pipe (`| tail`), no `-u`. The same goes for every `gh` command: `cd` in a separate call first. A bare `git push` runs outside the sandbox with your credentials. Chained or piped commands, and `-u` (which writes `.git/config`), get blocked by the sandbox.
- `gh pr create` uses `--head` explicitly, so the branch doesn't need upstream tracking.
If the remote has diverged on your own branch: `git pull --rebase origin <branch>`, rerun tests, push again (`--force-with-lease` only on your own feature branch, and only after a rebase you did).

## 4. Open or update the pull request
Load the **`writing-prs`** skill. It covers gathering the facts, the title, the body template, the security rules, and `gh pr create --body-file`.
- If a PR already exists for the branch (`gh pr view`), pushing updates its commits. Also update its description when what the PR does has changed (`writing-prs`, step 5).

## 5. Report
The branch name, the commit hash(es) (`git log --oneline -n 3`) and the PR URL.
