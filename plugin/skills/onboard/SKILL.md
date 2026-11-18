---
name: onboard
description: Onboard an existing project to remi - find how it builds and tests, map its code onto the service architecture, check for secret files, then write a short CLAUDE.md and a [tool.remi] block if needed. Use when the user runs `remi onboard` or asks to set remi up in a project.
---

# Onboard a project

Work read-only until the user approves the plan. Be brief: the goal is a CLAUDE.md that makes the next task go right, not a report.

## 1. Look
- `git status --short`, `git branch --show-current`, `git remote -v`, `git log --oneline -5`.
- Tooling: `pyproject.toml` (ruff, pytest, mypy, `[tool.remi]`), `Makefile`, `package.json`, CI files. Note the real commands for format, lint, typecheck and test.
- Layout: list the package tree (Glob `src/**/*.py`, depth 2–3). Map it onto `service-architecture`: which package plays each layer (routes, services, repositories, tables, models, mappers, settings, errors, composition root), and what's missing or mixed.
- Secrets, by name only, never open them: `git ls-files | grep -iE '(^|/)\.env|\.pem$|\.key$|credentials|secrets?\.'` (committed secrets are a finding), and whether `.env.example` exists.
- Private package indexes in `pyproject.toml` (`[tool.uv.index]`, Poetry sources): the sandbox blocks hosts not in `sandbox.network.allowedDomains`.

## 2. Plan (plan mode)
Propose, in a few lines each:
- **CLAUDE.md** (≤ 200 lines; keep the existing one's content that still holds): the commands, the layer map ("routes live in `api/routers/`, services in `core/`"), and the conventions Claude can't infer from code. Where the project differs from `service-architecture`, write down which one new code follows.
- **`[tool.remi]`** in `pyproject.toml`, only when the auto-detected commands are wrong:
  ```toml
  [tool.remi]
  lint = "uv run ruff check src tests"
  test = "uv run pytest -q -m 'not integration'"
  ```
- **Findings for the user**: committed secret files, a missing `.env.example`, blocked package indexes, no tests.

## 3. Ship
After approval: branch `chore/remi-onboarding`, write the files, and ship with `git-ship`. Code changes (moving packages, fixing style) are separate tasks; list them as suggestions instead of doing them.
