#!/usr/bin/env python3
"""Subagent policy: cost routing and the parallel-worker cap.

* ``pre``  - PreToolUse on Agent: refuse expensive general-purpose subagents
             (they inherit the Opus orchestrator model) and refuse a new worker
             when ``CW_MAX_WORKERS`` (default 3) are already running.
* ``start`` - SubagentStart: turn a pending reservation into an active slot
               keyed by the worker's ``agent_id``.
* ``stop``  - SubagentStop: free that worker's slot.

Running workers = pending reservations + active slots. Active slots are keyed by
agent id, so a worker that is resumed with SendMessage (for example for review
fixes) re-registers under the same id: it is never counted twice, and it never
frees another worker's slot. The state lives in a per-session directory guarded
by an flock. Entries older than ``SLOT_TTL_SECONDS`` are treated as leaked and
reclaimed.
"""

from __future__ import annotations

import fcntl
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
    "general-purpose": "cw:worker",
    "claude": "cw:worker",
}
ALLOWED_AGENT_TYPES = frozenset({"cw:worker", "cw:reviewer", "cw:explorer"})
"""Only the wrapper's own agents (the explorer runs on Haiku; the built-in Explore inherits the expensive session model) may run; project-defined agents are refused."""


def max_workers() -> int:
    """Return the configured parallel-worker limit.

    Returns:
        Value of ``CW_MAX_WORKERS``, defaulting to 3.
    """
    try:
        return max(1, int(os.environ.get("CW_MAX_WORKERS", "3")))
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
        True for ``cw:worker`` (or any agent type ending in ``worker``).
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
            "'cw:explorer' (Haiku) for broad searches, or 'cw:reviewer' for review."
        )
    if agent_type == "Plan":
        block(
            "in cw the orchestrator designs the plan itself, and 'Plan' subagents would run on the expensive "
            "orchestrator model. Research with Grep/Read yourself, or cw:explorer for broad sweeps, then write the plan file and call "
            "ExitPlanMode."
        )
    if agent_type == "Explore":
        block(
            "the built-in 'Explore' runs on the session's model (Opus), not a cheap one. For a targeted lookup, use "
            "Grep and Read yourself: starting any subagent costs about 20k tokens. For a broad sweep across many "
            "directories, use subagent_type='cw:explorer' (Haiku, read-only)."
        )
    if agent_type not in ALLOWED_AGENT_TYPES:
        block(
            f"subagent '{agent_type}' is not part of cw. Use only cw:worker (implementation), "
            "cw:reviewer (review) or cw:explorer (broad search)."
        )
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


def on_start(payload: dict[str, Any]) -> int:
    """Register a started (or resumed) worker under its agent id.

    Args:
        payload: SubagentStart payload.

    Returns:
        Always 0.
    """
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
    handlers = {"pre": on_pre, "start": on_start, "stop": on_stop}
    handler = handlers.get(mode)
    return handler(payload) if handler else ALLOW


if __name__ == "__main__":
    sys.exit(main())
