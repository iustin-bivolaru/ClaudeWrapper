---
name: service-architecture
description: The reference architecture for an async Python backend service (FastAPI + SQLAlchemy + Pydantic + background worker) — package layout (one package per layer, one module per resource), layers and their rules, composition root, dependency injection, transaction ownership, error flow, auth, configuration, background work via transactional outbox, observability. Use when creating a new service or project, designing a subsystem, deciding where new code belongs, reviewing a design or a large change for architectural fit, or when a change crosses several layers. For a single concrete task, use the matching add-* skill.
---

# Service architecture

This skill describes the target shape. In an existing project, first map each piece below to the project's own folders and name any gaps or differences. Follow the project where it is consistent, and propose (don't silently impose) changes where it isn't.

## The layers

```
HTTP ─▶ middleware (auth, CSRF, request context)
     ─▶ routes        validate input, call ONE service method, return a response model
     ─▶ services      business rules, transactions, orchestration of repositories/integrations/outbox
     ─▶ repositories  one table/aggregate each, queries only, never commit
     ─▶ ORM tables    SQLAlchemy models

side folders: models (Pydantic API + domain), mappers (pure conversion functions),
              integrations (external systems behind ABCs), helpers (no business logic),
              configuration, exceptions, agents, worker
```

Dependencies point **downwards only**. Routes never touch repositories or sessions. Repositories never call services. ORM tables never leave the service layer: services return Pydantic models produced by mappers or `model_validate`. Integrations and repositories know nothing about HTTP.

## Package layout

Every layer is a **package** (a directory), and every resource or topic is **its own module** inside it. Don't create a flat `models.py`, `routes.py`, `schemas.py`, `services.py`, `errors.py` or `settings.py`, even in a small new service with one endpoint: start with the directories, so the second resource has an obvious place to go.

```
src/<package>/
  main.py                      entry point: runs uvicorn with settings.run.*
  api/
    app.py                     create_app(): routers under /api/v1, middleware, exception handlers
    lifespan.py                composition root
    dependencies.py            get_x(request) + XDependency = Annotated[...] aliases
    exception_handlers.py      AppError / validation / unknown → uniform error response
    middleware/<concern>.py    auth.py, csrf.py, ...
    routes/<resource>_routes.py   one APIRouter per resource (health_routes.py, document_routes.py, ...)
  configuration/
    config.py                  Settings root + nested groups, cached get_settings()
    log_config.py
  exceptions/
    app_error.py  error_codes.py  status_codes.py   __init__.py re-exports AppError, AppErrorCode
  models/                      Pydantic only
    base.py                    ApiModel / RequestModel / ResponseModel (camelCase aliases)
    pagination.py  error.py    shared shapes
    <resource>.py              request, response and domain models + enums of one resource
    <area>/<topic>.py          a sub-package once an area has several topic modules
  schemas/                     SQLAlchemy ORM only
    base.py                    declarative Base (+ timestamp mixin)
    <table>.py                 one table per module
    __init__.py                imports every table, so Alembic's env.py sees them all
  database/
    session_factory.py         async_sessionmaker wrapper: commit on success, rollback on error
  repositories/<table>_repository.py   one per table/aggregate
  services/<resource>_service.py       one per service; services/<area>/ when an area has several
  mappers/<resource>.py        from_<source>_to_<target>() functions for one resource
  integrations/<system>/       ABC + implementation(s) + helpers for one external system
  helpers/<topic>.py           no business logic
  worker/                      consumer/, handlers/, scanners/, scheduler.py (if there is background work)
alembic/                       env.py + versions/ (if there is a database)
tests/                         mirrors the package: tests/api/routes/, tests/services/, ...
```

Rules:
- A module holds one resource or topic. When a module passes ~200 lines or mixes topics, split it into a sub-package of topic modules (e.g. `models/candidate_selector/{job_opening,candidate_profile,page}.py`).
- `__init__.py` files stay empty unless a re-export is needed (the `schemas` table registry for Alembic, the `exceptions` public API). Import from the defining module otherwise: `from <package>.models.document import DocumentResponseModel`.
- Pydantic models live only in `models/`, ORM tables only in `schemas/`. Never put request or response models in the route module or under `api/`.
- Omit a package the service doesn't need yet (no database → no `schemas/`, `repositories/`, `database/`, `alembic/`). Add it, in this shape, the moment it is needed.
- In an existing project with a different but consistent layout, follow it. If it is flat, propose this layout to the user before restructuring; don't move files as a side effect of another task.

## Where does this code go?

| You are writing... | It goes in... |
|---|---|
| Request parsing, status codes, OpenAPI docs | route |
| "Is this allowed / valid / consistent?" | service |
| A `select` / `insert` | repository |
| A call to another system | integration (ABC + implementation), called by a service |
| ORM ↔ Pydantic ↔ external payload conversion | mapper |
| Anything slow, retryable, or that must happen after commit | outbox job + handler |
| Something periodic | scheduler job that only enqueues outbox rows |
| A tunable, secret, URL, flag | settings group |
| A user-facing failure | a new code in the error catalogue |
| LLM reasoning over a chat turn | an agent (see `agent-orchestration-architecture`) |

## Composition root

- One place (the FastAPI lifespan) builds **everything**: settings, session factory, clients, repositories' dependencies, services, agents and the worker. It stores what routes need on `app.state` and closes clients in `finally`.
- Routes reach services through small `get_x(request)` dependency functions and `Annotated[..., Depends(...)]` aliases. No module-level singletons, and no `get_settings()` calls deep in the code.
- Optional features (behind an `enabled` flag) are either fully built or not built at all, and downstream code checks for absence explicitly.
- To learn how anything is wired, read the composition root first.

## Transactions and data

- A session factory hands out `async with` sessions that commit on success and roll back on error. **Services own the session.** Each service operation opens its own session, and work that must be atomic shares one.
- Repositories take the session in `__init__` and never commit.
- Invariants that must never break belong in the database (unique constraints, check constraints, and triggers for append-only tables), not only in code.
- Schema changes go only through migrations, which are append-only once committed.

## Errors

- There is one application exception type that carries a code from a catalogue (code string, HTTP status, user-safe message). A global exception handler turns it into a uniform error response. Unknown exceptions become a generic 500 and are logged.
- Each boundary translates its library's exceptions exactly once (DB → `DATABASE_ERROR`, HTTP client → specific integration codes).
- Routes document every code they can return.

## Security

- Authentication and CSRF are global middleware, with an explicit allow-list of public paths. Tokens are in httpOnly cookies.
- Roles are checked by router-level dependencies (admin-only routers) or in the service (finer rules). What a role may do (features, data scopes) lives in a declarative permissions file, not scattered `if role ==` checks.
- Secrets are `SecretStr`, never logged, and never written to traces or checkpoints.

## Background work

- Use a **transactional outbox**. The service writes an outbox row in the same transaction as the change. A consumer claims rows in batches (`FOR UPDATE SKIP LOCKED`), dispatches each by type to an idempotent handler, and retries with exponential backoff and jitter up to a maximum number of attempts.
- Scheduled scans only enqueue rows. They never do the work inline.
- The worker may run in the API process (an asyncio task plus a scheduler started in the lifespan) to keep deployment simple. Design handlers so it can move to a separate process later without code changes. They take dependencies through constructors and share nothing in memory with requests.

## Configuration

pydantic-settings, one root made of nested groups, read from the environment with a `__` delimiter. Each component receives only its group. Every key is listed in the env example file.

## Streaming responses

For long-running or LLM responses, stream server-sent events from typed event models (progress, delta, payload, final, error) through a single mapper. Persist the result **before** sending the final event, so the client never sees "done" for something that wasn't saved.

## Observability

- Module loggers with `%s` arguments, ids rather than personal data, and `logger.exception` at the boundary that handles the error.
- LLM calls are traced (for example Langfuse) with a per-request trace id passed down through the context.

## Reviewing a design or a change against this

Check, and report each item that fails:
1. Does any layer reach past the one below it, or upward?
2. Is anything built outside the composition root, or read from global settings?
3. Who owns each transaction? Does anything that must be atomic span two sessions?
4. Is slow or retryable work done inside a request instead of the outbox?
5. Do library exceptions leak past their boundary? Is every user-facing failure a catalogue code?
6. Are permissions declared in the permissions file and enforced on the server, not only in a prompt or the UI?
7. Is an invariant enforced only in code when the database could enforce it?
8. Do secrets or personal data reach logs, traces or checkpoints?
9. Does new code follow the package layout: one module per resource in the layer's package, no new flat `models.py`/`routes.py`/`schemas.py`, Pydantic and ORM in their own packages?

When proposing a new subsystem, write a short design first: the modules it adds in the package layout, its layers, what it adds to the composition root, its tables and migrations, its settings, its outbox types and its error codes. Then list which add-* skills implement each part.
