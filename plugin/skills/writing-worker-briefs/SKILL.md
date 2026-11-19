---
name: writing-worker-briefs
description: How the orchestrator writes the prompt for a remi:worker (and for SendMessage follow-ups and review fixes) - the essentials at the beginning AND repeated at the end, with supporting detail in the middle. Use every time you delegate work to a worker.
---

# Writing a worker brief

A worker starts with only your brief, and models attend least to the middle of a prompt. So the essentials go first, are repeated briefly last, and the middle holds reference detail.

```markdown
## Task
<one or two sentences: the outcome, not the steps>

## Must
- Files you own: `src/app/fx/service.py`, `tests/test_fx_service.py` (touch nothing else)
- <at most 3 constraints that would make the work wrong: money as Decimal, no API change, reuse X>
- Done when: <checkable criteria: tests pass for A and B, endpoint returns 404 for C>

## Context
- Facts already found: paths, names, line ranges, the pattern to copy (`src/app/rates/service.py:40-85`)
- Skills to load: `add-feature` (API endpoint)
- Decisions already made, edge cases, and the test command: `uv run pytest -q tests/test_fx_service.py`

## Before you finish
- Only the owned files changed; <the same constraints, one line each>; done when <the same criteria>
- Report: files changed, what you did, test command and result, anything blocked. At most 15 lines.
```

- Be specific: exact paths and line ranges, not "the service". Point to files rather than pasting them.
- Don't repeat the house style, hooks or security rules (workers have them) unless the task is at special risk (money movement, auth, PII, migrations).
- Never add a requirement only at the end, and keep the brief to about 15–40 lines.

## Review fixes and follow-ups (SendMessage)

```markdown
## Fix these review findings (your files only)
1. [high] src/app/fx/service.py:88 - rounding uses float; use Decimal quantize. (verbatim from the reviewer)

## Context
<only what is new since the last brief>

## Before you finish
- Fix exactly finding 1. If you disagree, don't change the code; explain why.
- Run `uv run pytest -q tests/test_fx_service.py`. Report each finding as fixed or disputed.
```

This shape is not for `remi:reviewer`, which stays context-free (orchestrator step 6).
