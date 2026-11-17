---
name: add-sub-agent
description: Add a new sub-agent to an LLM orchestrator that routes each chat turn to specialised agents via route tools (LangGraph / deepagents style), with per-role entitlements and streamed events. Use when the user wants a new chat capability, a new agent, or a new kind of request the assistant should handle.
---

# Add a sub-agent

Load `python-service-style` too. A sub-agent is reachable only when **every** piece below exists. If you miss one, the agent is silently never offered, or startup crashes. Pick the smallest complete existing sub-agent as the reference and mirror it file for file.

Ask the user, or decide and state: what the agent handles, which roles may use it, whether it returns forms or cards to the UI, and whether it needs an external integration or settings.

## 1. Name and routing identity
- Add the agent to the agent-name enum. If that enum is stored as a Postgres enum (for example on messages), it **needs a migration** (`ALTER TYPE ... ADD VALUE`). See `add-migration`.
- Add its route tool name to the orchestrator's tool-name enum.

## 2. The agent package
Keep the project's per-agent layout. Typically:

| File | Holds |
|---|---|
| `agent.py` | the agent class (subclass of the shared agent base) with `name`, `description`, `astream(context)` |
| `context.py` | the input context from the invoker, plus a frozen runtime context for the tools |
| `state.py` | graph state, if it extends the base state |
| `prompts.py` | the system prompt as module constants |
| `middleware.py` | tool limits, tool filtering, prompt middleware |
| `tools/` | one tool per file (see `add-agent-tool`) |
| `consts.py` | progress texts, limits |

Rules:
- Build the graph once, in `__init__`, using the shared chat model and checkpointer.
- Restrict the agent to its own tools with a tool-filter middleware, because frameworks like deepagents add built-in tools.
- `astream` yields only the project's stream event types (usually progress, delta and **at most one** payload). Copy how the reference agent maps graph chunks to events and handles the recursion-limit error.
- Per-turn data and secrets go in the run's **context**, not its config, because LangGraph copies config values into checkpoint metadata.
- If the agent answers in prose, tell it to reply in the language of the user's message.

## 3. Orchestrator wiring
- **Route tool**: copy an existing one. Its description is what the orchestrator routes on, so say when to use it **and when not to**, so it doesn't overlap with the other agents. Register it in the orchestrator's tool list.
- **Invoker**: builds the agent's context from the orchestrator context. Give the agent its own thread id (`f"{conversation_id}:{agent_name}"`), attach the tracing callback, and pass form data when the turn is a form submission.
- **Catalogue entry**: add the route tool, the one-line "handles" text for the routing prompt, and any form events that should bypass routing.

## 4. Construction and entitlements
- Build the agent and register its invoker in the composition root. If it depends on an optional integration, build it only when that is enabled. Close any clients at shutdown.
- Add the agent to every role that may use it in the permissions or entitlements config. A role that doesn't list it never sees the route tool.
- Add a settings group (at least a recursion limit). See `add-setting`.

## 5. Verify
- Run lint and format-check, then start the app. Wiring errors show up at startup.
- Send a chat turn as each role and check the routing in the tracing tool.
- If the project has a prompt or agent review subagent, ask it to compare the new route description against the existing ones.
- Update the project's agent docs or CLAUDE.md if the agent adds a new pattern.
