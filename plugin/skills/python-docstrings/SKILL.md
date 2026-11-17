---
name: python-docstrings
description: Documentation standard for Python code - Google-style docstrings on every public module function, class and method (Args/Returns/Raises/Yields). Use whenever writing or modifying Python code.
---

# Python docstrings

Every **public** function, method and class gets a Google-style docstring. Private helpers (`_name`) get one when the logic is not obvious. Tests don't need them (a clear test name is enough).

A hook runs `ruff check --extend-select D101,D102,D103,D106,D417,D419` on each file you edit and reports what is missing — fix it immediately.

## Format

```python
def transfer(source: AccountId, target: AccountId, amount: Decimal, *, idempotency_key: str) -> TransferResult:
    """Move funds between two accounts of the same customer.

    The debit and credit run in one transaction; a repeated call with the same
    ``idempotency_key`` returns the original result instead of transferring twice.

    Args:
        source: Account to debit.
        target: Account to credit. Must differ from ``source``.
        amount: Positive amount in the account currency.
        idempotency_key: Client-supplied key that makes retries safe.

    Returns:
        The booked transfer with its ledger reference.

    Raises:
        InsufficientFundsError: If ``source`` balance is lower than ``amount``.
        ValueError: If ``amount`` is not positive or the accounts are equal.
    """
```

## Rules
- First line: one sentence, imperative mood ("Return…", "Validate…"), ends with a period.
- Blank line, then optional detail: **why** and non-obvious behaviour (side effects, idempotency, transactions, units/currency, time zones). Don't restate the code.
- `Args:` — every parameter except `self`/`cls`, in signature order. Don't repeat the type (it's in the annotation); describe meaning, units and constraints.
- `Returns:` (or `Yields:` for generators) — omit only for `-> None`.
- `Raises:` — exceptions the caller is expected to handle.
- Classes: describe what the class represents; document constructor args in the class docstring under `Args:` (or `Attributes:` for dataclasses/models when fields need explanation beyond `Field(description=...)`).
- Properties: a one-line docstring describing the value, written as a noun phrase.
- `@override` methods may use a one-liner if the base class documents the contract.
- Keep docstrings true: when you change behaviour, update the docstring in the same edit.
- Never put secrets, real customer data or internal hostnames in examples.
