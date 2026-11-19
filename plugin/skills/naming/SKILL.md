---
name: naming
description: Naming standard - descriptive, multi-word names written in lowercase words joined by a separator (variables_like_this in Python, variables-like-this where kebab-case is legal), never single letters or cryptic abbreviations. Use whenever writing or renaming variables, parameters, functions, classes, files, branches, CLI flags, URLs or config keys.
---

# Naming

Every name says **what the thing is**: real words, joined into lowercase multi-word names. Never use bare letters (`x`, `e`, `df`, `tmp1`).

A hook flags single-letter names in every Python file you edit, and the commit gate refuses commits that contain them. Ruff's pep8-naming rules (`N8xx`) enforce the casing.

## Which separator

Hyphens are not legal in Python identifiers (`account-balance` parses as `account - balance`). So the style depends on where the name lives:

| Where | Style | Example |
|---|---|---|
| Python variables, parameters, functions, methods, modules, packages | `snake_case` | `pending_transfers`, `calculate_interest_rate()`, `fx_rates.py` |
| Python classes, type aliases | `PascalCase` | `TransferRequest`, `AccountId` |
| Python constants | `UPPER_SNAKE_CASE` | `MAX_RETRY_ATTEMPTS` |
| Git branches, CLI flags, URL path segments, Docker/K8s resources, non-Python file names (YAML, SQL, Markdown) | `kebab-case` | `feat/sepa-instant-payments`, `--dry-run`, `/account-statements`, `payment-service`, `create-ledger-tables.sql` |
| Environment variables | `UPPER_SNAKE_CASE` | `DB_POOL_SIZE` |
| JSON/API fields | follow the existing API contract; for a new one, `snake_case` with Pydantic aliases if camelCase is required | `account_id` ↔ `accountId` |

## Rules

- **No single-letter names**, including loop variables, lambdas, comprehensions and `except` bindings:
  - `for i in range(n)` → `for attempt_number in range(max_attempts)`
  - `for k, v in mapping.items()` → `for currency_code, exchange_rate in rates.items()`
  - `except Exception as e` → `except ValidationError as validation_error`
  - `lambda x: x.amount` → `lambda transaction: transaction.amount`
  - The only exception is `_` for a value you deliberately ignore (`for _ in range(retry_count)`).
- **No cryptic abbreviations**: `acct` → `account`, `amt` → `amount`, `txn` → `transaction`, `cfg` → `settings`, `res` → `response`, `df` → `transactions_frame`. Well-known domain acronyms are fine as words: `iban`, `bic`, `fx`, `kyc`, `aml`, `sepa`, `http`, `url`, `id`.
- **Name by meaning, not by type**: `customer_accounts`, not `account_list` or `data`.
- **Booleans read as yes/no questions**: `is_active`, `has_overdraft`, `should_retry`, `can_withdraw`.
- **Functions are verbs**: `fetch_statement()`, `validate_iban()`. **Classes are nouns**: `StatementExporter`.
- **Include units where they matter**: `timeout_seconds`, `amount_minor_units`, `rate_percent`, `created_at_utc`.
- **Collections are plural**, and their items singular: `for payment in payments`.
- Don't shadow builtins (`id`, `type`, `list`, `input`, `hash`): use `account_id`, `payment_type`.
- When you touch existing code, rename the short names in the lines you change. Don't mass-rename untouched code in the same PR.
