---
name: add-feature
description: Checklists of the easy-to-miss pieces when adding an API endpoint, a setting, an Alembic migration, an external integration, an outbox job, an LLM sub-agent or an agent tool in a FastAPI + SQLAlchemy + Pydantic (+ LangGraph) service. Use when planning or doing one of those changes; read only the section you need.
---

# Add a feature

Copy the closest existing example in the project end to end, and follow `service-architecture` (or `agent-orchestration-architecture`) for where code goes. These lists hold only what is easy to miss. A missing piece usually means the feature silently doesn't work.

## API endpoint
- New table: export it where `alembic/env.py` imports the models (autogenerate sees only imported tables), then write a migration (below). Postgres enums: `Enum(PyEnum, name=..., values_callable=lambda m: [x.value for x in m])` so the DB stores values.
- Repositories wrap `SQLAlchemyError` into the app error once and never commit.
- New user-facing failure: add a code to the error catalogue, and list every code in the route's `responses={...}`.
- Build the service in the composition root, add `get_x(request)` plus an `XDependency` alias, and register the router under the API version prefix (admin routers get the router-level role dependency).
- Check it in `/docs`.

## Setting
- Field in a nested group as `Annotated[T, Field(description=...)]` with a precise type (`PositiveInt`, `HttpUrl`, `SecretStr`). A new group goes on the root `Settings` (`default_factory` if every field has a default).
- Add every new key to `.env.example`. Never read or edit the real `.env`: tell the user which keys to set.
- Pass the group, not the whole `Settings`, into the component's constructor.

## Alembic migration
- Find how the project runs Alembic (Makefile, `uv run`, `PYTHONPATH=.`). If the DB URL comes from `.env`, the sandbox blocks it: write the file and ask the user to run the commands (`! make migrate`) and paste the output.
- `alembic heads` must show one head; if two, ask the user before merging.
- Autogenerate misses or gets wrong, so fix by hand:
  - added enum values: `op.execute("ALTER TYPE t ADD VALUE 'V'")` (renames: `RENAME VALUE`; drops are impossible, so the downgrade is a documented no-op);
  - triggers, functions, schemas, grants, extensions: write with `op.execute` and drop them in the downgrade;
  - column renames appear as drop + add (data loss): use `op.alter_column(..., new_column_name=...)`;
  - a new NOT NULL column on a table with rows needs a `server_default` or a backfill.
- Round-trip: `upgrade head`, `downgrade -1`, `upgrade head`.
- Never edit a committed migration, and never import app code into one.

## External integration
- An ABC in domain models; the docstring states that "not found" returns `None` and failures raise the app error (never an empty result).
- One pooled async client built in `__init__`, `aclose()` called at shutdown. With plain httpx: whole-request timeout, `follow_redirects=False`, `trust_env=False`, TLS verify on.
- Validate into external-shape models, then map to domain models. Translate 401/403, not found, 429 (bounded `Retry-After`) and the rest into specific catalogue codes. Log status and resource, never bodies.
- Settings group with `enabled` if optional; build it only when enabled and handle its absence downstream. Test with `httpx.MockTransport`.

## Outbox job
- New member of the outbox type enum. If that column is a Postgres enum, it needs `ALTER TYPE ... ADD VALUE`.
- Enqueue in the **same session** as the business write. Payload keys are `Final` constants; ids are stored as strings.
- Handler method calls one service method, is idempotent, and is added to the consumer's dispatch. Check any type-specific post-processing in the consumer includes or excludes the new type on purpose.
- Raise to retry; log and return for failures that will never succeed. Periodic work: a scanner that only enqueues, cron in settings.

## LLM sub-agent
Every piece must exist, or the agent is never offered or startup crashes:
- agent-name enum (Postgres enum → migration) and route tool name;
- the agent package (mirror the smallest complete agent), graph built once in `__init__`, a tool-filter middleware so framework built-ins are hidden, `astream` yielding only the event contract (≤1 payload);
- route tool whose description says when to use it **and when not to**, registered in the orchestrator;
- invoker (own thread id `f"{conversation_id}:{agent}"`, tracing callback, form data) and catalogue entry;
- built and registered in the composition root (only if its integration is enabled);
- added to every role allowed to use it in the entitlements file;
- settings group with at least a recursion limit.
Then send a chat turn as each role and check routing in the trace.

## Agent tool
- Name in the agent's tool-name enum; input model with `Field(description=...)` written for the model, limits, and `None` defaults ("leave null if unsure") for values the model may not know.
- Per-turn data and secrets from `runtime.context`, never the config (LangGraph copies config into checkpoints). Shared clients from `configurable`; missing one is a wiring bug.
- Return a short `Success.`/`Failure.` string; never secrets or raw personal data.
- Add it to `tools=[...]` **and** to the tool filter if it uses an explicit allow list; update the agent's prompt to say when to use it.
