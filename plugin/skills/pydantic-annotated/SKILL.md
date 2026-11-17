---
name: pydantic-annotated
description: Conventions for DTOs and Settings - Pydantic v2 models and pydantic-settings classes declared with Annotated[type, Field(...)], reusable constrained types, SecretStr for secrets, Decimal for money. Use whenever creating or changing request/response models, DTOs, schemas or configuration/settings.
---

# DTOs and Settings with `Annotated`

All fields are declared as `name: Annotated[Type, Field(...)]` — never `name: Type = Field(...)`.
Metadata lives in the annotation, the default value (if any) goes after `=`. This keeps constraints reusable and visible in the type.

## Reusable types — define once, in `types.py` (or `<package>/types.py`)

```python
"""Reusable constrained types shared by DTOs and settings."""

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CurrencyCode = Annotated[
    str, StringConstraints(pattern=r"^[A-Z]{3}$"), Field(description="ISO 4217 code", examples=["EUR"])
]
Money = Annotated[Decimal, Field(max_digits=18, decimal_places=2, description="Monetary amount")]
PositiveMoney = Annotated[Money, Field(gt=0)]
Iban = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
]
```

## DTOs

```python
class TransferRequest(BaseModel):
    """Payload to request a transfer between two accounts."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source_iban: Annotated[Iban, Field(description="Account to debit")]
    target_iban: Annotated[Iban, Field(description="Account to credit")]
    amount: Annotated[PositiveMoney, Field(description="Amount to transfer")]
    currency: CurrencyCode
    reference: Annotated[str | None, Field(max_length=140, description="Free text shown to the payee")] = None
```

Rules:
- `model_config = ConfigDict(extra="forbid")` on inbound DTOs (reject unknown fields); `frozen=True` unless mutation is required.
- Every field has a `description` (it feeds the OpenAPI schema and replaces per-field docstrings). The class has a docstring.
- **Money is `Decimal`**, never `float`. Ids are `UUID` or a constrained `str`, not bare `str`.
- Separate inbound (`…Request`/`…Create`) and outbound (`…Response`) models; never return ORM objects or internal models directly.
- Mask sensitive data in outbound DTOs and in `__repr__`/logs (`Field(repr=False)`, or a masked serializer for IBAN/card fields).
- Cross-field rules go in `@model_validator(mode="after")` with a docstring.
- No real customer data in `examples=`.

## Settings (`pydantic-settings`)

```python
class DatabaseSettings(BaseSettings):
    """Connection settings for the primary database."""

    model_config = SettingsConfigDict(env_prefix="DB_", env_file=".env", extra="ignore", frozen=True)

    host: Annotated[str, Field(description="Database host name")]
    port: Annotated[int, Field(ge=1, le=65535, description="Database port")] = 5432
    name: Annotated[NonEmptyStr, Field(description="Database name")]
    user: Annotated[NonEmptyStr, Field(description="Login role")]
    password: Annotated[SecretStr, Field(description="Login password")]
    pool_size: Annotated[int, Field(ge=1, le=100, description="Max pooled connections")] = 10


@lru_cache
def get_settings() -> AppSettings:
    """Return the process-wide settings, loaded once."""
    return AppSettings()
```

Rules:
- **Every secret is `SecretStr`** (passwords, tokens, keys, connection strings). Read it with `.get_secret_value()` only at the point of use; never log it.
- No secret has a default value. Non-secret fields may have safe defaults.
- Group settings per concern (`DatabaseSettings`, `KafkaSettings`…) and compose them in `AppSettings` with `Annotated[DatabaseSettings, Field(default_factory=DatabaseSettings)]`.
- Use `env_prefix` per group; `env_nested_delimiter="__"` when nesting.
- Access settings through a cached `get_settings()` / dependency injection, not module-level globals.
- **Whenever you add a setting, add it to `.env.example`** with a placeholder (`DB_PASSWORD=changeme`), never a real value. You may not read `.env` itself — it is blocked by design.
