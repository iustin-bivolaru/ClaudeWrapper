#!/usr/bin/env python3
"""Subagent policy: cost routing and the parallel-worker cap.

* ``pre``  - PreToolUse on Agent: refuse expensive general-purpose subagents
             (they inherit the Opus orchestrator model) and refuse a new worker
             when ``REMI_MAX_WORKERS`` (default 3) are already running.
* ``start`` - SubagentStart: turn a pending reservation into an active slot
               keyed by the worker's ``agent_id``.
* ``stop``  - SubagentStop: free that worker's slot.
* ``prompt`` - UserPromptSubmit: record the user's message, so the quote of the
               original request sent to the reviewer can be checked as verbatim.
* ``message`` - PreToolUse on SendMessage: check the request message sent to a reviewer.

The review has two steps. The reviewer is launched with one base-branch line and
nothing else, so its code review is blind. When a new reviewer starts (and the
user has sent a message this session), a request check falls due: the
orchestrator must send that reviewer the user's messages, quoted verbatim in
``<request>`` blocks, with SendMessage. The commit gate refuses to commit while
the check is due (see ``request_check_due``).

Running workers = pending reservations + active slots. Active slots are keyed by
agent id, so a worker that is resumed with SendMessage (for example for review
fixes) re-registers under the same id: it is never counted twice, and it never
frees another worker's slot. The state lives in a per-session directory guarded
by an flock. Entries older than ``SLOT_TTL_SECONDS`` are treated as leaked and
reclaimed.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, block, read_payload, state_dir  # noqa: E402

SLOT_TTL_SECONDS = 45 * 60
REDIRECT = {
    "general-purpose": "remi:worker",
    "claude": "remi:worker",
}
ALLOWED_AGENT_TYPES = frozenset({"remi:worker", "remi:reviewer", "remi:explorer"})
"""Only the wrapper's own agents (the explorer runs on Haiku; the built-in Explore inherits the expensive session model) may run; project-defined agents are refused."""

REQUEST_BLOCK = re.compile(r"<request>(.*?)</request>", re.DOTALL)
REQUESTS_FILE = "user-requests.json"
REVIEWERS_DIRECTORY = "reviewers"
REQUEST_CHECK_DUE_FILE = "request-check-due"
MAX_RECORDED_REQUESTS = 50
MAX_REVIEWER_LINE_CHARS = 200


def max_workers() -> int:
    """Return the configured parallel-worker limit.

    Returns:
        Value of ``REMI_MAX_WORKERS``, defaulting to 3.
    """
    try:
        return max(1, int(os.environ.get("REMI_MAX_WORKERS", "3")))
    except ValueError:
        return 3


@contextmanager
def slots_locked(payload: dict[str, Any]) -> Iterator[tuple[Path, Path]]:
    """Lock the worker state and yield its pending and active directories.

    Stale entries are pruned first.

    Args:
        payload: Hook payload (scopes state to the session).

    Yields:
        ``(pending_directory, active_directory)`` while an exclusive lock is held.
    """
    root = state_dir(payload)
    pending_directory = root / "workers-pending"
    active_directory = root / "workers-active"
    pending_directory.mkdir(exist_ok=True)
    active_directory.mkdir(exist_ok=True)
    with open(root / "workers.lock", "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        now = time.time()
        for directory in (pending_directory, active_directory):
            for entry in directory.iterdir():
                if now - entry.stat().st_mtime > SLOT_TTL_SECONDS:
                    entry.unlink(missing_ok=True)
        yield pending_directory, active_directory


def _oldest(directory: Path) -> list[Path]:
    """Return a directory's entries, oldest first.

    Args:
        directory: Directory to list.

    Returns:
        Entries sorted by modification time.
    """
    return sorted(directory.iterdir(), key=lambda entry: entry.stat().st_mtime)


def _is_worker(payload: dict[str, Any]) -> bool:
    """Tell whether a subagent event belongs to a worker.

    Args:
        payload: SubagentStart or SubagentStop payload.

    Returns:
        True for ``remi:worker`` (or any agent type ending in ``worker``).
    """
    return str(payload.get("agent_type", "")).endswith("worker")


def _slot_name(payload: dict[str, Any]) -> str:
    """Build a filesystem-safe slot name from the payload's agent id.

    Args:
        payload: SubagentStart or SubagentStop payload.

    Returns:
        The sanitised agent id, or a random name when there is none.
    """
    agent_id = str(payload.get("agent_id") or uuid.uuid4().hex)
    return re.sub(r"[^\w-]", "_", agent_id)


def _normalise(text: str) -> str:
    """Collapse all whitespace so quoting survives re-wrapping.

    Args:
        text: Text to normalise.

    Returns:
        The text with each whitespace run replaced by one space, stripped.
    """
    return " ".join(text.split())


def _recorded_requests(payload: dict[str, Any]) -> list[str]:
    """Load the user messages recorded for this session.

    Args:
        payload: Hook payload (scopes state to the session).

    Returns:
        The recorded messages, oldest first; empty when none or unreadable.
    """
    try:
        recorded = json.loads((state_dir(payload) / REQUESTS_FILE).read_text())
    except (OSError, ValueError):
        return []
    return [str(message) for message in recorded] if isinstance(recorded, list) else []


def _outside_requests(text: str) -> list[str]:
    """Return the non-empty lines of a prompt outside its ``<request>`` blocks.

    Args:
        text: Prompt or message text.

    Returns:
        Stripped, non-empty lines that are not part of a request block.
    """
    return [line.strip() for line in REQUEST_BLOCK.sub("", text).splitlines() if line.strip()]


def _has_context(text: str) -> bool:
    """Tell whether text holds more than one short instruction line.

    Args:
        text: Prompt or message text, request blocks excluded by the caller.

    Returns:
        True when there is more than one line, or a line too long to be a bare instruction.
    """
    outside = _outside_requests(text)
    return len(outside) > 1 or any(len(line) > MAX_REVIEWER_LINE_CHARS for line in outside)


def _check_reviewer_launch(payload: dict[str, Any]) -> None:
    """Refuse a reviewer launch that carries context or the request.

    Args:
        payload: PreToolUse payload for the Agent tool.
    """
    prompt = str((payload.get("tool_input") or {}).get("prompt") or "")
    if REQUEST_BLOCK.search(prompt) or "<request>" in prompt:
        block(
            "launch the reviewer blind: only the base-branch line. Send the user's request afterwards, "
            "when its code review is back, with SendMessage to the same reviewer."
        )
    if _has_context(prompt):
        block(
            "the reviewer gets no context. Its prompt is one line naming the base branch "
            '("Review the changes against `main`."). Do not add the plan, what was done or why.'
        )


def _reviewer_ids(payload: dict[str, Any]) -> Path:
    """Return the directory that marks which agent ids are reviewers.

    Args:
        payload: Hook payload (scopes state to the session).

    Returns:
        An existing directory with one empty file per reviewer agent id.
    """
    directory = state_dir(payload) / REVIEWERS_DIRECTORY
    directory.mkdir(exist_ok=True)
    return directory


def request_check_due(payload: dict[str, Any]) -> str:
    """Return the reviewer still waiting for the user's request, if any.

    Args:
        payload: Hook payload (scopes state to the session).

    Returns:
        The agent id of the latest reviewer whose request check hasn't been sent, or "".
    """
    try:
        return (state_dir(payload) / REQUEST_CHECK_DUE_FILE).read_text().strip()
    except OSError:
        return ""


def on_message(payload: dict[str, Any]) -> int:
    """Check the request message the orchestrator sends to a reviewer.

    Args:
        payload: PreToolUse payload for the SendMessage tool.

    Returns:
        Exit code (blocking exits with 2 inside ``block``).
    """
    tool_input = payload.get("tool_input") or {}
    recipient = re.sub(r"[^\w-]", "_", str(tool_input.get("to") or ""))
    if not recipient or not (_reviewer_ids(payload) / recipient).exists():
        return ALLOW
    message = str(tool_input.get("message") or "")
    quotes = [_normalise(quote) for quote in REQUEST_BLOCK.findall(message)]
    if not quotes:
        return ALLOW  # not a request check (e.g. a follow-up question)
    if _has_context(message):
        block(
            'the request check carries no context either: one instruction line ("Check the change against the '
            "user's request.\") plus the user's messages in <request>...</request>. No plan, no reasons."
        )
    recorded = [_normalise(message) for message in _recorded_requests(payload)]
    if not recorded:
        block("no user message is recorded for this session, so there is no request to check against.")
    for quote in quotes:
        if not quote or not any(quote in message for message in recorded):
            block(
                "a <request> block is not a verbatim quote of a user message. Copy the user's words exactly; "
                f"don't summarise or reword them. Not found: {quote[:120]!r}"
            )
    due_file = state_dir(payload) / REQUEST_CHECK_DUE_FILE
    if request_check_due(payload) == recipient:
        due_file.unlink(missing_ok=True)
    return ALLOW


def on_prompt(payload: dict[str, Any]) -> int:
    """Record the user's message for the reviewer's request check.

    Args:
        payload: UserPromptSubmit payload.

    Returns:
        Always 0: recording never blocks the user.
    """
    message = str(payload.get("prompt") or "").strip()
    if not message:
        return ALLOW
    root = state_dir(payload)
    with open(root / "requests.lock", "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        recorded = _recorded_requests(payload) + [message]
        (root / REQUESTS_FILE).write_text(json.dumps(recorded[-MAX_RECORDED_REQUESTS:]))
    return ALLOW


def on_pre(payload: dict[str, Any]) -> int:
    """Validate a pending Agent call.

    Args:
        payload: PreToolUse payload for the Agent tool.

    Returns:
        Exit code (blocking exits with 2 inside ``block``).
    """
    agent_type = str((payload.get("tool_input") or {}).get("subagent_type") or "general-purpose")
    if agent_type in REDIRECT:
        block(
            f"'{agent_type}' runs on the expensive orchestrator model. Use "
            f"subagent_type='{REDIRECT[agent_type]}' (Sonnet) for implementation, "
            "'remi:explorer' (Haiku) for broad searches, or 'remi:reviewer' for review."
        )
    if agent_type == "Plan":
        block(
            "in remi the orchestrator designs the plan itself, and 'Plan' subagents would run on the expensive "
            "orchestrator model. Research with Grep/Read yourself, or remi:explorer for broad sweeps, then write the plan file and call "
            "ExitPlanMode."
        )
    if agent_type == "Explore":
        block(
            "the built-in 'Explore' runs on the session's model (Opus), not a cheap one. For a targeted lookup, use "
            "Grep and Read yourself: starting any subagent costs about 20k tokens. For a broad sweep across many "
            "directories, use subagent_type='remi:explorer' (Haiku, read-only)."
        )
    if agent_type not in ALLOWED_AGENT_TYPES:
        block(
            f"subagent '{agent_type}' is not part of remi. Use only remi:worker (implementation), "
            "remi:reviewer (review) or remi:explorer (broad search)."
        )
    if agent_type == "remi:reviewer":
        _check_reviewer_launch(payload)
    if not agent_type.endswith("worker"):
        return ALLOW
    with slots_locked(payload) as (pending_directory, active_directory):
        running = len(_oldest(pending_directory)) + len(_oldest(active_directory))
        if running >= max_workers():
            block(
                f"{running} workers already running (limit {max_workers()}). Wait for one to finish, "
                "or give this work to the worker that owns the related files (SendMessage)."
            )
        (pending_directory / uuid.uuid4().hex).touch()
    return ALLOW


def _register_reviewer(payload: dict[str, Any]) -> None:
    """Remember a new reviewer and make its request check due.

    A reviewer resumed with SendMessage starts again under the same id; that is
    not a new review, so it leaves the due check alone.

    Args:
        payload: SubagentStart payload for a reviewer.
    """
    reviewer_marker = _reviewer_ids(payload) / _slot_name(payload)
    if reviewer_marker.exists():
        return
    reviewer_marker.touch()
    if _recorded_requests(payload):
        (state_dir(payload) / REQUEST_CHECK_DUE_FILE).write_text(reviewer_marker.name)


def on_start(payload: dict[str, Any]) -> int:
    """Register a started (or resumed) worker under its agent id.

    Args:
        payload: SubagentStart payload.

    Returns:
        Always 0.
    """
    if str(payload.get("agent_type", "")).endswith("reviewer"):
        _register_reviewer(payload)
    if not _is_worker(payload):
        return ALLOW
    with slots_locked(payload) as (pending_directory, active_directory):
        active_slot = active_directory / _slot_name(payload)
        if not active_slot.exists():
            pending_reservations = _oldest(pending_directory)
            if pending_reservations:
                pending_reservations[0].unlink(missing_ok=True)
        active_slot.touch()
    return ALLOW


def on_stop(payload: dict[str, Any]) -> int:
    """Free the slot of the worker that just finished.

    Args:
        payload: SubagentStop payload.

    Returns:
        Always 0.
    """
    if not _is_worker(payload):
        return ALLOW
    with slots_locked(payload) as (_, active_directory):
        (active_directory / _slot_name(payload)).unlink(missing_ok=True)
    return ALLOW


def main() -> int:
    """Dispatch to the mode given on the command line.

    Returns:
        Process exit code.
    """
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = read_payload()
    except ValueError:
        return ALLOW
    handlers = {"pre": on_pre, "start": on_start, "stop": on_stop, "prompt": on_prompt, "message": on_message}
    handler = handlers.get(mode)
    return handler(payload) if handler else ALLOW


if __name__ == "__main__":
    sys.exit(main())
