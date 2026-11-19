---
name: python-service-style
description: House style for async Python backend services (FastAPI, SQLAlchemy 2.x, Pydantic, LangGraph agents) — module layout, typing, naming, reST docstrings, logging, a single application error type, dependency injection, async I/O. Load before writing or changing any Python in such a service, including small fixes. The task skill add-feature builds on this one.
---

# Python service style

**The project wins.** Before writing, open the nearest existing file of the same kind (service, repository, table, model, tool, client, mapper) and copy its shape. Use the rules below where the project has no convention of its own. When the project clearly does something different, follow the project and don't "fix" it.

## Files and imports

- Format and lint with the project's tool (usually ruff). Run its formatter rather than hand-formatting.
- Start each module with a one-line docstring that says what the module is for.
- Use absolute imports from the package root. Don't use relative imports.
- Put `logger = logging.getLogger(__name__)` right after the imports in any module that logs.
- Module constants are `UPPER_SNAKE` and annotated `Final`. A constant whose meaning isn't obvious gets a string-literal docstring on the line after it.
- Keep one concept per file: one repository, one service, one tool or one mapper family.
- Each layer is a package with one module per resource: `models/<resource>.py`, `api/routes/<resource>_routes.py`, `schemas/<table>.py`, `repositories/`, `services/`, `mappers/`. Never a single `models.py` or `routes.py` holding every resource, even in a new or small service. The full tree is in `service-architecture` (Package layout); load it before creating a new service or a new package.

## Which type goes where

Keep these folders separate. Find the project's names for them.

- **ORM tables** (often `schemas/` or `db/`): SQLAlchemy `Mapped[...]` and `mapped_column(..., doc="...")`. Use UUID primary keys (`uuid7` if available), and name FKs, unique constraints and enums explicitly.
- **Pydantic models** (`models/`, one module per resource, with shared bases in `models/base.py`): API request and response bodies, domain values and enums. API models share a base that sets camelCase aliases (`alias_generator=to_camel, populate_by_name=True`). Response models add `from_attributes=True`.
- **Mappers**: plain functions named `from_<source>_to_<target>(...)`. Don't put converters on the models themselves.
- **Helpers**: small reusable utilities with no business logic.
- Internal value objects use `@dataclass(kw_only=True)`, or `@dataclass(frozen=True, slots=True, kw_only=True)` when immutable. Document fields with `Annotated[T, "what it is"]`.

## Typing

- Annotate everything, including `-> None` on `__init__`.
- Use modern syntax: `X | None`, built-in generics, `collections.abc` types, and PEP 695 `type` aliases and generic functions where the Python version allows.
- Pydantic fields are declared as `Annotated[T, Field(...)]` (see `pydantic-annotated`).
- Names are descriptive words, never single letters (see `naming`).
- Fixed string sets are `StrEnum` with UPPER values. Names that code matches on (tool names, event names) live in an enum, never as bare strings.
- Use `*` to make arguments keyword-only when several share a type, or for optional flags.
- Secrets are `SecretStr`. Call `.get_secret_value()` only at the line that sends them.

## Classes and dependencies

- Dependencies come in through `__init__` and are stored as `self._private` attributes. No module-level singletons, and no reading settings deep inside the code. The app's composition root (for example the FastAPI lifespan) builds the objects and passes them in.
- Pass the settings *group* a component needs, not the whole settings object.
- A replaceable external boundary is an `ABC` with `@abstractmethod`s plus one concrete implementation.
- Long-lived clients are created once at startup and closed (`aclose()`) at shutdown.

## Docstrings

Every public class, method and function gets a multi-line reST docstring (`:param:`, `:returns:`, `:raises AppError:` with the error codes and when). Plain English, no restating the type hints. Load `python-docstrings` for the full rules and an example.

## Logging

- Pass arguments `%s`-style, never as f-strings.
- Use `logger.exception` inside `except` blocks, and `logger.warning` for refused or degraded paths you expect.
- Never log personal data, document contents, tokens or keys. Log ids, counts, statuses and resource names.

## Errors

- Use one application exception type, for example `AppError(code)`, where each code comes from an enum catalogue that pairs a stable code string, an HTTP status and a user-safe message. Add a code when none fits. An exception handler turns it into the HTTP response.
- Chain it: `raise AppError(Code.DATABASE_ERROR) from error`.
- Each layer translates its library's exceptions once. Repositories catch `SQLAlchemyError`, and HTTP clients translate httpx errors. Nothing above them sees library exceptions.
- Catch broad `Exception` only at boundaries that must keep running (stream loops, background consumers), and log it there.
- LLM tools **return** a failure string instead of raising, so the model can explain what went wrong.

## Async and DB

- All I/O is async. Run blocking or CPU-heavy calls with `asyncio.to_thread`.
- Bound concurrent fan-out with a semaphore-based helper rather than an unbounded `gather`.
- Services own the transaction (`async with session_factory.get_session() as session:`). Repositories never commit.

## Commands that need real secrets

The sandbox blocks reading `.env`. So anything that loads it, such as the dev server, `alembic upgrade` against the dev DB, or scripts that call real services, can fail inside the sandbox. Don't try to work around this. Use `httpx.MockTransport`, fakes or tests where you can. Otherwise ask the user to run the command with `! <command>` and paste the output they want you to see.

## Before you finish

1. Run the project's lint and format-check commands.
2. Check for: a bare string where an enum exists, a missing docstring, an f-string in a log call, a library exception leaking past its layer, or a secret or personal data in a log.
3. Don't run reviews yourself. Under `remi`, the orchestrator runs `remi:reviewer` on the whole branch. Mention any risky part (a migration, a prompt change, auth) in your report so it gets attention.
4. If there is no test suite, exercise the change with a small script in the project's dev or scripts folder, and say what you ran.
