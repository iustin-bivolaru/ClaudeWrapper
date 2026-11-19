---
name: pydantic-annotated
description: Conventions for DTOs and Settings - Pydantic v2 models and pydantic-settings groups declared with Annotated[type, Field(...)], reusable constrained types, camelCase API base models, SecretStr for secrets, Decimal for money. Use whenever creating or changing request/response models, DTOs, schemas or configuration/settings (together with add-setting).
---

# DTOs and Settings with `Annotated`

**The project wins** on folder names and base classes. On field syntax, though, always use `Annotated`: declare fields as `name: Annotated[Type, Field(...)]` and never as `name: Type = Field(...)`. Constraints and descriptions go in the annotation. A default, if there is one, goes after `=`. This keeps constraints reusable and visible in the type.

## Reusable types: define them once (e.g. `models/types.py`)

```python
"""Reusable constrained types shared by API models and settings."""

from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type CurrencyCode = Annotated[
    str, StringConstraints(pattern=r"^[A-Z]{3}$"), Field(description="ISO 4217 code.", examples=["EUR"])
]
type Money = Annotated[Decimal, Field(max_digits=18, decimal_places=2, description="Monetary amount.")]
type PositiveMoney = Annotated[Money, Field(gt=0)]
type Iban = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
]
```
On Python versions before 3.12, use a plain assignment (`NonEmptyStr = Annotated[...]`) instead of `type`.

## DTOs (API request and response bodies)

API models extend the project's API base. That base sets camelCase aliases (`alias_generator=to_camel, populate_by_name=True`), and response models add `from_attributes=True`.

```python
class TransferRequest(ApiRequestModel):
    """
    A request to move money between two of the caller's accounts.
    """

    source_iban: Annotated[Iban, Field(description="Account to debit.")]
    target_iban: Annotated[Iban, Field(description="Account to credit.")]
    amount: Annotated[PositiveMoney, Field(description="Amount to transfer.")]
    currency: CurrencyCode
    reference: Annotated[str | None, Field(max_length=140, description="Free text shown to the payee.")] = None
```

Rules:
- Request models are `extra="forbid"` (unknown fields are rejected). Make models `frozen=True` unless they need to be mutated.
- Every field has a `description`. It feeds OpenAPI and the LLM tool schemas. The class has a docstring.
- **Money is `Decimal`**, never `float`. Ids are `UUID`, not bare `str`. Fixed string sets are `StrEnum`.
- Keep separate inbound (`…Request`) and outbound (`…Response`) models. Convert ORM rows with `from_<row>_to_<model>` mapper functions. Never return ORM rows.
- Mask sensitive data in responses and in `repr`/logs, with `Field(repr=False)` or a masking serializer for IBAN and card fields.
- Cross-field rules go in `@model_validator(mode="after")`, with a docstring.
- No real customer data in `examples=`.
- Query parameters are `Annotated[..., Query(description=...)]`, and dependencies are `Annotated[Service, Depends(get_service)]`.

## Settings (`pydantic-settings`): root + nested groups

There is one root `Settings(BaseSettings)` made of `XxxSettings(BaseModel)` groups, read with `env_nested_delimiter="__"`. The env var `DATABASE__POOL_SIZE` becomes `settings.database.pool_size`.

```python
class DatabaseSettings(BaseModel):
    """
    Connection settings for the primary Postgres database.
    """

    host: Annotated[str, Field(description="Database host name.")]
    port: Annotated[PositiveInt, Field(le=65535, description="Database port.")] = 5432
    name: Annotated[NonEmptyStr, Field(description="Database name.")]
    user: Annotated[NonEmptyStr, Field(description="Login role.")]
    password: Annotated[SecretStr, Field(description="Login password.")]
    pool_size: Annotated[PositiveInt, Field(le=100, description="Maximum pooled connections.")] = 10


class Settings(BaseSettings):
    """
    Root application settings, loaded once at startup.
    """

    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore", frozen=True)

    database: Annotated[DatabaseSettings, Field(description="Primary database.")]
    http: Annotated[HttpSettings, Field(default_factory=HttpSettings, description="Outbound HTTP defaults.")]
```

Rules:
- **Every secret is `SecretStr`**: passwords, tokens, keys, connection strings. Call `.get_secret_value()` only on the line that uses it, and never log it.
- Secrets never have defaults. Leave a field required when a missing value should stop startup.
- Use precise types: `PositiveInt`, `NonNegativeInt`, `HttpUrl`, `StrEnum`.
- A group whose fields all have defaults uses `default_factory=Group` on the root. Otherwise the group is required.
- The composition root passes the **group** a component needs into its constructor. Never call `get_settings()` deep in services.
- **When you add a setting, add the key to `.env.example`** with an empty or placeholder value, and tell the user which keys to set in their own `.env`. You may not read or edit `.env` itself. That's blocked by design.
