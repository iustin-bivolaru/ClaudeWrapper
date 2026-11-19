# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

remi is a wrapper around Claude Code (`bin/remi` execs `claude`) for client work in banking/finance. It is **not** an application: it ships a Claude Code plugin (`plugin/`), hardened settings (`settings/settings.json`) and a launcher. Most changes are to agent prompts (Markdown), skills (Markdown) or hook scripts (Python). README.md is the user-facing spec. Keep it in sync when behaviour changes.

## Commands

```bash
uv run pytest -q                                   # all tests (tests/test_hooks.py)
uv run pytest -q tests/test_hooks.py::test_worker_cap   # one test
uv run pytest -q -k claude_md                      # tests matching a keyword
uv run ruff check . && uv run ruff format --check .
uv run python scripts/bench_hooks.py               # processes and ms per tool call for the current hooks.json
./install.sh                                       # symlink bin/remi into ~/.local/bin
remi onboard                                       # read-only readiness report (runs onboard.py)
```

Ruff: line length 120, py311, rules `E F I B UP S D` with Google docstrings (`D` is off for tests, `E501` off for hook scripts).

## Architecture

**Launcher (`bin/remi`, bash).** Resolves `REMI_*` config (CLI flag > env > `~/.config/remi/config` > default) and validates it. Agent frontmatter is static, so it renders a **content-addressed copy of `plugin/`** into `~/.cache/remi/plugin-<cksum>` with the worker/reviewer/explorer `model`, `effort` and `maxTurns` filled in. Then it runs `claude --plugin-dir <copy> --settings settings/settings.json --agent remi:orchestrator`. It refuses to start unless `gh` is logged in to github.com with the `repo` scope (`gh api --include user`). It also translates `--plan` into `--permission-mode plan` and passes claude subcommands (`mcp`, `config`, …) straight through. Consequence: editing `plugin/` changes the hash, so the next `remi` launch picks it up automatically.

**Agents (`plugin/agents/*.md`).**
- `orchestrator`: the main session (Opus). It sizes tasks XS/S/M/L, delegates, routes review fixes back to the owning worker via `SendMessage`, and ships.
- `worker`: Sonnet, 1–3 in parallel, never commits.
- `reviewer`: Opus, read-only, launched with only the base branch so it reviews the code cold; then sent the user's verbatim request (SendMessage) to check the change against it.
- `explorer`: Haiku, read-only search.

The workflow lives in `orchestrator.md`. The hooks enforce the parts that must not depend on the prompt.

**Skills (`plugin/skills/*/SKILL.md`).** There are three kinds:
- house style: `python-service-style`, `python-docstrings`, `pydantic-annotated`, `naming`, `git-ship`, `writing-prs`, `writing-worker-briefs`
- architecture: `service-architecture`, `agent-orchestration-architecture`
- task checklists: `add-*`

These are aimed at the *target* projects remi runs in, not at this repo.

**Hooks (`plugin/hooks/hooks.json` → `plugin/hooks/scripts/`).** One `python3` process per hook event: every entry in `hooks.json` runs `dispatch.py <event>` (`pre-tool`, `post-tool`, `subagent-start`, `subagent-stop`, `prompt`), which reads the payload once and calls the modules below in order. The first check to block wins, and modules are imported only when a check needs them. **Standard library only** (hooks must run in any project without dependencies). Contract (`_common.py`): exit `0` = allow, exit `2` = block with the stderr reason fed back to Claude. A malformed `pre-tool` payload **fails closed**. Gates find the repository from the call's `cwd` (`project_dir`), never from the launch folder.

| Module | Called on | Role |
|---|---|---|
| `guard.py` | `pre-tool`: Read/Edit/Write/Grep/Glob/Bash | secret paths, env dumps, dangerous git, protected branches (except a new repo's empty first commit and its `gh repo create --push`), `gh`/`git push/pull/fetch/init` only standalone (else sandboxed), Conventional Commits (via `conventional_commits.py`), no Claude attribution |
| `agent_policy.py` | `pre-tool` Agent/SendMessage, `subagent-start`, `prompt` | only `remi:worker/reviewer/explorer` allowed; worker slot cap `REMI_MAX_WORKERS` tracked per agent id with `fcntl` locks; reviewer launched blind (one base-branch line), then sent the user's messages verbatim in `<request>` blocks (recorded by `prompt`); a new reviewer makes that request check due until sent |
| `quality.py` | `post-tool`, `subagent-stop`, `pre-tool` Agent (review) and Bash (commit) | format+lint per edit, per-worker gate on stop, pre-review gate (all workers done, whole change green), commit gate (request check sent, secret/IBAN/Luhn scan, naming, format, lint, tests) |
| `merge_gate.py` | `pre-tool` Bash; `record_review` when a reviewer stops | `gh pr merge` only after the latest reviewer's `VERDICT: APPROVE` + `REQUEST: MATCHES`, HEAD equal to the reviewed snapshot (path → blob id), `--match-head-commit` = HEAD, never `--admin` |
| `claude_md.py` | `pre-tool` Write/Edit/MultiEdit/Bash | CLAUDE.md ≤ `REMI_CLAUDE_MD_MAX_LINES` (200), computed on the resulting file |
| `scope.py`, `naming.py` | helpers | legacy-safe: existing files are checked only on changed lines; no single-letter names |
| `onboard.py`, `statusline.py` | launcher / status line | not hooks |

Hook state (worker slots, edited files) lives per session under `REMI_STATE_DIR` (default `$TMPDIR/remi/<session_id>`). `quality.py` finds target-project tools in this order: `.venv/bin`, `uv run`, `PATH`, `uvx ruff`. A `[tool.remi]` table in the target's pyproject overrides them.

**Settings (`settings/settings.json`).** Enables the official `pyright-lsp` plugin (needs `pyright-langserver` on PATH; `install.sh` installs it). Its diagnostics reach only the main session, so workers run `pyright` on their files themselves. The settings also hold the permission deny rules for secret files, the OS sandbox (secret `denyRead`, network allowlist, no unsandboxed escape) and empty attribution. `SECRET_PATH_PATTERNS` in `_common.py` is mirrored here, so change both together.

## Tests

`tests/test_hooks.py` runs the dispatcher as a **subprocess** (`run_hook(event, payload)`) with a JSON payload built by `tool(name, cwd, **tool_input)`. Git behaviour uses the `repo` fixture (a temp repo on `main`). Pipeline tests isolate state with `REMI_STATE_DIR` (`pipeline_env`). Add a test there for any new guard or gate rule, covering both the blocked case and the allowed case.

## Conventions for changes here

- Never add Claude attribution (`Co-Authored-By: Claude`, "Generated with Claude Code") to commits or PRs. The guard enforces this only inside remi sessions.
- Commit headers use Conventional Commits (`feat(orchestrator): …`, lowercase, no trailing period, ≤ 100 chars). PR titles are plain sentences about the outcome.
- Work on a feature branch. Commits to `main` are blocked by the guard in remi sessions.
- Before pushing to a branch that already has a PR, check it's still open (`gh pr view <n> --json state`). If it was merged, branch again from the updated `main` and open a new PR; a push to a merged PR's branch never reaches `main`.
- When a change is done and checks pass, commit and push it (and open the PR) without asking. Merging a PR always needs the user's approval (`.claude/settings.json` asks for every `gh pr merge`).
- Every CLAUDE.md (including this one) stays ≤ 200 lines.
