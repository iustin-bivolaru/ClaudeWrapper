#!/usr/bin/env python3
"""Keep every CLAUDE.md at or under ``CW_CLAUDE_MD_MAX_LINES`` (default 200) lines.

CLAUDE.md is loaded into every session's context, so each extra line costs tokens on
every request. This PreToolUse hook works out the file as it would look after a
Write, Edit or MultiEdit, and blocks the change when the result would be too long.
It also blocks shell commands that write to a CLAUDE.md (redirects, ``tee``,
``sed -i``), because their result can't be checked in advance. The commit gate
re-checks staged CLAUDE.md files as a backstop.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, block, read_payload  # noqa: E402

CLAUDE_MD_NAME = re.compile(r"(^|/)CLAUDE(\.local)?\.md$")
SHELL_WRITE = re.compile(
    r"(>>?|\btee\b(\s+-a)?)\s*[\"']?[^\s;&|]*CLAUDE(\.local)?\.md\b"  # redirect or tee into it
    r"|\bsed\s+-i\b[^;&|]*CLAUDE(\.local)?\.md\b"  # in-place sed
    r"|\b(cp|mv)\s[^;&|]*[\s/]CLAUDE(\.local)?\.md\s*($|[;&|])"  # copied or moved onto it
)


def max_lines() -> int:
    """Return the configured line limit.

    Returns:
        Value of ``CW_CLAUDE_MD_MAX_LINES``, defaulting to 200.
    """
    try:
        return max(1, int(os.environ.get("CW_CLAUDE_MD_MAX_LINES", "200")))
    except ValueError:
        return 200


def is_claude_md(path: str) -> bool:
    """Tell whether a path is a CLAUDE.md (or CLAUDE.local.md) file.

    Args:
        path: File path from the tool call.

    Returns:
        True for any CLAUDE.md at any depth.
    """
    return bool(CLAUDE_MD_NAME.search(path.replace("\\", "/")))


def count_lines(text: str) -> int:
    """Count lines the way editors show them.

    Args:
        text: File content.

    Returns:
        Number of lines; a trailing newline doesn't add one.
    """
    return len(text.splitlines())


def resulting_text(tool: str, tool_input: dict[str, Any]) -> str | None:
    """Work out the file content after the pending tool call.

    Args:
        tool: ``Write``, ``Edit`` or ``MultiEdit``.
        tool_input: The tool's arguments.

    Returns:
        The new content, or None when it can't be worked out (then the edit is allowed and
        the commit gate catches any overflow).
    """
    if tool == "Write":
        return str(tool_input.get("content", ""))
    path = Path(str(tool_input.get("file_path", "")))
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
    for edit in edits or []:
        old_text = str(edit.get("old_string", ""))
        new_text = str(edit.get("new_string", ""))
        if old_text and old_text not in current:
            return None
        if edit.get("replace_all"):
            current = current.replace(old_text, new_text)
        else:
            current = current.replace(old_text, new_text, 1) if old_text else new_text + current
    return current


def overflow_message(path: str, line_count: int) -> str:
    """Build the instructions shown to Claude when a CLAUDE.md is too long.

    Args:
        path: The CLAUDE.md path.
        line_count: How many lines it would have.

    Returns:
        A message with concrete ways to shrink the file.
    """
    limit = max_lines()
    return (
        f"{path} would have {line_count} lines; the limit is {limit}. CLAUDE.md is loaded into every "
        f"request, so keep it short. Cut at least {line_count - limit} lines: keep only rules Claude can't infer "
        "from the code (commands, conventions, gotchas); remove anything a linter or hook already enforces; "
        "move long procedures into a skill (.claude/skills/<name>/SKILL.md) or docs/ and link to them; "
        "compress prose into one-line bullets; drop duplicate or outdated rules. Then retry."
    )


def main() -> int:
    """Check the pending tool call against the CLAUDE.md line limit.

    Returns:
        Exit code (blocking exits with 2 inside ``block``).
    """
    try:
        payload = read_payload()
    except ValueError:
        return ALLOW
    tool = str(payload.get("tool_name", ""))
    tool_input: dict[str, Any] = payload.get("tool_input") or {}
    if tool == "Bash":
        if SHELL_WRITE.search(str(tool_input.get("command", ""))):
            block(
                "don't write CLAUDE.md from the shell: its length can't be checked in advance. Use the "
                f"Write or Edit tool, and keep it at or under {max_lines()} lines."
            )
        return ALLOW
    path = str(tool_input.get("file_path", ""))
    if tool not in {"Write", "Edit", "MultiEdit"} or not is_claude_md(path):
        return ALLOW
    new_text = resulting_text(tool, tool_input)
    if new_text is not None and count_lines(new_text) > max_lines():
        block(overflow_message(path, count_lines(new_text)))
    return ALLOW


if __name__ == "__main__":
    sys.exit(main())
