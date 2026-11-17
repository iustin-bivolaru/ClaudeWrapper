---
name: add-integration
description: Add a client for an external service (REST API, mail sender, third-party system) — abstract boundary, HTTP implementation on a shared pooled async client, external-response models and mappers, error translation, startup/shutdown wiring. Use when code must call a system outside this service.
---

# Add an external integration

Load `python-service-style` too. Look for an existing integration in the project and mirror its layout.

## 1. Settings
Use the `add-setting` skill for a `FooSettings` group: base URL (`HttpUrl`), credentials (`SecretStr`), timeout, and an `enabled: bool` flag if the feature is optional.

## 2. The boundary (abstract)
- `class FooClient(ABC)` with `@abstractmethod` async methods that use **the app's domain models**, never the external payload shapes.
- The class docstring states the contract: a missing entity returns `None` (never a fabricated record), and a failure raises the application error (never an empty result).
- Keep it as narrow as the feature needs. If you only read, make it read-only.

## 3. The implementation
- Wrap one pooled async HTTP client built in `__init__` from the settings group (base URL, auth headers, timeout). Reuse the project's shared HTTP client wrapper if it has one. Otherwise use `httpx.AsyncClient` with a whole-request timeout, `follow_redirects=False`, `trust_env=False` and TLS verification on.
- Add `async def aclose(self) -> None`. Say "Create it once at startup and close it with aclose() at shutdown." in the class docstring.
- Path segments go in a `StrEnum`. Retry and limit numbers are `Final` constants.
- Validate responses into Pydantic models of the **external** shape, using a module-level `TypeAdapter` for list or page types. Then map them to domain models with `from_<external>_to_<domain>` functions in a mapper module. Skip and count malformed records rather than failing a whole page.
- Translate errors once, here. Auth failures (401/403), not found (`None`), rate limiting (retry with a bounded `Retry-After`) and everything else each become a specific error code you add to the catalogue. Log the resource name and status, never response bodies.
- Bound per-item fan-out with a semaphore-based gather.

## 4. Wiring
- Build it once in the composition root, only when `enabled` if it is optional, and pass it to the services or agents that need it.
- Close it at shutdown next to the other clients.
- Code that can run without the integration must handle its absence explicitly. For example, don't build the feature that needs it.

## 5. Calling it
- Services depend on the abstract class, never on the vendor implementation or httpx.
- Send or sync work that must retry or happen after a commit through the outbox (see `add-outbox-job`).

## 6. Verify
- Run lint.
- Write a small dev script that calls each method. `httpx.MockTransport` lets you replay responses without the real service.
- Add the new keys to the env example file and tell the user which ones to set.
