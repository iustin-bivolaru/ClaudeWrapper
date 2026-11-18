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
./install.sh                                       # symlink bin/remi into ~/.local/bin
remi onboard                                       # read-only readiness report (runs onboard.py)
```

Ruff: line length 120, py311, rules `E F I B UP S D` with Google docstrings (`D` is off for tests, `E501` off for hook scripts).

## Architecture

**Launcher (`bin/remi`, bash).** Resolves `REMI_*` config (CLI flag > env > `~/.config/remi/config` > default) and validates it. Agent frontmatter is static, so it renders a **content-addressed copy of `plugin/`** into `~/.cache/remi/plugin-<cksum>` with the worker/reviewer/explorer `model`, `effort` and `maxTurns` filled in. Then it runs `claude --plugin-dir <copy> --settings settings/settings.json --agent remi:orchestrator`. It also translates `--plan` into `--permission-mode plan` and passes claude subcommands (`mcp`, `config`, …) straight through. Consequence: editing `plugin/` changes the hash, so the next `remi` launch picks it up automatically.

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

**Hooks (`plugin/hooks/hooks.json` → `plugin/hooks/scripts/`).** These are standalone `python3` scripts, **standard library only** (they must run in any project without dependencies). Contract (`_common.py`): read a JSON payload from stdin; exit `0` = allow, exit `2` = block with the stderr reason fed back to Claude. Malformed input **fails closed**. Scripts import siblings via `sys.path.insert(0, Path(__file__).parent)`.

| Script | Event | Role |
|---|---|---|
| `guard.py` | PreToolUse (Read/Edit/Write/Grep/Glob/Bash) | secret paths, env dumps, dangerous git, protected branches, Conventional Commits (via `conventional_commits.py`), no Claude attribution |
| `agent_policy.py` | PreToolUse Agent (`pre`), SubagentStart (`start`), UserPromptSubmit (`prompt`), PreToolUse SendMessage (`message`) | only `remi:worker/reviewer/explorer` allowed; worker slot cap `REMI_MAX_WORKERS` tracked per agent id with `fcntl` locks; reviewer launched blind (one base-branch line), then sent the user's messages verbatim in `<request>` blocks (recorded by `prompt`); a new reviewer makes that request check due until sent |
| `quality.py` | `edit` / `worker` / `review` / `commit` modes | format+lint per edit, per-worker gate on stop, pre-review gate (all workers done, whole change green), commit gate (request check sent, secret/IBAN/Luhn scan, naming, format, lint, tests) |
| `claude_md.py` | PreToolUse Write/Edit/MultiEdit/Bash | CLAUDE.md ≤ `REMI_CLAUDE_MD_MAX_LINES` (200), computed on the resulting file |
| `scope.py`, `naming.py` | helpers | legacy-safe: existing files are checked only on changed lines; no single-letter names |
| `onboard.py`, `statusline.py` | launcher / status line | not hooks |

Hook state (worker slots, edited files) lives per session under `REMI_STATE_DIR` (default `$TMPDIR/remi/<session_id>`). `quality.py` finds target-project tools in this order: `.venv/bin`, `uv run`, `PATH`, `uvx ruff`. A `[tool.remi]` table in the target's pyproject overrides them.

**Settings (`settings/settings.json`).** These hold the permission deny rules for secret files, the OS sandbox (secret `denyRead`, network allowlist, no unsandboxed escape) and empty attribution. `SECRET_PATH_PATTERNS` in `_common.py` is mirrored here, so change both together.

## Tests

`tests/test_hooks.py` runs each hook as a **subprocess** (`run_hook(script, payload, *args)`) with a JSON payload built by `tool(name, cwd, **tool_input)`. Git behaviour uses the `repo` fixture (a temp repo on `main`). Pipeline tests isolate state with `REMI_STATE_DIR` (`pipeline_env`). Add a test there for any new guard or gate rule, covering both the blocked case and the allowed case.

## Conventions for changes here

- Never add Claude attribution (`Co-Authored-By: Claude`, "Generated with Claude Code") to commits or PRs. The guard enforces this only inside remi sessions.
- Commit headers use Conventional Commits (`feat(orchestrator): …`, lowercase, no trailing period, ≤ 100 chars). PR titles are plain sentences about the outcome.
- Work on a feature branch. Commits to `main` are blocked by the guard in remi sessions.
- Every CLAUDE.md (including this one) stays ≤ 200 lines.
