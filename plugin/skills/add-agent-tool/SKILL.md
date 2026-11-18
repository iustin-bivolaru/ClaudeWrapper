---
name: add-agent-tool
description: Add or change an LLM tool on an existing LangGraph / deepagents sub-agent — the @tool function, its input schema, how it reaches per-turn data and shared clients, and how the agent is allowed to call it. Use when an agent should be able to do something new, call a new API, or when changing a tool's arguments or behaviour. For a whole new agent use add-sub-agent.
---

# Add a tool to an agent

Load `python-service-style` too. Use an existing tool of the same agent that calls an external service as the reference, and look at how the agent's `__init__` wires its tools.

## 1. Name and input schema
- Add the name to the agent's tool-name `StrEnum`.
- Add a Pydantic `<Action>Input(BaseModel)`. Declare every field as `Annotated[T, Field(description=...)]`, with the description written for the model, plus limits (`max_length`, enums instead of free text). For fields the model may not know, use `Annotated[T | None, Field(description="... Leave null if unsure.")] = None`, so it doesn't invent values.

## 2. The tool: one file in the agent's `tools/`

```python
@tool(FooAgentTools.LOOKUP_ASSET, args_schema=LookupAssetInput)
async def lookup_asset(
    asset_tag: str,
    *,
    runtime: ToolRuntime[FooRuntimeContext],
) -> str:
    """
    <What it does and WHEN the model should use it.>

    :param asset_tag: ...
    :param runtime: The turn's runtime context and config.
    :return: ...
    """
```

- The docstring's first paragraph is shown to the model. Say when to use the tool and when not to.
- **Per-turn data and secrets** (caller identity, service URLs, API keys) come from `runtime.context`, a frozen dataclass. **Never put them in the config**, because LangGraph copies config values into checkpoint metadata.
- **Shared clients** (HTTP client, services) come from `runtime.config["configurable"]`, put there by the agent when it builds the run config. If one is missing, log an error and raise the internal-error application error. That is a wiring bug.
- Call downstream systems through a service or integration, not raw SQL or httpx inside the tool.
- Return a short string that starts with `Success.` or `Failure.`. Catch the application error and return a failure string so the model can tell the user. Never return secrets or raw personal data: the result goes to the LLM and into traces.
- Prefix logs with `[Tool: %s]`.
- If the tool produces a form or card for the UI, the agent's `astream` emits it as a payload event. The tool's string return isn't the payload.

## 3. Wire it into the agent
- Add it to the agent's `tools=[...]`.
- Make sure the tool filter lets it through, or the model never sees it. Some filters allow every member of the tool-name enum, so step 1 is enough. Others take an explicit allowed-names collection, so add the name there.
- If it needs a new per-turn value, add a field to the runtime context and fill it where the agent builds that context. If it needs a new client, inject it through the agent's constructor from the composition root.
- If it needs a call limit, use the agent's tool-call-limit middleware and settings (see `add-setting`).

## 4. Prompt
Update the agent's system prompt so it says when to use the new tool and how it relates to the existing ones. Keep any language or safety instructions it already has.

## 5. Verify
- Run lint.
- Send a chat turn that should trigger the tool. Check the call and its result in the tracing tool.
- If the project has a prompt or agent review subagent, ask it to review the description, the schema and the prompt.
