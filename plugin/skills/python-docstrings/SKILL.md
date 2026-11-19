---
name: python-docstrings
description: Documentation standard for Python code - reST docstrings (:param:/:returns:/:raises:) on every public module, class, method and function, in plain English. Use whenever writing or modifying Python code.
---

# Python docstrings (reST, house style)

**The project wins.** If the repo already uses another docstring style consistently, for example Google `Args:`, follow it. Otherwise use the reST style below, which is the house style (see also `python-service-style`).

Every **public** module, class, method and function gets a docstring. Private helpers (`_name`) get one when the logic isn't obvious. Tests don't need one: a clear test name is enough.

When an agent finishes, a hook flags public classes, methods and functions that have no docstring (ruff `D101`/`D102`/`D103`/`D106`/`D419`). Write them as you go so that check stays quiet.

## Format

```python
async def transfer(
    self, *, source_account_id: UUID, target_account_id: UUID, amount: Decimal, idempotency_key: str
) -> TransferModel:
    """
    Move funds between two accounts of the same customer.

    The debit and the credit run in one transaction. Repeating the call with the same
    idempotency key returns the original transfer instead of booking it twice.

    :param source_account_id: The account to debit.
    :param target_account_id: The account to credit. Must differ from the source.
    :param amount: A positive amount in the account currency.
    :param idempotency_key: A key from the client that makes retries safe.
    :returns: The booked transfer with its ledger reference.
    :raises AppError: INSUFFICIENT_FUNDS if the source balance is too low,
        VALIDATION_ERROR if the amount isn't positive or the accounts are the same,
        DATABASE_ERROR if the booking fails.
    """
```

## Rules
- Use the multi-line form: the opening `"""` goes on its own line and the summary starts on the next line.
- The summary is one sentence in the imperative ("Return…", "Validate…") and ends with a period.
- Add more detail only for the **why** and for behaviour that isn't obvious: side effects, idempotency, transactions, units or currency, time zones. Don't restate the code or the type hints.
- Write one `:param name:` line for every parameter except `self`/`cls`, in signature order. Describe what the value means and its constraints, not its type.
- Write `:returns:` unless the function returns `None`, and `:yields:` for generators.
- `:raises AppError:` names **which error codes** can be raised, and when.
- The `__init__` docstring holds only `:param:` lines. The class docstring says what the class is, its invariants, and its lifecycle ("Create it once at startup and close it with aclose() at shutdown.").
- Every module starts with a one-line docstring saying what it is for.
- Properties get a one-line docstring describing the value.
- Keep docstrings true: when you change behaviour, update the docstring in the same edit.
- Never put secrets, real customer data or internal hostnames in examples.
