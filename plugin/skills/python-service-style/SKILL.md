---
name: python-service-style
description: House style for async Python services (FastAPI, SQLAlchemy 2.x, Pydantic v2, pydantic-settings) — files and imports, where each type lives, typing, Annotated Pydantic fields and settings, names, reST docstrings, logging, one application error type, dependency injection, async I/O. Load before writing or changing Python in such a service.
---

# Python service style

**Copy the neighbour.** Before writing, open the nearest existing file of the same kind (route, service, repository, table, model, mapper, client) and match its shape. In a new service, the package layout comes from `service-architecture`.

## Files and imports
- Each module starts with a one-line docstring. Imports are absolute from the package root.
- `logger = logging.getLogger(__name__)` right after the imports in a module that logs.
- Module constants are `UPPER_SNAKE: Final`.
- One concept per module: one resource's models, one repository, one service, one mapper family. Never a catch-all `models.py`, `routes.py` or `utils.py`.

## Where each type lives
- **ORM tables** (`schemas/<table>.py`): `Mapped[...]` with `mapped_column(..., doc="...")`, UUID primary keys, explicitly named FKs, unique constraints and enums.
- **Pydantic models** (`models/<resource>.py`): request and response bodies, domain values, enums. API models share a base in `models/base.py` with camelCase aliases (`alias_generator=to_camel, populate_by_name=True`); response models add `from_attributes=True`.
- **Mappers** (`mappers/`): plain functions `from_<source>_to_<target>(...)`. Never return ORM rows from the API.
- Internal value objects: `@dataclass(frozen=True, slots=True, kw_only=True)`.

## Typing and Pydantic
- Annotate everything. Use `X | None`, built-in generics and `collections.abc`.
- Fields are `name: Annotated[Type, Field(description=...)] = default`, never `name: Type = Field(...)`. Every field has a description; it feeds OpenAPI and LLM tool schemas.
- Request models are `extra="forbid"`. Models are `frozen=True` unless they must change.
- **Money is `Decimal`**, never `float`. Ids are `UUID`. Fixed string sets are `StrEnum`. Names that code matches on live in an enum, not in bare strings.
- Reusable constrained types (`NonEmptyStr`, `Money`, `CurrencyCode`, `Iban`) live once in `models/types.py`.
- Mask sensitive fields in responses and `repr` (`Field(repr=False)`).

## Settings
- One root `Settings(BaseSettings)` made of `XxxSettings(BaseModel)` groups, `env_nested_delimiter="__"` (`DATABASE__POOL_SIZE` → `settings.database.pool_size`), loaded once by a cached `get_settings()`.
- Every secret is `SecretStr` with no default. Call `.get_secret_value()` only on the line that uses it.
- The composition root passes each component the settings **group** it needs. Never call `get_settings()` deep in the code.
- A new key goes into `.env.example` with a placeholder. Tell the user which keys to set in their `.env`; you can't read or edit it.

## Classes and dependencies
- Dependencies come in through `__init__` and are stored as `self._private`. No module-level singletons.
- The composition root (the FastAPI lifespan) builds long-lived objects once and closes them (`aclose()`) at shutdown.
- A replaceable external boundary is an `ABC` plus one concrete implementation.

## Names
- Descriptive words: `pending_transfers`, `timeout_seconds`, `is_active`. Never single letters (only `_` for an ignored value) or cryptic abbreviations (`acct`, `txn`, `cfg`).
- Functions are verbs, classes are nouns, collections are plural. Don't shadow builtins (`id`, `type`).

## Docstrings (reST)
Every public module, class, method and function. Say what it does for the caller and the edge cases; don't restate the types.
```python
async def get_version(self, user_id: UUID, version: int) -> VersionModel:
    """
    Return one version of a user's document.

    :param user_id: The user's id.
    :param version: The version number.
    :returns: That version.
    :raises AppError: NOT_FOUND if that version does not exist, DATABASE_ERROR if the query fails.
    """
```
The `__init__` docstring holds only `:param:` lines. Tests need no docstrings.

## Logging
- `%s`-style arguments, never f-strings. `logger.exception` inside `except`.
- Never log personal data, document contents, tokens or keys. Log ids, counts and statuses.

## Errors
- One application exception, `AppError(code)`, with codes in an enum that pairs a stable code, an HTTP status and a user-safe message. An exception handler turns it into the response.
- Chain it: `raise AppError(AppErrorCode.DATABASE_ERROR) from error`.
- Each layer translates its library's exceptions once: repositories catch `SQLAlchemyError`, HTTP clients translate httpx errors.
- Catch broad `Exception` only at boundaries that must keep running (stream loops, background consumers), and log it.
- LLM tools **return** a failure string instead of raising.

## Async and the database
- All I/O is async. Blocking or CPU-heavy work goes through `asyncio.to_thread`.
- Bound concurrent fan-out with a semaphore instead of an unbounded `gather`.
- Services own the transaction (`async with session_factory.get_session() as session:`). Repositories never commit.

## Commands that need real secrets
The sandbox blocks `.env`, so the dev server or `alembic upgrade` against a real DB can fail. Use fakes, `httpx.MockTransport` or tests. Otherwise ask the user to run the command with `! <command>`.
