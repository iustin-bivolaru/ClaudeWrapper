---
name: python-service-style
description: House style for async Python backend services (FastAPI, SQLAlchemy 2.x, Pydantic, LangGraph agents) — reST docstrings, descriptive names, Annotated Pydantic fields and settings, SecretStr and Decimal, logging, a single application error type, async I/O. Load before writing or changing any Python in such a service, including small fixes.
---

# Python service style

**The project wins.** Open the nearest file of the same kind and copy its shape. Use the rules below only where the project has no convention of its own. Where code goes (layers, one module per resource, composition root, transactions) is in `service-architecture`.

## Modules and types
- Each module starts with a one-line docstring. Absolute imports only. `logger = logging.getLogger(__name__)` after the imports.
- Module constants are `UPPER_SNAKE` and `Final`; a non-obvious one gets a string docstring on the next line.
- Fixed string sets are `StrEnum`. Names code matches on (tool names, event names) live in an enum, never as bare strings.
- Mappers are plain `from_<source>_to_<target>()` functions, not methods on models. Internal value objects are `@dataclass(kw_only=True)` (add `frozen=True, slots=True` when immutable).
- Dependencies come through `__init__` into `self._private` attributes. Pass the settings group a component needs, never call `get_settings()` deep in the code.

## Names
- Descriptive lowercase words: `snake_case` in Python, `kebab-case` for branches, CLI flags, URL segments and non-Python files. **No single-letter names**, not even loop variables, lambdas or `except ... as error` (only `_` for an ignored value). A hook rejects them.
- No cryptic abbreviations (`acct`, `amt`, `txn`, `cfg`, `res`); domain acronyms (`iban`, `fx`, `kyc`) are fine.
- Booleans read as questions (`is_active`), units are in the name (`timeout_seconds`, `amount_minor_units`), and builtins aren't shadowed (`account_id`, not `id`).
- When you edit a line, rename its short names. Don't mass-rename untouched code.

## Docstrings (reST)
Every public module, class, method and function gets one; tests don't. A hook flags missing ones.

```python
async def transfer(self, *, source_account_id: UUID, target_account_id: UUID, amount: Decimal) -> TransferModel:
    """
    Move funds between two accounts of the same customer.

    The debit and the credit run in one transaction.

    :param source_account_id: The account to debit.
    :param target_account_id: The account to credit. Must differ from the source.
    :param amount: A positive amount in the account currency.
    :returns: The booked transfer with its ledger reference.
    :raises AppError: INSUFFICIENT_FUNDS if the balance is too low, DATABASE_ERROR if the booking fails.
    """
```
- Opening `"""` on its own line; an imperative one-sentence summary. Extra text only for the why and non-obvious behaviour (side effects, idempotency, units, time zones).
- One `:param:` per parameter in order, describing meaning and constraints, not the type. `:raises AppError:` names the codes and when.
- `__init__` holds only `:param:` lines; the class docstring states what it is and its lifecycle.

## Pydantic models and settings
- Fields are always `name: Annotated[T, Field(description=...)] = default`, never `name: T = Field(...)`. Every field has a description (it feeds OpenAPI and LLM tool schemas). Shared constrained types (`Money`, `Iban`, `CurrencyCode`) are defined once, e.g. in `models/types.py`.
- **Money is `Decimal`**, never `float`. Ids are `UUID`.
- API models extend the project's camelCase base; request models are `extra="forbid"`, separate from response models. Mask IBAN and card fields in responses and `repr` (`Field(repr=False)`).
- Settings: one root `BaseSettings` of nested `BaseModel` groups, `env_nested_delimiter="__"`. Every secret is `SecretStr` with no default; call `.get_secret_value()` only on the line that uses it.
- New setting → add the key to `.env.example` with a placeholder and tell the user to set it. Never read or edit `.env`.

## Logging and errors
- Log with `%s` arguments, never f-strings. `logger.exception` in `except` blocks. Never log personal data, document contents, tokens or keys: log ids, counts and statuses.
- One application error, `AppError(code)`, with codes from a catalogue (code, HTTP status, user-safe message). Add a code when none fits. Chain it: `raise AppError(Code.DATABASE_ERROR) from error`.
- Each boundary translates its library's exceptions once (repositories catch `SQLAlchemyError`, clients translate httpx errors).
- Catch broad `Exception` only at loops that must keep running. LLM tools return a failure string instead of raising.

## Async
- All I/O is async; blocking or CPU-heavy work goes through `asyncio.to_thread`. Bound fan-out with a semaphore rather than an unbounded `gather`.

## Commands that need real secrets
The sandbox blocks `.env`, so the dev server, `alembic` against the dev DB, or scripts that call real services may fail. Don't work around it: use `httpx.MockTransport`, fakes or tests, or ask the user to run the command with `! <command>` and paste the output.
