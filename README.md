# cw — Claude Wrapper

A cost-aware, security-hardened layer over Claude Code for client work in banking and finance.
`cw` is plain `claude` underneath, so every flag, slash command and subcommand still works. On top, it adds:

| Piece | What it does |
|---|---|
| **Orchestrator** (`cw:orchestrator`, Opus) | The main session. Plans the work, creates a branch, delegates, reviews, commits and pushes. |
| **Worker** (`cw:worker`, Sonnet) | Does the implementation. Up to 4 run in parallel, each owning its own files. Never commits. |
| **Reviewer** (`cw:reviewer`, Sonnet, read-only) | Gets only a git range (`main...feat/x`) and no explanation, so it reviews the code cold. |
| **Skills** | `python-docstrings` (Google-style docs on every public function, method and class), `pydantic-annotated` (DTOs and Settings use `Annotated[..., Field()]`, `SecretStr`, `Decimal` for money), `git-ship` (branch → commit → push). |
| **Hooks** | Secret guard, git policy, subagent cost policy, per-edit format and lint, and a quality gate before every commit and at the end of each worker run. |
| **Sandbox** | Bash runs in the OS sandbox. Secret files are unreadable, network goes through an allowlist, and nothing can escape to unsandboxed mode. |

## Install

```bash
./install.sh                 # symlinks bin/cw into ~/.local/bin
brew install gitleaks        # optional: stronger secret scan at commit time
```
Requires `claude`, `python3` (3.11+), and `uv` or `ruff`/`pytest` available in the target project.

## Use

```bash
cd ~/work/some-client-repo
cw                           # interactive, orchestrator on Opus
cw -c                        # continue the last session
cw -p "fix the failing test in tests/test_fx.py"
cw --model sonnet            # cheaper orchestrator for simple tasks
CW_MAX_WORKERS=2 cw          # tighter parallelism
cw mcp list                  # subcommands pass straight through to claude
```

The status line shows the model, the branch (with a warning when you are on `main`), the session cost in $, and how full the context is.

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
- The orchestrator commits with conventional messages and pushes with `git push -u origin HEAD`.

## Quality gate

| When | What runs |
|---|---|
| After every Edit/Write of a `.py` file | `ruff format` + `ruff check --fix` plus the docstring rules; leftover issues go back to the agent |
| When a worker tries to finish (if the tree is dirty) | format check → lint → tests; the worker keeps going while the gate is red |
| Before `git commit` | secret scan → format check → lint → tests; the commit is refused on failure |

Tools are found automatically (`.venv/bin`, then `uv run`, then `PATH`, then `uvx ruff`). For a `package.json`-only project, the gate uses the npm scripts `format:check`, `lint`, `typecheck` and `test`. To override per project:

```toml
# pyproject.toml of the target project
[tool.cw]
format = "uv run ruff format --check src tests"
lint = "uv run ruff check src tests"
typecheck = "uv run mypy src"
test = "uv run pytest -q -x -m 'not integration'"
```

## Cost controls

- Opus plans and Sonnet does the typing: implementation is delegated to workers.
- `general-purpose` subagents, which would inherit Opus, are blocked by a hook and redirected to `cw:worker`.
- Searching goes to `Explore`, which is cheap.
- At most `CW_MAX_WORKERS` (4) workers run in parallel. Every worker pays its own startup context, so fewer, well-scoped workers cost less.
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
  skills/                      python-docstrings, pydantic-annotated, git-ship
  hooks/hooks.json             hook wiring
  hooks/scripts/               guard, quality gate, agent policy, status line
tests/test_hooks.py            tests for the guards and gates
```
