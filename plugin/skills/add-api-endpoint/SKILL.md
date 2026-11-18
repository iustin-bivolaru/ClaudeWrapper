---
name: add-api-endpoint
description: Add or change a REST endpoint in a FastAPI service layered route → service → repository, with a central error-code catalogue and app.state-based dependency injection. Use when the user asks for a new route, endpoint, API, or CRUD for a resource.
---

# Add an API endpoint

Load `python-service-style` too. First find the project's equivalents of: the ORM folder, the Pydantic models folder and API base models, mappers, repositories, services, the error catalogue, the dependency module, the composition root (lifespan) and the router registration. Copy the closest existing resource end to end. If the project has no such packages yet, create them as in `service-architecture` (Package layout): each file below is a new module for this resource, not an addition to a shared `models.py` or `routes.py`.

Work bottom-up. Each layer has one job.

## 1. Table (only if new data): `schemas/<table>.py`
- SQLAlchemy 2.x `Mapped[...]` / `mapped_column` on the shared `Base`, plus the project's timestamp or audit mixin if there is one.
- Postgres enums: `Enum(PyEnum, name="snake_name", values_callable=lambda m: [x.value for x in m])`, so the DB stores values, not member names.
- Export the class wherever Alembic's `env.py` imports the models from. Autogenerate only sees what is imported.
- Then use the `add-migration` skill.

## 2. Pydantic models: `models/<resource>.py`, mapper in `mappers/<resource>.py`
- Request and response models go in the models folder, never next to the ORM tables. Extend the project's API base classes so field names stay consistent (usually camelCase).
- Paginated lists return the project's generic page response and take pagination as `Annotated[..., Query()]`.
- Add a mapper function `from_<row>_to_<response_model>`.

## 3. Repository: `repositories/<table>_repository.py`
- `class FooRepository` takes an `AsyncSession` in `__init__`.
- Wrap each query: `try / except SQLAlchemyError` → `logger.exception(...)` → `raise AppError(DATABASE_ERROR) from error`.
- **Never commit.** The session factory's context manager owns the transaction.

## 4. Service: `services/<resource>_service.py`
- Takes the session factory, plus collaborators, in `__init__`.
- Each operation opens its own session and builds repositories from it. Work that must be atomic (a row plus its outbox entry) goes in one session.
- Business rule failures raise `AppError` with a specific code. Add a new code to the catalogue (code, HTTP status, user-safe message) when none fits.
- Slow or retryable side effects go in the outbox (see the `add-outbox-job` skill), not in the request.

## 5. Wiring: `api/lifespan.py`, `api/dependencies.py`
- Build the service once in the composition root and store it on `app.state`, unless it's cheap enough to build per request.
- In the dependencies module, add `get_foo_service(request) -> FooService` and `FooServiceDependency = Annotated[FooService, Depends(get_foo_service)]`. Get the caller's identity and role from the existing auth-context dependency.

## 6. Route: `api/routes/<resource>_routes.py`, registered in `api/app.py`
- `router = APIRouter(prefix="/foos", tags=["foos"])`.
- Each decorator declares `response_model`, `status_code` and a `responses={...}` map that documents every error code the endpoint can return. Use the project's helper for error response docs if it has one.
- The handler only validates input, calls one service method and returns. No SQL, no business logic.
- Register the router under the API version prefix. Protect admin-only routers with a router-level role dependency. Finer role checks go in the service and return FORBIDDEN.

## 7. Verify
- Run lint and format-check.
- Start the dev server and check the endpoint in the OpenAPI docs (`/docs`).
- If the project has an architecture review subagent, ask it to review the diff.
