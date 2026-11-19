# remi

A cost-aware, security-hardened layer over Claude Code for client work in banking and finance.
`remi` is plain `claude` underneath, so every flag, slash command and subcommand still works. On top, it adds:

| Piece | What it does |
|---|---|
| **Orchestrator** (`remi:orchestrator`, Opus) | The main session. Sizes each task (XS/S/M/L) and uses as few workers as the work allows: none for XS (it edits directly), 1 for S, 2 for M, 3 only for L. Related subtasks go to the same worker. It creates the branch, delegates, routes review fixes back to the owning workers, then commits, pushes and opens the PR. |
| **Worker** (`remi:worker`, Sonnet) | Does the implementation. Usually one; up to 3 run in parallel for large, independent work, each owning its own files, and each later fixes the review findings for its files. Never commits. |
| **Explorer** (`remi:explorer`, Haiku, read-only) | Cheap search for broad sweeps across many files. It answers with file paths and line numbers, never whole files. |
| **Reviewer** (`remi:reviewer`, Opus, read-only) | Is launched with only the base branch and no explanation, so it reviews the code cold. It applies the architecture skills' checklists when the diff touches those layers. Once that review is back, the orchestrator sends it your original request, quoted word for word, and it reports anything the change misses or adds that you didn't ask for. |
| **House-style skills** | `python-service-style` (layering, typing, logging, errors, async I/O; preloaded in workers), `ponytail` (lazy senior dev: the smallest change that fully solves the task, reuse before stdlib before a new dependency, no unasked-for options or abstractions; adapted from DietrichGebert/ponytail, MIT; also preloaded in workers; the rest load on demand), `python-docstrings` (reST `:param:`/`:returns:`/`:raises:` docstrings on every public module, class, method and function), `pydantic-annotated` (DTOs and Settings use `Annotated[..., Field()]`, `SecretStr`, `Decimal` for money), `naming` (descriptive multi-word names: `snake_case` in Python, `kebab-case` for branches, URLs and CLI flags, never single letters), `git-ship` (branch → commit → push → PR), `writing-worker-briefs` (preloaded in the orchestrator: every worker brief puts the task, owned files, critical constraints and acceptance criteria at the beginning and repeats them at the end, with detail in the middle), `writing-prs` (a plain, readable title, a body built from facts: what, why, how it was verified, risk and rollout, config and migrations, commits; no client data or attribution; kept current as the branch changes). |
| **Architecture skills** (loaded on demand by the orchestrator and reviewer) | `service-architecture` (package layout with one package per layer and one module per resource, layers, composition root, transactions, errors, outbox, plus a review checklist) and `agent-orchestration-architecture` (routing, hand-offs, entitlements, stream contract, prompt-injection boundaries, plus a review checklist). |
| **Task skill** (loaded on demand) | `add-feature`: short checklists of the easy-to-miss pieces when adding an endpoint, setting, migration, integration, outbox job, sub-agent or agent tool. |
| **pyright-lsp** (official plugin, enabled in `settings.json`) | Type errors after every edit and an `LSP` tool for definitions and references. Workers and the reviewer get the tool; workers also run `pyright` on their files before reporting, because diagnostics only reach the main session. |
| **Hooks** | Secret guard, git policy, subagent cost policy, per-edit format and lint, and a quality gate before every commit and at the end of each worker run. |
| **Sandbox** | Bash runs in the OS sandbox. Secret files are unreadable, network goes through an allowlist, and nothing can escape to unsandboxed mode. |

## Install

```bash
./install.sh                 # symlinks bin/remi into ~/.local/bin
brew install gh && gh auth login -h github.com   # required
uv tool install pyright      # type errors and code navigation (install.sh does this)
brew install gitleaks        # optional: stronger secret scan at commit time
```
Requires `claude`, `python3` (3.11+), and `uv` or `ruff`/`pytest` available in the target project.

**GitHub is required.** remi creates repos, commits, pushes and opens PRs itself, so `remi` refuses to start unless `gh` is logged in to github.com with a token that has the `repo` scope. If it isn't, the launcher tells you exactly what to run (`gh auth login -h github.com`, or `gh auth refresh -h github.com -s repo`). `remi onboard` reports the same problem as a failure. The permission allow list already covers `git commit`, `git push`, `git pull`, `git rebase`, `git merge`, `git cherry-pick`, `gh repo create` (private only), `gh pr create/edit` and `gh pr merge` (still behind the merge gate), so none of them prompt.

## Use

Interactive sessions start with the remi hamster (set `REMI_NO_BANNER=1` to hide it):

```
 ▗█▖ ▗█▖   remi
 ▟▀███▀▙   orchestrator opus/medium · workers sonnet/medium 1–3 by size · reviewer opus/medium
 ▜█████▛   sandbox on · secrets guarded · branch feat/x
  ▘▘ ▝▝    shift+tab cycles modes (plan · accept edits · default)
```

```bash
cd ~/work/some-client-repo
remi                           # interactive, orchestrator on Opus
remi -c                        # continue the last session
remi -p "fix the failing test in tests/test_fx.py"
remi --plan                    # start in plan mode (same as --permission-mode plan)
remi --effort high            # think harder on a complex task (default: medium)
remi --model sonnet            # cheaper orchestrator for simple tasks
REMI_MAX_WORKERS=2 remi          # tighter parallelism
remi mcp list                  # subcommands pass straight through to claude
remi onboard                   # readiness report for an existing project (read-only)
```

The status line shows the model, the branch (with a warning when you are on `main`), the session cost in $, and how full the context is. From 60% it adds `/clear when done`: clear once the task has shipped, not in the middle of one.

## Onboarding an existing project

```bash
cd ~/PycharmProjects/existing-service
remi onboard
```
A **read-only** report. It changes nothing in the project, and never opens secret files.
- **Git and GitHub:** branch, default branch, remote, uncommitted changes, `gh` login (a failure: `remi` won't start without it).
- **Tooling:** pyproject, ruff, pytest, mypy, Makefile targets, and any existing `[tool.remi]` overrides.
- **Code baseline:** how far the existing code is from the house style (unformatted files, project lint findings, missing docstrings, naming, f-strings in log calls, relative imports, single-letter names). It's information only: existing code is checked only where you change it (see "Legacy-safe" below).
- **CLAUDE.md:** files found, and any over the 200-line limit.
- **The project's own `.claude/`:** subagents (blocked under remi), skills (and which share a name with remi's), and hooks (a project ruff or black hook is flagged as a duplicate of remi's).
- **Secrets:** secret files **by name only**. It flags any that are committed to git or not git-ignored, and a missing `.env.example`.
- **Sandbox network:** package indexes from pyproject (`[tool.uv.index]`, Poetry sources) that the sandbox allowlist would block.

At the end it offers to start a **plan-mode session**, with the findings filled in. That session maps the architecture, confirms the commands, drafts a CLAUDE.md (≤ 200 lines) and proposes a `[tool.remi]` block if needed. After you approve, it ships them on `chore/remi-onboarding` as a PR. To run it later:
```bash
remi --plan "$(remi onboard --print-prompt)"
```

## Modes (Shift+Tab)

`remi` is the normal interactive Claude Code, so **Shift+Tab cycles the permission modes** just as in `claude`: default → accept edits → plan (→ auto, if your account has it). The "skip all permissions" mode is disabled on purpose.

| Mode | What the orchestrator does |
|---|---|
| **Plan** | Researches read-only (Grep/Read itself, and `remi:explorer` only for broad sweeps), asks only questions that change the design, and writes a plan: context and assumptions, size and worker map, per-subtask skills and test commands, risks (migrations, new `.env` keys, auth, money or PII paths), and the review and ship plan with Conventional Commit headers. It creates no branch, starts no workers and doesn't commit until you approve. `Plan` and the built-in `Explore` subagents are blocked: the orchestrator designs the plan itself, and Explore would run on Opus. |
| **Accept edits / auto** | Runs the full pipeline without asking for each edit. M and L tasks still pause once, for you to approve the worker map before any worker starts. |
| **Default** | The same pipeline, but you approve edits and some commands one by one. The orchestrator batches its actions to keep the prompts few. |

Start in a mode with `remi --plan`, `remi --permission-mode acceptEdits`, or `REMI_PERMISSION_MODE=plan` (also allowed in `~/.config/remi/config`; values: `manual`, `acceptEdits`, `plan`, `auto`).

## Configuration: model and effort per role

| Role | Model | Effort | Override |
|---|---|---|---|
| Orchestrator | `REMI_ORCHESTRATOR_MODEL` = `opus` | `REMI_ORCHESTRATOR_EFFORT` = `medium` | also `--model` / `--effort` |
| Worker | `REMI_WORKER_MODEL` = `sonnet` | `REMI_WORKER_EFFORT` = `medium` | |
| Reviewer | `REMI_REVIEWER_MODEL` = `opus` | `REMI_REVIEWER_EFFORT` = `medium` | |
| Explorer | `REMI_EXPLORER_MODEL` = `haiku` | `REMI_EXPLORER_EFFORT` = `low` | |

Turn limits per agent: `REMI_WORKER_MAX_TURNS` (`50`), `REMI_REVIEWER_MAX_TURNS` (`25`) and `REMI_EXPLORER_MAX_TURNS` (`15`). The orchestrator has none: you drive the main session. Plus `REMI_MAX_WORKERS` (`3`), `REMI_PERMISSION_MODE` (starting mode), `REMI_NEW_REPO` (`ask`; see *New projects ship too*) and `REMI_NO_BANNER`. Effort is one of `low`, `medium`, `high`, `xhigh` or `max`. Precedence is **CLI flag > environment > config file > default**. To make a setting persistent, put it in `~/.config/remi/config`:

```ini
# ~/.config/remi/config  (only REMI_* keys are read; the file is parsed, never executed)
REMI_REVIEWER_EFFORT=high
REMI_WORKER_MODEL=sonnet
```

Agent frontmatter is static, so `remi` writes a copy of the plugin with the worker and reviewer settings filled in, under `~/.cache/remi/plugin-<hash>`. Each different configuration gets its own copy, so two sessions with different settings never overwrite each other.

## The pipeline

```
remi launch: gh logged in with the `repo` scope? no ⇒ refuses to start
             ▼
             no git repo here? asks, then git init · empty first commit on main ·
             private GitHub repo (gh repo create --push) · then starts the session
             ▼
you: task ─▶ orchestrator: feature branch
             │  size it (XS/S/M/L; a new project is at least M), plan, worker map
             │  (M/L: waits for you to approve the worker map)
             │
             ├─ XS ─▶ orchestrator edits directly (checked when its turn ends) ─┐
             │                                                                  │
             └─ S/M/L ─▶ 1–3 workers (related subtasks share a worker)          │
                          each edit:    nothing runs (the file is only noted)   │
                          worker done:  ruff --fix · format · lint on ITS files │
                                        (+ tests if it's the last worker)       │
                                        red ⇒ the worker keeps going            │
                          ▼                                                     │
              pre-review gate (hook): all workers finished?                     │
                          ruff check --fix · ruff format · lint · tests on the  │
                          whole change; red ⇒ review refused, fix via workers   │
                          ▼                                                     │
              remi:reviewer (no context, only "against main"): code review      │
                          ▼  then, in parallel:                                 │
              ├─ same reviewer gets your request, verbatim (SendMessage):       │
              │    does it do what you asked? missing / extra / unclear         │
              └─ code findings ─▶ owning worker(s) fix (SendMessage)            │
                          ▼                                                     │
              request findings ─▶ orchestrator re-plans or asks you             │
                          any fixes ⇒ one more review (both steps)              │
                          ▼                                                     ▼
              commit gate (hook): request check sent · secret scan · naming ·
                                  format · lint · tests
                          ▼
              push branch ─▶ gh pr create
                          ▼
              merge gate (hook): latest review APPROVE + MATCHES · HEAD is exactly
                          what was reviewed · pinned with --match-head-commit
                          ▼
              gh pr merge --merge --delete-branch   (otherwise the PR stays open)
```

## Security layers (defence in depth)

1. **Prompts.** Every agent is told never to touch secrets or real client data.
2. **Permission deny rules** (`settings/settings.json`). The Read and Edit tools are denied on `.env`, `.env.local`, `.env.prod*`, …, `*.pem`/`*.key`/`*.p12`/`*.jks`, `~/.ssh`, `~/.aws`, kube, docker and npm/pypi credentials, and similar files.
3. **PreToolUse guard** (`plugin/hooks/scripts/guard.py`). Covers Read, Edit, Write, Grep, Glob and Bash. It blocks secret paths, environment dumps (`printenv`, `env`, `/proc/*/environ`, `os.environ` in inline Python, keychain and vault reads), `sudo`, `curl | sh`, `git reset --hard`, `git clean -f`, history rewrites, and force pushes. It fails closed. `.env.example` and other template files stay readable.
4. **OS sandbox.** It denies reads of the same secret files to every Bash process, so `grep -r` and scripts can't get at them either. Network access is limited to an allowlist (PyPI, GitHub, npm, astral). Commands may listen on localhost ports (`allowLocalBinding`, macOS only) so tests and dev servers can run; this opens no route to the internet. `allowUnsandboxedCommands: false` and `failIfUnavailable: true` are set. Only `git push/pull/fetch` and `gh` run outside the sandbox, because they need your credentials. The guard still checks them, and refuses them inside a chain or pipe (`cd x && gh …`), because a chained command runs sandboxed: `gh` then can't read its token from the keychain and looks logged out. `gh repo create` is allowed, but the guard refuses public repositories (`--public`, `--visibility public`).
5. **Commit gate.** Before any `git commit`, it scans the staged diff for private keys, AWS, GitHub, Slack and API tokens, JWTs, hard-coded passwords, DB URLs that contain a password, **IBANs**, and **Luhn-valid card numbers**, plus gitleaks if it is installed. A deliberate test fixture can be allowed with `# remi: allow-secret` on that line.
6. **`bypassPermissions` mode is disabled.**

## Git workflow (enforced)

- No commits on `main`/`master`/`develop`/`release`/`production`. The agent creates `feat/…`, `fix/…` and similar branches.
- **New projects ship too.** Start `remi` in a folder with no repository and the launcher asks before the session starts: `[Y]` runs `git init -b main`, makes one empty `chore: initial commit` on `main` and creates the private GitHub repo with `gh repo create <folder> --private --source . --push`; `[l]` makes only the local repository; `[n]` starts without one. `REMI_NEW_REPO=github|local|off` answers for you (non-interactive runs never create one unasked). It can't happen inside the session: Claude Code's sandbox only protects a repository that exists when the session starts and locks every write to a `.git` made later, so the guard refuses `git init`, `git clone` and `gh repo clone` in the session and points you back to the launcher. The session then works on a feature branch and opens the PR against `main`. The guard allows only that one empty commit on `main`, and `gh repo create --push` only while `main` holds nothing else.
- No pushes to protected branches, no `--force` (only `--force-with-lease` on your own branch), no `--no-verify`.
- Files are staged and committed in separate commands, so the gate checks exactly what gets committed.
- **Conventional Commits, enforced.** Commit headers must look like `<type>(<scope>)!: <description>`. PR titles are plain, readable sentences about the outcome (e.g. `Add SEPA instant payments to transfers`), never a copied commit message. Allowed types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`, `chore`, `style`, `revert`. The description is lowercase with no trailing period, and the header is ≤ 100 characters (72 recommended). Messages must be passed with `-m`, so editor and `-F` commits are refused.
- The orchestrator commits, pushes with a standalone `git push origin HEAD`, and opens the PR with `gh pr create`.
- **It merges its own PR, with no human in the loop, only after an approving, matching review:** the latest reviewer said `VERDICT: APPROVE` and `REQUEST: MATCHES`, HEAD holds exactly the files the reviewer saw (files that match `main` after a rebase count as seen), and the merge is pinned with `--match-head-commit`. Otherwise the PR stays open for you. `--admin` is never allowed. Low findings that come with an approval are listed as follow-ups instead of being fixed, because a fix after the approval would need a new review. Turn on branch protection with required checks on GitHub if CI should also gate the merge.
- **No Claude attribution.** The `attribution` settings are empty, and the guard blocks `Co-Authored-By: Claude` and "Generated with Claude Code" in commits and PRs. You are the only author.

## Quality gate

**Legacy-safe.** A file that already exists in git is held to the rules **only on the lines the change adds or modifies**. Old code you don't touch is never reported, and is never reformatted unless that file was already ruff-clean. Concretely:
- **Fix and format:** `ruff check --fix` and `ruff format` run on the whole file only if its committed version was already clean. Otherwise only the changed line ranges are formatted (`ruff format --range`).
- **Lint and naming:** findings are filtered to the changed lines.
- **New files:** checked in full.
- **Typecheck and tests:** always run on the whole project.

So one fixed line in an old module stays a one-line diff, and workers don't spend turns cleaning up code nobody asked them to touch.

| When | What runs |
|---|---|
| Each Edit/Write of a `.py` file | Nothing is checked. The path is appended to the agent's edit list, so editing never waits on ruff and files don't change under the agent mid-task. |
| When a worker tries to finish | `ruff check --fix` + `ruff format` (scoped as above), then lint (changed lines) with the docstring, pep8-naming, no-f-strings-in-logs (`G004`) and no-relative-imports (`TID252`) rules, plus the single-letter-name check, on **the files that worker edited**, so it is never blocked by another worker's half-finished files. Tests run only if it is the last worker still running. The worker keeps going while red. |
| When the main session's turn ends (`Stop`) | Only if the orchestrator edited Python itself this turn (XS work): the same fix, format, lint and naming on those files, then tests unless a worker is still running. Red sends it back once. Turns without Python edits run nothing. |
| Before `remi:reviewer` starts | Every worker must be finished. Then `ruff check --fix` + `ruff format` on the whole change, followed by naming → format check → lint → typecheck → tests. If anything is red, the review is refused and the failures go to the workers. |
| When `remi:reviewer` is launched | Its prompt must be one base-branch line. Explanations, plans and your request are refused, so the code review is blind. |
| When the request goes to the reviewer (`SendMessage`) | One instruction line plus your messages quoted word for word in `<request>` blocks (your messages are recorded by a `UserPromptSubmit` hook, which skips subagent hand-backs and task notifications; your answers to AskUserQuestion count too, read from the session transcript). Explanations, plans and reworded quotes are refused. |
| Before `gh pr merge` | the latest review said `APPROVE` and `MATCHES` → every file in HEAD matches the snapshot taken when the code review finished, or matches `main` (brought in by a rebase onto `main`) → `--match-head-commit` is HEAD → no `--admin`; otherwise the merge is refused |
| Before `git commit` | request check sent to the latest reviewer (if one ran) → secret scan → naming (staged files) → format check → lint → tests; the commit is refused on failure |

Tools are found automatically (`.venv/bin`, then `uv run`, then `PATH`, then `uvx ruff`). For a `package.json`-only project, the gate uses the npm scripts `format:check`, `lint`, `typecheck` and `test`. To override per project:

```toml
# pyproject.toml of the target project
[tool.remi]
fix = "uv run ruff check --fix . && uv run ruff format ."   # applied before the pre-review gate
format = "uv run ruff format --check src tests"
lint = "uv run ruff check src tests"
typecheck = "uv run mypy src"
test = "uv run pytest -q -x -m 'not integration'"
```

## CLAUDE.md line limit

Every `CLAUDE.md` / `CLAUDE.local.md`, at any depth, stays at or under **200 lines**. It is loaded into every request, so each line costs tokens every time.
- **Write / Edit / MultiEdit:** the hook works out the resulting file and refuses the change if it would exceed the limit. It also tells Claude how to shrink it: keep only rules it can't infer, drop what linters already enforce, move procedures into skills or `docs/`, and compress prose into bullets.
- **Shell:** redirects, `tee`, `sed -i`, and `cp`/`mv` onto a CLAUDE.md are refused, because their result can't be checked in advance.
- **Commit gate:** staged CLAUDE.md files over the limit block the commit.
- Change the limit with `REMI_CLAUDE_MD_MAX_LINES`.

## Cost controls

- Opus plans and Sonnet does the typing: implementation is delegated to workers.
- Only `remi:worker`, `remi:reviewer` and `remi:explorer` may run. A hook refuses everything else: `general-purpose`, `Plan` and the built-in `Explore`, which all inherit Opus, and any subagents a project defines in its `.claude/agents/`.
- **Searching:** the orchestrator greps and reads targeted ranges itself, because starting any subagent costs about 20k tokens of context. Broad sweeps go to `remi:explorer`, which runs on Haiku. A measured search cost $0.0018, while the built-in Explore would run on Opus.
- **Turn limits:** workers 50, reviewer 25, explorer 15. A worker that hits its limit stops with a partial result, and the orchestrator continues it with `SendMessage` (which gives it a fresh budget) or re-plans. Workers are told to wind down cleanly at about two-thirds of their budget.
- **Task sizing.** XS changes are made directly by the orchestrator, with no worker and no Opus review unless the change is security-sensitive. S uses 1 worker, M 2, L 3. A new project from scratch is at least M: one worker for the core logic and one for the project shell and API, working in parallel against an interface fixed in the plan. `REMI_MAX_WORKERS` (3) is a hard ceiling, not a target. Related subtasks share one worker.
- **Lean workers.** Only `python-service-style` and `ponytail` are preloaded. Other skills load when the brief asks for them. Briefs pass along facts the orchestrator already found, so workers don't search again.
- **No duplicate checks.** Hooks lint and test when a worker or the main turn ends, never per edit. A passing run records the git tree hash of the working tree, so the pre-review and commit gates skip tests (and the whole gate) that already passed on the identical tree; any change, or a partially staged commit, reruns them. The commit gate always runs the secret scan, whatever the tree. The orchestrator doesn't rerun them in between. Slots are tracked per agent id, so a worker resumed for review fixes is never counted twice.
- Review fixes go back to the worker that already has the context (`SendMessage`), not to a fresh one. The Opus reviewer runs a second time only if it found critical or high issues (in the code review or the request check). The request check reuses the same reviewer, so it costs one more message, not a new agent.
- Hook output is trimmed to the last 30–60 lines before it reaches the model.
- Session cost is always visible in the status line.

## Customising

- Allowed network domains: add your private registries (Artifactory, Nexus…) to `sandbox.network.allowedDomains` in `settings/settings.json`.
- Protected branches: `PROTECTED_BRANCHES` in `guard.py`.
- Secret file patterns: `SECRET_PATH_PATTERNS` in `_common.py`, mirrored in `settings.json`.
- Run this repo's own tests: `uv run pytest -q`.

## Layout

```
bin/remi                         launcher
settings/settings.json         permissions, sandbox, status line
plugin/                        Claude Code plugin "remi"
  agents/                      orchestrator, worker, reviewer, explorer
  skills/                      house style: python-service-style, ponytail, python-docstrings, pydantic-annotated, naming, git-ship, writing-prs
                               architecture: service-architecture, agent-orchestration-architecture
                               task: add-feature
  hooks/hooks.json             hook wiring: every event runs dispatch.py <event>, one process per call
  hooks/scripts/               dispatcher, guard, quality gate, merge gate, naming check, agent policy, status line
scripts/bench_hooks.py         measures processes and time per tool call for the current hooks.json
tests/test_hooks.py            tests for the guards and gates
```
