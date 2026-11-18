# remi

A lean layer over Claude Code for client work in banking and finance. `remi` is plain `claude` underneath, so every flag, slash command and subcommand still works. It adds:

| Piece | What it does |
|---|---|
| **Orchestrator** (Opus, the main session) | Sizes each task (XS/S/M/L), plans, delegates to as few workers as the work allows, sends the change to the reviewer, routes fixes back, then commits, pushes, opens and merges the PR. |
| **Worker** (Sonnet, 1–3) | Implements, each owning its own files. Fixes the review findings for those files. Never commits. |
| **Reviewer** (Opus, read-only) | Reviews the diff cold (launched with only the base branch), then checks it against your request, quoted word for word. |
| **Explorer** (Haiku, read-only) | Cheap search for broad sweeps. |
| **Skills** | `service-architecture` (the reference layout and layers, from nexttech-assistant-svc), `python-service-style` (house style, preloaded in workers), `add-*` checklists (`add-api-endpoint`, `add-setting`, `add-migration`, `add-integration`, `add-outbox-job`, `add-sub-agent`, `add-agent-tool`), `agent-orchestration-architecture`, `git-ship`, `onboard`. |
| **Hooks** | Secret and git guard, format and lint on every edit, a full gate on every commit, a merge gate. |
| **Sandbox** | Bash runs in the OS sandbox: secret files unreadable, network through an allowlist. |

## Install

```bash
./install.sh                                     # symlinks bin/remi into ~/.local/bin
brew install gh && gh auth login -h github.com   # required: remi creates repos, pushes and opens PRs
brew install gitleaks                            # optional: stronger secret scan at commit
```
Requires `claude` and `python3` (3.11+); the target project needs `uv` or `ruff`/`pytest`.

## Use

```bash
remi                      # interactive; shift+tab cycles plan · accept edits · default
remi --plan               # start in plan mode
remi -c                   # continue the last session
remi -p "fix the failing test in tests/test_fx.py"
remi --model sonnet       # cheaper orchestrator for simple tasks
remi onboard              # plan-mode session that onboards the current project
remi mcp list             # subcommands pass straight through to claude
```

Models, effort and turn limits per role are set in `plugin/agents/*.md`. `REMI_NO_BANNER=1` hides the hamster.

## The pipeline

```
you: task ─▶ orchestrator: branch (new project: git init · private GitHub repo) · size · plan
             (M/L: waits for your go-ahead)
   XS ─▶ orchestrator edits directly
   S/M/L ─▶ 1–3 workers ── every edit: ruff fix · format · lint on the changed lines
             ▼
         reviewer: blind code review ─▶ then your request, verbatim: missing / extra / unclear
             ▼                           (code findings go to the owning workers in parallel)
         commit gate: secret scan (keys, tokens, IBANs, Luhn card numbers) · lint · format · typecheck · tests
             ▼
         push ─▶ PR ─▶ merge gate: latest review APPROVE + MATCHES · HEAD is exactly what was reviewed
                       · --match-head-commit · never --admin   (otherwise the PR stays open)
```

## Onboarding and new projects

- **New project:** remi runs `git init -b main`, an empty first commit, `gh repo create --private --push`, then works on a feature branch. A new service starts from the `service-architecture` package layout, split into a core worker and a shell worker.
- **Existing project:** `remi onboard` maps the code onto the architecture, finds the real format/lint/test commands, checks for committed secret files, and proposes a CLAUDE.md (≤ 200 lines) and a `[tool.remi]` block. Nothing changes until you approve.

## Quality gate

**Legacy-safe:** existing files are checked only on the lines a change adds or modifies, and are reformatted as a whole only if they were already clean. New files are checked in full. Typecheck and tests run on the whole project.

Tools are found automatically (`.venv/bin`, `uv run`, `PATH`, `uvx ruff`). Override them per project:
```toml
[tool.remi]
format = "uv run ruff format --check src tests"
lint = "uv run ruff check src tests"
typecheck = "uv run mypy src"
test = "uv run pytest -q -x -m 'not integration'"
```

## Security

1. **Guard hook**: blocks secret paths (`.env*` except templates, keys, cloud credentials), environment dumps, `sudo`, `curl | sh`, `git reset --hard`, force pushes, commits or pushes to protected branches, public repos and Claude attribution. It fails closed.
2. **Permission deny rules** for reading secret files.
3. **OS sandbox**: the same secret files are unreadable to every Bash process, network goes through an allowlist (PyPI, GitHub, npm, astral), localhost ports may be bound for tests. Only standalone `gh`, `git push/pull/fetch` and `git init` run outside it, because they need your credentials.
4. **Commit gate**: scans the staged diff for keys, tokens, passwords, DB URLs with passwords, IBANs and Luhn-valid card numbers (plus gitleaks if installed). A deliberate fixture line can carry `# remi: allow-secret`.
5. `bypassPermissions` mode is disabled.

Add private registries to `sandbox.network.allowedDomains` in `settings/settings.json`. The secret patterns live in `SECRET_PATH_PATTERNS` (`plugin/hooks/scripts/_common.py`) and are mirrored in `settings.json`.

## Layout

```
bin/remi                  launcher
settings/settings.json    permissions, sandbox, status line
plugin/agents/            orchestrator, worker, reviewer, explorer
plugin/skills/            architecture, house style, add-* checklists, git-ship, onboard
plugin/hooks/             hooks.json → scripts/dispatch.py (guard, quality, merge gate, CLAUDE.md limit)
scripts/bench_hooks.py    hook cost per tool call
tests/test_hooks.py       tests for the guard and gates
```
