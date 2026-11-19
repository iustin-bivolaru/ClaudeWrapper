"""Conventional Commits validation for commit messages.

The format is ``<type>(<optional scope>)<optional !>: <description>``, for example
``feat(payments): add SEPA instant transfers`` or ``fix!: drop the legacy IBAN format``.
See https://www.conventionalcommits.org/en/v1.0.0/.
"""

from __future__ import annotations

import re
import shlex
from typing import Final

ALLOWED_TYPES: Final = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
"""Commit types accepted by the gate (the Angular/commitlint convention set)."""

MAX_HEADER_LENGTH: Final = 100
"""Hard limit for the header line (the commitlint default). The git-ship skill recommends ≤ 72."""

HEADER_PATTERN: Final = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?(?P<breaking>!)?: (?P<description>\S.*)$"
)
"""Shape of a header: type, optional lowercase scope, optional ``!``, colon, space, description."""


def header_problems(header: str) -> list[str]:
    """List every way a commit header breaks the Conventional Commits format.

    Args:
        header: The first line of a commit message.

    Returns:
        Human-readable problems; empty when the header is valid.
    """
    header = header.strip()
    match = HEADER_PATTERN.match(header)
    if not match:
        return [
            f"'{header}' isn't '<type>(<scope>): <description>'. The type is lowercase, the scope is "
            "optional and lowercase, and a colon and one space follow"
        ]
    problems: list[str] = []
    if match.group("type") not in ALLOWED_TYPES:
        problems.append(f"type '{match.group('type')}' must be one of: {', '.join(ALLOWED_TYPES)}")
    description = match.group("description")
    if description.endswith("."):
        problems.append("the description must not end with a period")
    if description[:1].isupper() and not description.split(" ", 1)[0].isupper():
        problems.append("start the description in lowercase, in the imperative ('add …', not 'Added …')")
    if len(header) > MAX_HEADER_LENGTH:
        problems.append(f"the header is {len(header)} characters; keep it ≤ 72 (hard limit {MAX_HEADER_LENGTH})")
    return problems


def _first_heredoc_line(message: str) -> str:
    """Pull the first real line out of a ``"$(cat <<'EOF' ... EOF)"`` message.

    Args:
        message: The raw ``-m`` argument.

    Returns:
        The first non-empty line inside the heredoc, or the message unchanged.
    """
    if "<<" not in message:
        return message
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if "<<" in line:
            for candidate in lines[index + 1 :]:
                if candidate.strip():
                    return candidate
    return message


def commit_header(command: str) -> str | None:
    """Find the header of the commit message that a ``git commit`` command will write.

    Args:
        command: Raw shell command line containing ``git commit``.

    Returns:
        The header line; ``""`` when the command opens an editor or reads a file (the
        message can't be checked); ``None`` when no new message is written (``--no-edit``).
    """
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    try:
        start = next(index for index in range(len(words) - 1) if words[index] == "git" and words[index + 1] == "commit")
    except StopIteration:
        return None
    arguments = words[start + 2 :]
    for index, argument in enumerate(arguments):
        if argument in {"&&", "||", ";", "|"}:
            arguments = arguments[:index]
            break
    if "--no-edit" in arguments:
        return None
    for index, argument in enumerate(arguments):
        message = None
        if argument in {"-m", "--message"} and index + 1 < len(arguments):
            message = arguments[index + 1]
        elif argument.startswith("--message="):
            message = argument.split("=", 1)[1]
        elif argument.startswith("-m") and len(argument) > 2 and not argument.startswith("--"):
            message = argument[2:]
        if message is not None:
            return _first_heredoc_line(message).splitlines()[0] if message.strip() else ""
    return ""
