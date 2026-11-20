"""The single entry point for every remi hook: one process per hook event.

``python3 dispatch.py <event>`` reads the payload once and runs, in order, the checks
that apply to it. The first check that blocks wins: it exits with 2 and its reason
goes to Claude on stderr. Modules are imported only when a check needs them, so a
plain Read or ``ls`` stays cheap.

Events:

* ``pre-tool``       - PreToolUse: the secret and git guard, the CLAUDE.md limit, and,
                       per tool, the commit gate and merge gate (Bash), the agent
                       policy and pre-review gate (Agent), the reviewer message check
                       (SendMessage). Write/Edit of a ``.py`` file is recorded for
                       the end-of-turn gates; nothing is linted per edit.
* ``subagent-start`` - SubagentStart: turn a worker's reservation into a slot.
* ``subagent-stop``  - SubagentStop: the worker gate, or record a reviewer's verdict.
* ``stop``           - Stop: the main session's end-of-turn gate, only if it edited Python.
* ``prompt``         - UserPromptSubmit: record the user's message for the request check,
                       or a reviewer's hand-back for the merge gate.

A malformed payload blocks a PreToolUse call (fail closed) and is ignored elsewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, BLOCK, block, read_payload, record_edit  # noqa: E402

FILE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})


def pre_tool(payload: dict[str, Any]) -> int:
    """Run every PreToolUse check that applies to the tool.

    Args:
        payload: PreToolUse payload.

    Returns:
        0 to allow; a blocking check exits with 2 itself.
    """
    tool = str(payload.get("tool_name", ""))
    if tool == "Agent":
        import agent_policy
        import quality

        return agent_policy.on_pre(payload) or quality.on_review(payload)
    if tool == "SendMessage":
        import agent_policy

        return agent_policy.on_message(payload)

    import guard

    guard.check(payload)
    if tool in FILE_WRITE_TOOLS or tool == "Bash":
        import claude_md

        claude_md.check(payload)
    if tool in FILE_WRITE_TOOLS:
        record_edit(payload)
    if tool == "Bash":
        import merge_gate
        import quality

        return quality.on_commit(payload) or merge_gate.check_merge(payload)
    return ALLOW


def stop(payload: dict[str, Any]) -> int:
    """Gate the end of the main session's turn.

    Args:
        payload: Stop payload.

    Returns:
        2 to send problems back, else 0.
    """
    import quality

    return quality.on_stop(payload)


def subagent_start(payload: dict[str, Any]) -> int:
    """Register a starting subagent.

    Args:
        payload: SubagentStart payload.

    Returns:
        Always 0.
    """
    import agent_policy

    return agent_policy.on_start(payload)


def subagent_stop(payload: dict[str, Any]) -> int:
    """Gate a stopping worker, or record a stopping reviewer's verdict.

    Args:
        payload: SubagentStop payload.

    Returns:
        2 to keep a worker going, else 0.
    """
    import quality

    return quality.on_worker_stop(payload)


def prompt(payload: dict[str, Any]) -> int:
    """Record the user's message, or a reviewer's hand-back.

    Args:
        payload: UserPromptSubmit payload.

    Returns:
        Always 0.
    """
    import agent_policy
    import merge_gate

    merge_gate.record_handback(payload)
    return agent_policy.on_prompt(payload)


EVENTS = {
    "pre-tool": pre_tool,
    "subagent-start": subagent_start,
    "subagent-stop": subagent_stop,
    "stop": stop,
    "prompt": prompt,
}


def main(arguments: list[str]) -> int:
    """Read the payload and run the handler for the event named on the command line.

    Args:
        arguments: Command-line arguments; the first is the event.

    Returns:
        Process exit code.
    """
    event = arguments[0] if arguments else ""
    handler = EVENTS.get(event)
    if handler is None:
        block(f"unknown hook event {event!r}; check plugin/hooks/hooks.json.")
    try:
        payload = read_payload()
    except ValueError as error:
        if event == "pre-tool":
            block(f"could not parse the hook payload ({error}); failing closed.")
        return ALLOW
    return BLOCK if handler(payload) else ALLOW


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
