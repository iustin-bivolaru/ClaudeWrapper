# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

remi is a wrapper around Claude Code (`bin/remi` execs `claude`) for client work in banking/finance. It is **not** an application: it ships a Claude Code plugin (`plugin/`), hardened settings (`settings/settings.json`) and a launcher. Most changes are to agent prompts (Markdown), skills (Markdown) or hook scripts (Python). README.md is the user-facing spec. Keep it in sync when behaviour changes.

**Keep it light.** remi should help Claude implement well, not pile up rules. Before adding a rule, prompt line, setting or hook check, ask whether it changes what gets built. Prefer one place for each rule: a hook (if it must hold), otherwise a skill or prompt, never several.

## Commands

```bash
uv run pytest -q                                   # all tests (tests/test_hooks.py)
uv run ruff check . && uv run ruff format --check .
uv run python scripts/bench_hooks.py               # processes and ms per tool call for the current hooks.json
./install.sh                                       # symlink bin/remi into ~/.local/bin
```

## Architecture

**Launcher (`bin/remi`, bash).** Runs `claude --plugin-dir plugin --settings settings/settings.json --agent remi:orchestrator --effort medium`. It refuses to start unless `gh` is logged in to github.com with the `repo` scope, translates `--plan` into `--permission-mode plan`, turns `remi onboard` into a plan-mode session with the `onboard` skill, and passes claude subcommands (`mcp`, `config`, …) straight through. Models, effort and turn limits per role live in the agents' frontmatter.

**Agents (`plugin/agents/*.md`).**
- `orchestrator`: the main session (Opus). Sizes tasks XS/S/M/L, delegates, routes review fixes to the owning worker via `SendMessage`, ships and merges. The workflow lives here.
- `worker`: Sonnet, 1–3 in parallel, fixed tool list, never commits.
- `reviewer`: Opus, read-only. Launched with only the base branch (blind code review), then sent the user's request verbatim to check the change against it.
- `explorer`: Haiku, read-only search.

**Skills (`plugin/skills/*/SKILL.md`)**, aimed at the *target* projects remi runs in, not at this repo: `service-architecture` (the reference, modelled on nexttech-assistant-svc), `python-service-style`, the `add-*` checklists, `agent-orchestration-architecture`, `git-ship`, `onboard`.

**Hooks (`plugin/hooks/hooks.json` → `plugin/hooks/scripts/`).** One `python3` process per event: `dispatch.py <event>` (`pre-tool`, `post-tool`, `subagent-stop`) reads the payload once and calls the modules below in order; the first block wins. **Standard library only.** Exit `0` = allow, `2` = block with the stderr reason fed back to Claude. A malformed `pre-tool` payload fails closed. Gates find the repository from the call's `cwd` (`project_dir`), never from the launch folder.

| Module | Called on | Role |
|---|---|---|
| `guard.py` | `pre-tool` | secret paths, env dumps, dangerous git, protected branches (except a new repo's empty first commit and its `gh repo create --push`), `gh`/`git push/pull/fetch/init` only standalone, no public repos, no Claude attribution, only `remi:*` subagents |
| `claude_md.py` | `pre-tool` Write/Edit/MultiEdit/Bash | CLAUDE.md ≤ 200 lines, computed on the resulting file |
| `quality.py` | `post-tool`, `pre-tool` Bash (`git commit`) | fix+format+lint per edit; commit gate (secret/IBAN/Luhn scan, lint, format, typecheck, tests) |
| `merge_gate.py` | `pre-tool` Bash, `subagent-stop` (reviewer) | `gh pr merge` only after the latest reviewer's `VERDICT: APPROVE` + `REQUEST: MATCHES`, HEAD equal to the reviewed snapshot, `--match-head-commit` = HEAD, never `--admin` |
| `scope.py` | helper | legacy-safe: existing files are checked only on changed lines |
| `statusline.py` | status line | not a hook |

Review state lives per session under `REMI_STATE_DIR` (default `$TMPDIR/remi/<session_id>`). `quality.py` finds target-project tools in this order: `.venv/bin`, `uv run`, `PATH`, `uvx ruff`; `[tool.remi]` in the target's pyproject overrides them.

**Settings (`settings/settings.json`).** Enables the official `pyright-lsp` plugin (needs `pyright-langserver` on PATH; `install.sh` installs it). Its diagnostics reach only the main session, so workers run `pyright` on their files themselves. Read deny rules for secret files, the OS sandbox (secret `denyRead`, network allowlist, no unsandboxed escape) and empty attribution. `SECRET_PATH_PATTERNS` in `_common.py` is mirrored here, so change both together.

## Tests

`tests/test_hooks.py` runs the dispatcher as a **subprocess** (`run_hook(event, payload)`) with a payload built by `tool(name, cwd, **tool_input)`. Git behaviour uses the `repo` fixture (a temp repo on `main`). Add a test for any new guard or gate rule, covering both the blocked and the allowed case.

## Conventions for changes here

- Never add Claude attribution (`Co-Authored-By: Claude`, "Generated with Claude Code") to commits or PRs.
- Commit headers use Conventional Commits (`feat(orchestrator): …`, lowercase, no trailing period, ≤ 100 chars). PR titles are plain sentences about the outcome.
- Work on a feature branch. Commits to `main` are blocked by the guard in remi sessions.
- Before pushing to a branch that already has a PR, check it's still open (`gh pr view <n> --json state`). If it was merged, branch again from the updated `main` and open a new PR.
- When a change is done and checks pass, commit and push it (and open the PR) without asking. Merging a PR always needs the user's approval (`.claude/settings.json` asks for every `gh pr merge`).
- Every CLAUDE.md (including this one) stays ≤ 200 lines.
