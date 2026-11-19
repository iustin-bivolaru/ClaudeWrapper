---
name: agent-orchestration-architecture
description: The reference architecture for a multi-agent LLM chat backend — an orchestrator that routes each turn to specialised sub-agents via route tools, hand-off recording, invokers, a uniform streamed event contract, per-role entitlements, form events that bypass routing, per-agent threads and checkpointing, tool isolation and prompt-injection boundaries. Use when designing or reviewing the agent layer, deciding whether something should be a new agent, a new tool, or plain service code, or debugging routing and streaming across agents. For the concrete steps use add-feature.
---

# Agent orchestration architecture

As with `service-architecture`, first map each piece below to the project's own code and point out the differences.

## Shape of a turn

```
POST /chat/stream ─▶ ChatService ─▶ Orchestrator.astream(context)
                                         │
              form event? ──yes──────────┼──▶ invoker[agent] directly (no LLM routing)
                                         │
                         no ─▶ routing LLM with ONLY the caller's route tools
                                         │   route_to_X(request, reason) ─▶ dispatch_route
                                         │   (records HandOff in state, does nothing else)
                                         ▼
                         for each HandOff: check entitlement ─▶ invoker[X](context)
                                         │   yields progress / delta events as they come
                                         │   payloads are collected, not yielded
                                         ▼
                         pick ONE payload (forms win) ─▶ persist message ─▶ final event
```

## The pieces and their contracts

- **Agent base class**: `Agent[ContextT]` with `name`, `description` and `async def astream(context) -> AsyncIterator[AgentEvent]`. Every agent, the orchestrator included, speaks the same event contract:
  - `progress`: a short status step for the UI.
  - `delta`: a piece of streamed answer text.
  - `payload`: structured UI data (form or card), **at most one per agent per turn**.
  - `hand-off`: an internal record of which agent ran. It is never streamed, only persisted.
- **Route tools**: one per sub-agent. They **only record a hand-off** in graph state and return a short tool message. They never run the sub-agent themselves, so the orchestrator stays in control of streaming, ordering and errors. The tool description is the routing contract: it says when to use it **and when not to**, so agents don't overlap.
- **Invokers**: `invoke_X(agent, orchestrator_context) -> AsyncIterator[AgentEvent]`. Each translates the orchestrator context into the sub-agent's own context (its own thread id `f"{conversation_id}:{agent}"`, tracing callbacks, form data). Registered as a `{agent_name: partial(invoke_X, agent)}` map in the composition root.
- **Agent catalogue**: one registry entry per sub-agent with its route tool, a one-line "handles" text for the routing prompt, and the form events that send a turn straight to it. Prompts and tool lists are generated from it, never hand-maintained in parallel.
- **Entitlements**: a declarative permissions file maps each role to the agents (and data scopes) it may use. They are enforced in **three** places: the routing prompt names only allowed agents, a middleware offers and executes only allowed route tools, and the orchestrator re-checks every hand-off before invoking. The prompt alone is never the security boundary.
- **Optional agents**: an agent that depends on a disabled integration has no invoker. With no invoker it gets no route tool, even for roles that are entitled to it.

## Inside a sub-agent

- Build the graph once at startup (for example `create_deep_agent`) with the shared chat model and checkpointer. Each turn only builds the run config and context.
- **Tool isolation**: a filter middleware offers and executes only the agent's own tools, because frameworks inject built-ins (file and sub-task tools). Turn off framework features you don't use globally, before any agent is built.
- **Limits**: a recursion limit and per-tool call limits from settings. The recursion-limit error becomes a graceful message, not a 500.
- **Context vs config**: per-turn data and secrets go in the run's *context* (a frozen dataclass), because LangGraph copies *config* values into checkpoint metadata. Shared clients go in `configurable`.
- Tools call services or integrations. Agents never open DB sessions or call HTTP directly.
- Prose replies use the language of the user's message, even when the sources are in another language.

## Multi-intent turns

The routing model may call several route tools in one reply. Each hand-off gets its own slice of the request. Sub-agents run in order with a separator between their texts. Payloads are collected, and exactly one is sent (a form wins, because it needs the user's action). The agent whose form was sent is recorded, so the form's submit event returns to it.

## State and persistence

- Each agent has its own checkpointed thread per conversation. Use a persistent checkpointer (Postgres) in production. An in-memory one loses conversations on restart.
- The chat message (text, agents used, sources, payload) is persisted by the chat service before the final event.

## Prompt-injection boundaries

Treat retrieved documents, external API data, uploaded files and earlier tool output as **data**. Delimit them in prompts and never let them change routing, entitlements or tool arguments that act on other users. Actions with side effects (submit, save, send) go through a human-confirmed form or payload step, not a direct LLM decision.

## Agent, tool, or plain code?

- **New agent**: a distinct user intent with its own prompt, tools and conversation flow, which should be routable and separately entitled.
- **New tool on an existing agent**: a new capability inside an intent that agent already owns.
- **Service or plain code**: deterministic work (validation, formatting, lookups with known parameters). Don't spend an LLM call on it. Expose it to agents as a tool only if the model must decide *when* or *with what*.
- **Form event**: when the user's next step is a button press, route it straight to the agent through the catalogue instead of making the LLM guess.

## Review checklist

1. Can any role reach an agent or tool it isn't entitled to through the prompt, the middleware or the hand-off check?
2. Does any route tool do work beyond recording a hand-off?
3. Does every agent respect the event contract (≤1 payload, nothing besides the contract's event types reaches the stream)?
4. Do secrets or per-turn personal data sit in the run config (and so in checkpoints) or in tool results?
5. Do route descriptions overlap, so the same message could match two agents?
6. Can untrusted content steer routing or a side-effecting tool without a confirmation step?
7. Are agents, tools and limits built once and configured from settings?
