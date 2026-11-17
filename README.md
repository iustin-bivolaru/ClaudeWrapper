# cw — Claude Wrapper

A cost-aware, security-hardened layer over Claude Code for client work in banking and finance.
`cw` is plain `claude` underneath, so every flag, slash command and subcommand still works. On top, it adds:

| Piece | What it does |
|---|---|
| **Orchestrator** (`cw:orchestrator`, Opus) | The main session. Sizes each task (XS/S/M/L) and uses as few workers as the work allows: none for XS (it edits directly), 1 for S, 2 for M, 3 only for L. Related subtasks go to the same worker. It creates the branch, delegates, routes review fixes back to the owning workers, then commits, pushes and opens the PR. |
| **Worker** (`cw:worker`, Sonnet) | Does the implementation. Usually one; up to 3 run in parallel for large, independent work, each owning its own files, and each later fixes the review findings for its files. Never commits. |
| **Reviewer** (`cw:reviewer`, Opus, read-only) | Gets only a git range (`main...feat/x`) and no explanation, so it reviews the code cold. It applies the architecture skills' checklists when the diff touches those layers. |
| **House-style skills** | `python-service-style` (layering, typing, logging, errors, async I/O; the only one preloaded in workers, the rest load on demand), `python-docstrings` (reST `:param:`/`:returns:`/`:raises:` docstrings on every public module, class, method and function), `pydantic-annotated` (DTOs and Settings use `Annotated[..., Field()]`, `SecretStr`, `Decimal` for money), `naming` (descriptive multi-word names: `snake_case` in Python, `kebab-case` for branches, URLs and CLI flags, never single letters), `git-ship` (branch → commit → push → PR). |
| **Architecture skills** (loaded on demand by the orchestrator and reviewer) | `service-architecture` (layers, composition root, transactions, errors, outbox, plus a review checklist) and `agent-orchestration-architecture` (routing, hand-offs, entitlements, stream contract, prompt-injection boundaries, plus a review checklist). |
| **Task skills** (loaded on demand) | `add-api-endpoint`, `add-setting`, `add-migration`, `add-integration`, `add-outbox-job`, `add-sub-agent`, `add-agent-tool`. Each is a checklist of every piece a change of that kind must touch. |
| **Hooks** | Secret guard, git policy, subagent cost policy, per-edit format and lint, and a quality gate before every commit and at the end of each worker run. |
| **Sandbox** | Bash runs in the OS sandbox. Secret files are unreadable, network goes through an allowlist, and nothing can escape to unsandboxed mode. |

## Install

```bash
./install.sh                 # symlinks bin/cw into ~/.local/bin
brew install gitleaks        # optional: stronger secret scan at commit time
```
Requires `claude`, `python3` (3.11+), and `uv` or `ruff`/`pytest` available in the target project.

## Use

Interactive sessions start with the cw hamster (set `CW_NO_BANNER=1` to hide it):

```
 ▗█▖ ▗█▖   cw
 ▟▀███▀▙   orchestrator opus/medium · workers sonnet/medium 1–3 by size · reviewer opus/medium
 ▜█████▛   sandbox on · secrets guarded · branch feat/x
  ▘▘ ▝▝
```

```bash
cd ~/work/some-client-repo
cw                           # interactive, orchestrator on Opus
cw -c                        # continue the last session
cw -p "fix the failing test in tests/test_fx.py"
cw --effort high            # think harder on a complex task (default: medium)
cw --model sonnet            # cheaper orchestrator for simple tasks
CW_MAX_WORKERS=2 cw          # tighter parallelism
cw mcp list                  # subcommands pass straight through to claude
```

The status line shows the model, the branch (with a warning when you are on `main`), the session cost in $, and how full the context is.

## Configuration: model and effort per role

| Role | Model | Effort | Override |
|---|---|---|---|
| Orchestrator | `CW_ORCHESTRATOR_MODEL` = `opus` | `CW_ORCHESTRATOR_EFFORT` = `medium` | also `--model` / `--effort` |
| Worker | `CW_WORKER_MODEL` = `sonnet` | `CW_WORKER_EFFORT` = `medium` | |
| Reviewer | `CW_REVIEWER_MODEL` = `opus` | `CW_REVIEWER_EFFORT` = `medium` | |

Plus `CW_MAX_WORKERS` (`3`) and `CW_NO_BANNER`. Effort is one of `low`, `medium`, `high`, `xhigh` or `max`. Precedence is **CLI flag > environment > config file > default**. To make a setting persistent, put it in `~/.config/cw/config`:

```ini
# ~/.config/cw/config  (only CW_* keys are read; the file is parsed, never executed)
CW_REVIEWER_EFFORT=high
CW_WORKER_MODEL=sonnet
```

Agent frontmatter is static, so `cw` writes a copy of the plugin with the worker and reviewer settings filled in, under `~/.cache/cw/plugin-<hash>`. Each different configuration gets its own copy, so two sessions with different settings never overwrite each other.

## The pipeline

```
you: task ─▶ orchestrator: size it (XS/S/M/L), plan, worker map
             │
             ├─ XS ─▶ orchestrator edits directly ─────────────────────────────┐
             │                                                                  │
             └─ S/M/L ─▶ 1–3 workers (related subtasks share a worker)          │
                          each edit:    ruff check --fix · ruff format · lint   │
                          worker done:  same on ITS files (+ tests if last)     │
                                        red ⇒ the worker keeps going            │
                          ▼                                                     │
              pre-review gate (hook): all workers finished?                     │
                          ruff check --fix · ruff format · lint · tests on the  │
                          whole change; red ⇒ review refused, fix via workers   │
                          ▼                                                     │
              cw:reviewer (no context, only "against main")                     │
                          ▼                                                     │
              findings ─▶ orchestrator ─▶ owning worker(s) fix (SendMessage)    │
                          critical/high ⇒ one more review                       │
                          ▼                                                     ▼
              commit gate (hook): secret scan · naming · format · lint · tests
                          ▼
              push branch ─▶ gh pr create
```

## Security layers (defence in depth)

1. **Prompts.** Every agent is told never to touch secrets or real client data.
2. **Permission deny rules** (`settings/settings.json`). The Read and Edit tools are denied on `.env`, `.env.local`, `.env.prod*`, …, `*.pem`/`*.key`/`*.p12`/`*.jks`, `~/.ssh`, `~/.aws`, kube, docker and npm/pypi credentials, and similar files.
3. **PreToolUse guard** (`plugin/hooks/scripts/guard.py`). Covers Read, Edit, Write, Grep, Glob and Bash. It blocks secret paths, environment dumps (`printenv`, `env`, `/proc/*/environ`, `os.environ` in inline Python, keychain and vault reads), `sudo`, `curl | sh`, `git reset --hard`, `git clean -f`, history rewrites, and force pushes. It fails closed. `.env.example` and other template files stay readable.
4. **OS sandbox.** It denies reads of the same secret files to every Bash process, so `grep -r` and scripts can't get at them either. Network access is limited to an allowlist (PyPI, GitHub, npm, astral). `allowUnsandboxedCommands: false` and `failIfUnavailable: true` are set. Only `git push/pull/fetch` and `gh` run outside the sandbox, because they need your credentials, and the guard still checks them.
5. **Commit gate.** Before any `git commit`, it scans the staged diff for private keys, AWS, GitHub, Slack and API tokens, JWTs, hard-coded passwords, DB URLs that contain a password, **IBANs**, and **Luhn-valid card numbers**, plus gitleaks if it is installed. A deliberate test fixture can be allowed with `# cw: allow-secret` on that line.
6. **`bypassPermissions` mode is disabled.**

## Git workflow (enforced)

- No commits on `main`/`master`/`develop`/`release`/`production`. The agent creates `feat/…`, `fix/…` and similar branches.
- No pushes to protected branches, no `--force` (only `--force-with-lease` on your own branch), no `--no-verify`.
- Files are staged and committed in separate commands, so the gate checks exactly what gets committed.
- **Conventional Commits, enforced.** Commit headers and PR titles must look like `<type>(<scope>)!: <description>`. Allowed types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`, `chore`, `style`, `revert`. The description is lowercase with no trailing period, and the header is ≤ 100 characters (72 recommended). Messages must be passed with `-m`, so editor and `-F` commits are refused.
- The orchestrator commits, pushes with a standalone `git push origin HEAD`, and opens the PR with `gh pr create`.
- **No Claude attribution.** The `attribution` settings are empty, and the guard blocks `Co-Authored-By: Claude` and "Generated with Claude Code" in commits and PRs. You are the only author.

## Quality gate

| When | What runs |
|---|---|
| After every Edit/Write of a `.py` file | `ruff check --fix` + `ruff format`, then lint with the docstring, pep8-naming, no-f-strings-in-logs (`G004`) and no-relative-imports (`TID252`) rules, plus the single-letter-name check; leftover issues go back to the agent |
| When a worker tries to finish | `ruff check --fix` + `ruff format` + lint + naming on **the files that worker edited**, so it is never blocked by another worker's half-finished files. Tests run only if it is the last worker still running. The worker keeps going while red. |
| Before `cw:reviewer` starts | Every worker must be finished. Then `ruff check --fix` + `ruff format` on the whole change, followed by naming → format check → lint → typecheck → tests. If anything is red, the review is refused and the failures go to the workers. |
| Before `git commit` | secret scan → naming (staged files) → format check → lint → tests; the commit is refused on failure |

Tools are found automatically (`.venv/bin`, then `uv run`, then `PATH`, then `uvx ruff`). For a `package.json`-only project, the gate uses the npm scripts `format:check`, `lint`, `typecheck` and `test`. To override per project:

```toml
# pyproject.toml of the target project
[tool.cw]
fix = "uv run ruff check --fix . && uv run ruff format ."   # applied before the pre-review gate
format = "uv run ruff format --check src tests"
lint = "uv run ruff check src tests"
typecheck = "uv run mypy src"
test = "uv run pytest -q -x -m 'not integration'"
```

## Cost controls

- Opus plans and Sonnet does the typing: implementation is delegated to workers.
- Only `cw:worker`, `cw:reviewer` and `Explore` may run. A hook refuses everything else: `general-purpose`, which would inherit Opus, and any subagents a project defines in its `.claude/agents/`.
- Searching goes to `Explore`, which is cheap.
- **Task sizing.** XS changes are made directly by the orchestrator, with no worker and no Opus review unless the change is security-sensitive. S uses 1 worker, M 2, L 3. `CW_MAX_WORKERS` (3) is a hard ceiling, not a target. Related subtasks share one worker.
- **Lean workers.** Only `python-service-style` is preloaded. Other skills load when the brief asks for them. Briefs pass along facts the orchestrator already found, so workers don't search again.
- **No duplicate checks.** Hooks lint on edit, test when a worker stops, and run everything at commit. The orchestrator doesn't rerun them in between. Slots are tracked per agent id, so a worker resumed for review fixes is never counted twice.
- Review fixes go back to the worker that already has the context (`SendMessage`), not to a fresh one. The Opus reviewer runs a second time only if it found critical or high issues.
- Hook output is trimmed to the last 30–60 lines before it reaches the model.
- Session cost is always visible in the status line.

## Customising

- Allowed network domains: add your private registries (Artifactory, Nexus…) to `sandbox.network.allowedDomains` in `settings/settings.json`.
- Protected branches: `PROTECTED_BRANCHES` in `guard.py`.
- Secret file patterns: `SECRET_PATH_PATTERNS` in `_common.py`, mirrored in `settings.json`.
- Run this repo's own tests: `uv run pytest -q`.

## Layout

```
bin/cw                         launcher
settings/settings.json         permissions, sandbox, status line
plugin/                        Claude Code plugin "cw"
  agents/                      orchestrator, worker, reviewer
  skills/                      house style: python-service-style, python-docstrings, pydantic-annotated, naming, git-ship
                               architecture: service-architecture, agent-orchestration-architecture
                               tasks: add-api-endpoint, add-setting, add-migration, add-integration, add-outbox-job, add-sub-agent, add-agent-tool
  hooks/hooks.json             hook wiring
  hooks/scripts/               guard, quality gate, naming check, agent policy, status line
tests/test_hooks.py            tests for the guards and gates
```
