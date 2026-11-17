---
name: add-outbox-job
description: Add a new kind of background/deferred work processed through a transactional outbox (or a new scheduled scan that enqueues it). Use when work is slow, must retry, or must happen only after a request commits — e.g. sending an email, calling an external API, reindexing.
---

# Add an outbox job

The pattern: a service writes an outbox row **in the same DB session** as the change that caused it. A consumer claims rows in batches, dispatches each by type to a handler, and retries with exponential backoff and jitter up to a maximum number of attempts.

First find the project's outbox type enum, the outbox repository, the consumer and its dispatch, the handlers class, the scheduler, and where they are started.

## 1. Type and payload keys
- Add a member to the outbox type enum.
- Add `Final` constants, with docstrings, for the payload keys. Don't scatter string literals.
- If the type column is a **Postgres enum**, the new value needs a migration (`ALTER TYPE ... ADD VALUE`). See the `add-migration` skill.

## 2. Enqueue it
- In the service that causes the work, add the row through the outbox repository **inside the same session block** as the business write, so both commit together or neither does.
- Payloads are JSON. Store ids as strings and parse them back (`UUID(...)`) in the handler.

## 3. Handle it
- Add `async def <type>(self, *, payload: dict[str, Any]) -> None` to the handlers class. It unpacks the payload and calls **one** service method. Inject any new service through the handlers' constructor in the composition root.
- Add a branch to the consumer's dispatch (`match` / `case`).
- Look for type-specific post-processing in the consumer, such as bookkeeping that only applies to some types. Make sure the new type is included or excluded on purpose. Prefer a set of types over another chained `!=`.
- Handlers must be **idempotent**. A row can run again after a crash or a retry.
- Raise the application error for failures that should be retried. Log and return for failures that never will succeed.

## 4. A scheduled trigger (optional)
For periodic work, write a scanner that only *enqueues* outbox rows, register it with the scheduler, and put its cron expression in settings (see the `add-setting` skill). The scan itself stays cheap, and retries come from the outbox.

## 5. Verify
- Run lint.
- Run the app, trigger the work, check the logs for the row being claimed, and check its final status in the outbox table.
