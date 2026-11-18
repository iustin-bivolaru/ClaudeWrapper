"""The single entry point for every remi hook: one process per hook event.

``python3 dispatch.py <event>`` reads the payload once and runs, in order, the checks
that apply to it. The first check that blocks wins: it exits with 2 and its reason
goes to Claude on stderr. Modules are imported only when a check needs them, so a
plain Read or ``ls`` stays cheap.

Events:

* ``pre-tool``      - PreToolUse: the guard (secrets, git policy, allowed subagents), the
                      CLAUDE.md limit, and for Bash the commit gate and the merge gate.
* ``post-tool``     - PostToolUse on Edit/Write: fix, format and lint the file.
* ``subagent-stop`` - SubagentStop: record a reviewer's verdict for the merge gate.

A malformed payload blocks a PreToolUse call (fail closed) and is ignored elsewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, BLOCK, block, read_payload  # noqa: E402

FILE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})


def pre_tool(payload: dict[str, Any]) -> int:
    """Run every PreToolUse check that applies to the tool.

    Args:
        payload: PreToolUse payload.

    Returns:
        0 to allow; a blocking check exits with 2 itself.
    """
    import guard

    tool = str(payload.get("tool_name", ""))

    guard.check(payload)
    if tool in FILE_WRITE_TOOLS or tool == "Bash":
        import claude_md

        claude_md.check(payload)
    if tool == "Bash":
        import merge_gate
        import quality

        return quality.on_commit(payload) or merge_gate.check_merge(payload)
    return ALLOW


def post_tool(payload: dict[str, Any]) -> int:
    """Fix, format and lint an edited file.

    Args:
        payload: PostToolUse payload.

    Returns:
        2 when lint problems remain, else 0.
    """
    if str(payload.get("tool_name", "")) not in FILE_WRITE_TOOLS:
        return ALLOW
    import quality

    return quality.on_edit(payload)


def subagent_stop(payload: dict[str, Any]) -> int:
    """Record a stopping reviewer's verdict and the files it saw, for the merge gate.

    Args:
        payload: SubagentStop payload.

    Returns:
        Always 0.
    """
    if str(payload.get("agent_type", "")).endswith("reviewer"):
        import merge_gate

        merge_gate.record_review(payload)
    return ALLOW


EVENTS = {
    "pre-tool": pre_tool,
    "post-tool": post_tool,
    "subagent-stop": subagent_stop,
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
