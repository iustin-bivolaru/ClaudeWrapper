---
name: writing-worker-briefs
description: How the orchestrator writes the prompt for a remi:worker (and for SendMessage follow-ups and review fixes) - the most important things go at the beginning AND are repeated at the end, with supporting detail in the middle. Use every time you delegate work to a worker.
---

# Writing a worker brief

A worker starts with nothing but your brief. Models pay most attention to the **beginning** and the **end** of a prompt, and least to the middle. So put what must not be missed at the top, repeat it briefly at the bottom, and keep the middle for detail the worker can look up when needed.

## Structure (always in this order)

```markdown
## Task
<one or two sentences: the outcome, not the steps>

## Must
- Files you own: `src/app/fx/service.py`, `tests/test_fx_service.py` (touch nothing else)
- <the 1–3 constraints that would make the work wrong if broken: money as Decimal, no API change, reuse X>
- Done when: <acceptance criteria you can check: tests pass for A and B, endpoint returns 404 for C>

## Context
- Facts you already found: paths, function names, line numbers, the pattern to copy (`see src/app/rates/service.py:40-85`)
- Skills to load: `add-feature` (API endpoint)
- Decisions already made, and why (one line each)
- Edge cases to cover
- Test command: `uv run pytest -q tests/test_fx_service.py`

## Before you finish (repeat of the Must)
- Only the owned files changed: `src/app/fx/service.py`, `tests/test_fx_service.py`
- <the same 1–3 critical constraints, restated in one line each>
- Done when: <the same acceptance criteria>
- Report: files changed, what you did, test command and result, anything blocked. At most 15 lines.
```

## Rules

- **Top = what and the hard limits.** The task in one or two sentences, then **Must**: file ownership, the few constraints that make the work wrong if broken, and the acceptance criteria. Never open with background.
- **Bottom = the same essentials again,** shorter. Repeating them is deliberate: the end is the last thing the worker reads before acting and before reporting. Never introduce new requirements only at the end.
- **Middle = reference material,** such as facts, pointers, skills, decisions, edge cases and the test command. Anything critical that sits here must also be in **Must**.
- **Be specific.** Exact paths, names and line ranges, not "the service" or "fix the tests".
- **At most 3 critical constraints.** If everything is marked critical, nothing is. The house style, the hooks and the security rules are already in the worker's own instructions, so don't repeat them unless this task is at special risk (money movement, auth, PII, migrations).
- **Don't paste whole files.** Point to them with paths and line ranges; the worker can read them.
- **Keep it short,** around 15–40 lines. A long brief buries its own middle.

## Follow-ups and review fixes (SendMessage)

Use the same shape, scaled down:

```markdown
## Fix these review findings (your files only)
1. [high] src/app/fx/service.py:88 - rounding uses float; use Decimal quantize. (verbatim from the reviewer)
2. [medium] tests/test_fx_service.py - no test for an unknown currency.

## Context
<only what is new since your last brief>

## Before you finish
- Fix exactly findings 1 and 2. If you disagree with one, don't change the code; explain why in your report.
- Run `uv run pytest -q tests/test_fx_service.py`. Report each finding as fixed or disputed.
```

## The same principle for other prompts
- **remi:explorer:** the question first, then where to look, then "Answer with file:line references" at the end.
- **remi:reviewer:** stays context-free. Its launch prompt is only the base branch, and its later request check is one line plus the user's words verbatim in `<request>` blocks (see the orchestrator's step 6). This skill doesn't apply to either.
