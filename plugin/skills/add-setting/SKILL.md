---
name: add-setting
description: Add a configuration value or a new settings group (env var) in a pydantic-settings project with nested groups. Use when code needs a tunable, secret, URL, limit, or feature flag.
---

# Add a setting

The pattern: one root `Settings(BaseSettings)` made of `XxxSettings(BaseModel)` groups, read from `.env` with a nested delimiter (usually `__`). `FOO__MAX_ITEMS` maps to `settings.foo.max_items`. Find the project's config module and its env example file first.

## Steps
1. **Group**: add a field to an existing group, or create a new group class next to the others.
   - Use precise types: `PositiveInt`, `NonNegativeInt`, `HttpUrl`, `SecretStr` for every secret (read it with `.get_secret_value()` only where it is used).
   - Declare every field with `Annotated`: `max_items: Annotated[PositiveInt, Field(description="...")] = 50`. Never use `max_items: int = Field(default=50, ...)`. Give a default when a sensible one exists. Leave the field required when a missing value should stop startup.
   - Cross-field rules go in `@model_validator(mode="after")`. Raise the project's application error with a specific code if it has one, so pydantic doesn't wrap it in a generic `ValueError`.
2. **Root**: add the group to `Settings`, with `Field(default_factory=XxxSettings)` if every field has a default, otherwise as a required field.
3. **Env example**: add every new key, with an empty or safe value, to `.env.example` (or its equivalent) in the matching block. That file is the list of env vars teammates see.
4. **Containers**: change compose or deployment files only if they list env vars one by one, or if the setting affects them (ports, volumes).
5. **Consume**: pass the group (not the whole `Settings`) into the component's constructor in the composition root. Don't call `get_settings()` deep inside services.
6. Tell the user which keys to add to their own `.env`. Never read or edit the real `.env` yourself.
