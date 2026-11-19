# remi: what it is so far

## In one sentence

remi is plain Claude Code with a fixed team, house rules and safety rails added on top. You give it a task, and it plans, builds, reviews and merges the PR itself, following the same workflow in every project.

## What it is, and what it isn't

- **It isn't a separate application.** `remi` is a small bash launcher that starts `claude` with three extra things:
  - a **plugin** (`plugin/`): the agents, skills and hooks
  - **settings** (`settings/settings.json`): permissions, the sandbox, no attribution
  - a starting agent: `remi:orchestrator`
- Every `claude` flag, slash command and subcommand still works (`remi -c`, `remi -p "..."`, `remi mcp list`).
- Installing it means linking `bin/remi` into `~/.local/bin` (`./install.sh`). It needs `gh` logged in to GitHub.

## The team: three agents

| Agent | Model | Job |
|---|---|---|
| **Orchestrator** | Opus | The session you talk to. It sizes the task (XS/S/M/L), creates the branch, plans, hands work to workers and sends review fixes back to them. Then it writes the commit message and PR text, and ships with one `remi ship` command. |
| **Worker** | Sonnet | Writes the code. There is 1 by default, and up to 3 in parallel for big tasks, each owning its own files. Workers never commit. |
| **Reviewer** | Opus, read-only | Reviews the diff **cold**: it gets only "against main", with no explanation. After that it gets your original request word for word and checks that the change does what you asked, and nothing extra. |

Using as few workers as possible keeps the cost down: XS is no workers (the orchestrator edits directly), S is 1, M is 2 and L is 3.

## The flow of one task

```
you: "add X"
  → orchestrator: branch, size, plan (M/L: you approve the worker map)
  → workers build
  → gate: all workers done, ruff fix/format, lint, typecheck and tests green on the whole change
  → reviewer: blind code review, then the check against your request
  → findings go back to the workers that own the files, then one more review
  → orchestrator writes the commit message and PR text, then runs `remi ship`, which does:
      commit gate (secret scan, naming, format, lint, tests) → commit → push → PR
      → merge gate (review said APPROVE + MATCHES, code is exactly what was reviewed)
      → gh pr merge (otherwise the PR stays open for you)
```

## The rules: skills (guidance the model reads)

- **House style:**
  - `python-service-style`: docstrings, descriptive names, Pydantic models, `SecretStr`, `Decimal` for money, logging, errors
  - `ponytail`: the smallest change that fully solves the task, with no unasked-for extras
  - `git-ship`: how to branch, commit and write the PR
  - `writing-worker-briefs`: how the orchestrator briefs a worker
- **Architecture:** `service-architecture` covers the package layout, layers, transactions, errors and the outbox, plus a review checklist.
- **Checklists:** `add-feature` lists the easy-to-miss parts of adding an endpoint, setting, migration, integration and so on.

## The rails: hooks (enforcement that doesn't depend on the prompt)

Skills are advice. Hooks are Python scripts that Claude Code runs on every tool call, and they can block it. Everything goes through `dispatch.py` and uses the standard library only.

| Hook | What it enforces |
|---|---|
| `guard.py` | Blocks access to secret files and environment dumps, and dangerous git commands (force push, `reset --hard`, history rewrites). Blocks commits to `main`. Enforces Conventional Commits. Blocks Claude attribution. Blocks public repos. |
| `agent_policy.py` | Only remi's worker and reviewer can be started, with a cap on workers. Keeps the reviewer blind, and requires your request to be sent to it word for word. |
| `quality.py` | Runs the lint, format and test gates when a worker finishes, before the review and before every commit. Scans commits for secrets, IBANs and card numbers. |
| `merge_gate.py` | Allows a merge only after an approving, matching review of exactly the current code. Never allows `--admin`. |
| `claude_md.py` | Keeps every CLAUDE.md at 200 lines or fewer. |

**Legacy-safe:** in existing files, only the lines you change are checked, so old code is never flagged or reformatted when nobody touched it.

## The walls: security

These layers stack, so a gap in one is caught by the next:

1. Prompts tell every agent not to touch secrets.
2. Permission deny rules stop the Read and Edit tools on `.env`, keys and cloud credentials.
3. The `guard.py` hook blocks the same paths and environment dumps, and fails closed.
4. The OS sandbox: Bash can't read secret files, network access goes through an allowlist, and there's no way out of the sandbox. Only `git push/pull/fetch` and `gh` run outside it.
5. The commit gate scans every commit for secrets.
6. "Skip all permissions" mode is disabled.

## The three problems it targets

### 1. Consistency across projects and people

- The same agents, skills, hooks and settings apply in every project and for every person on the same remi version.
- The important rules are enforced by hooks, so the result doesn't depend on how the model feels that day.
- Each project's own details go in its CLAUDE.md and a `[tool.remi]` table in `pyproject.toml` (its lint and test commands).

### 2. New projects from scratch

- Run `remi` in an empty folder and the launcher offers to create the git repo, an empty first commit and a **private GitHub repo**. This has to happen before the session starts, because the sandbox locks a `.git` folder created during a session.
- The orchestrator treats a new project as size M or bigger. It loads `service-architecture`, fixes the package layout and the interfaces between parts, then splits the work into a **core** worker (domain logic) and a **shell** worker (setup, settings, API).

### 3. Onboarding existing projects

- `remi onboard` is a **read-only report**: git and GitHub, tooling, how far the code is from the house style, CLAUDE.md files, the project's own `.claude/`, secret files (by name only), and package indexes the sandbox would block.
- It then offers a **plan-mode session**. The orchestrator reads the code, runs the real lint, typecheck and test commands, and maps the risky modules (money, PII, auth, migrations). It drafts a **CLAUDE.md** and a ranked **`docs/improvement-plan.md`**, then ships both as one PR.
- Each improvement is later run as its own normal remi task, so every fix gets its own review.

## Configuration

Settings are resolved in this order: CLI flag, then environment, then `~/.config/remi/config`, then the default.

| Setting | Default |
|---|---|
| `REMI_ORCHESTRATOR_MODEL` / `_EFFORT` | opus / medium |
| `REMI_WORKER_MODEL` / `_EFFORT` / `_MAX_TURNS` | sonnet / medium / 50 |
| `REMI_REVIEWER_MODEL` / `_EFFORT` / `_MAX_TURNS` | opus / medium / 25 |
| `REMI_MAX_WORKERS` | 3 |
| `REMI_PERMISSION_MODE` | manual (also `acceptEdits`, `plan`, `auto`) |
| `REMI_NEW_REPO` | ask (also `github`, `local`, `off`) |

The status line shows the model, the branch, the session cost and how full the context is.

## Known limits today

- **Python-first.** The style skills and gates assume Python with ruff and pytest. npm scripts are only a fallback.
- **New projects aren't templated.** The `pyproject`, CI, `.env.example` and CLAUDE.md are generated by the model each time, so two new projects won't be identical.
- **Per-user config can differ.** Teammates can choose different models and effort, and a project can't pin which remi version it expects.
- **The onboarding audit is shallow on security.** It has no dependency vulnerability audit and no test coverage measurement.
- **Improvement-plan items are started by hand**, one task at a time.
- **It relies on GitHub.** It won't start without `gh` logged in with the `repo` scope.

## Where things live

| Path | What |
|---|---|
| `bin/remi` | The launcher (also `remi onboard` and `remi ship`) |
| `plugin/agents/` | `orchestrator.md`, `worker.md`, `reviewer.md` |
| `plugin/skills/` | The skills listed above |
| `plugin/hooks/` | `hooks.json`, `dispatch.py` and the hook modules |
| `settings/settings.json` | Permissions, sandbox, plugins |
| `tests/test_hooks.py` | Hook tests |
| `README.md` | The full user-facing spec |
