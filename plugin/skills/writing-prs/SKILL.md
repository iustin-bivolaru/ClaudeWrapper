---
name: writing-prs
description: How to write a pull request - Conventional Commits title, a body built from facts gathered with git (what, why, how it was verified, risk and rollout, config and migrations, breaking changes, commits), banking-grade rules (no client data, secrets or Claude attribution), and keeping the description current as the branch changes. Use whenever opening a PR, editing a PR title or description, or after pushing commits that change what an open PR does.
---

# Writing a pull request

A PR description is for the human reviewer who has five minutes and no context. Lead with the outcome, back every claim with something checkable, and point out exactly where the risk is.

## 1. Gather the facts first (don't write from memory)

```bash
base=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)
git log --reverse --format='- `%h` %s' "$base"..HEAD      # commit list
git diff --stat "$base"...HEAD | tail -1                    # size
git diff --name-only "$base"...HEAD                         # touched areas
gh pr view --json number,title,body 2>/dev/null             # an existing PR to update?
```

Also scan the diff for:
- **Migrations**: new files under `alembic/versions/`.
- **Config**: new keys in `.env.example` or a settings class.
- **API**: changed routes, request or response models, error codes.
- **Permissions**: roles, the permissions or entitlements file, auth dependencies.
- **Agents**: prompts, route tools, the agent catalogue.
- **Dependencies**: `pyproject.toml`, the lock file.
- **Breaking changes**: a `!` in any commit header.

Use only test commands and results that actually ran in this session, such as a worker's report, the pre-review gate, or the commit gate. Never claim a check you didn't run.

## 2. Title

- Use the Conventional Commits header (enforced by the guard): `<type>(<scope>)!: <description>`, lowercase, imperative, no trailing period, ≤ 72 characters.
- It describes the **whole PR**, because it usually becomes the squash-merge commit. Use the type of the most significant change. A PR with a `feat` and some `fix` commits is a `feat`.
- Add `!` if any commit is breaking.

## 3. Body template

Keep only the sections that apply. Delete the others rather than writing "N/A".

```markdown
## What
<1–3 sentences: the outcome for users or callers, not a list of files.>
- <key change>
- <key change>

## Why
<the problem or need, and the ticket: Refs: PAY-123>

## How it was verified
- <test command> - <result> (e.g. `uv run pytest -q` - 214 passed)
- <manual check that was actually done, and how>
- Review: <context-free reviewer verdict; findings fixed or disputed>

## Risk and rollout
- **Risk:** low | medium | high - <why; which code path or what data>
- **Migration:** <revision id, what it does, whether it's reversible, how long it locks>
- **Config:** new env keys `FOO__BAR`, `FOO__TOKEN` (secret). Set them before deploying. See `.env.example`.
- **Breaking change:** <what callers must change>
- **Rollback:** <revert the PR | downgrade the migration first | toggle off the feature flag>

## Commits
- `abc1234` feat(x): …

## Notes for reviewers
<where to start reading, a tricky part, a decision that needs a second opinion, a follow-up left out on purpose>
```

## 4. Rules

- **Facts over adjectives.** "Adds idempotency keys to `POST /transfers`", not "improves robustness". Name endpoints, tables, settings and error codes.
- **Short.** A reviewer should be able to read it in under two minutes. Use bullets and tables; leave out the internal process story.
- **Security and data (finance clients):**
  - No real client data: no names, IBANs, card or account numbers, emails, or screenshots containing them. Use obviously fake values (`RO00TEST0000000000000000`).
  - No secrets, tokens, internal hostnames or connection strings, even redacted ones.
  - Say explicitly when a change touches auth, permissions, money movement, PII handling, crypto or logging of sensitive fields, so it gets a careful review.
- **No Claude attribution.** No "Generated with Claude Code" and no co-author lines. A hook blocks them.
- **Ship it with `gh`.** Write the body to a temp file and pass it with `--body-file`, so markdown and backticks survive the shell:
  ```bash
  gh pr create --base "$base" --head "$(git branch --show-current)" --title "<header>" --body-file "${TMPDIR:-/tmp}/pr-body.md"
  ```
  Use `--draft` only when the user asks or the work is knowingly unfinished. Never use `--fill`, because the commit list is not a description.

## 5. Keep it current

When you push commits that change what the PR does, update the description in the same step:

```bash
gh pr view --json body -q .body > "${TMPDIR:-/tmp}/pr-body.md"   # edit only the sections that changed
gh pr edit --title "<header>" --body-file "${TMPDIR:-/tmp}/pr-body.md"
```

Update the commit list, the test counts and results, and the risk and config sections. Remove claims that are no longer true. Don't append a changelog of review rounds; the description describes the PR as it is now.
