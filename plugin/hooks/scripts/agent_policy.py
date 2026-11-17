#!/usr/bin/env python3
"""Subagent policy: cost routing and the parallel-worker cap.

* ``pre``  - PreToolUse on Agent: refuse expensive general-purpose subagents
             (they inherit the Opus orchestrator model) and refuse a new worker
             when ``CW_MAX_WORKERS`` (default 4) are already running.
* ``stop`` - SubagentStop: release a worker slot.

Slots are files in a per-session state dir, guarded by an flock. Slots older
than ``SLOT_TTL_SECONDS`` are treated as leaked and reclaimed.
"""

from __future__ import annotations

import fcntl
import os
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


def max_workers() -> int:
    """Return the configured parallel-worker limit.

    Returns:
        Value of ``CW_MAX_WORKERS``, defaulting to 4.
    """
    try:
        return max(1, int(os.environ.get("CW_MAX_WORKERS", "4")))
    except ValueError:
        return 4


@contextmanager
def slots_locked(payload: dict[str, Any]) -> Iterator[Path]:
    """Lock and yield the worker slot directory, pruning stale slots.

    Args:
        payload: Hook payload (scopes state to the session).

    Yields:
        The slot directory, while holding an exclusive lock.
    """
    root = state_dir(payload)
    slots = root / "workers"
    slots.mkdir(exist_ok=True)
    with open(root / "workers.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = time.time()
        for slot in slots.iterdir():
            if now - slot.stat().st_mtime > SLOT_TTL_SECONDS:
                slot.unlink(missing_ok=True)
        yield slots


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
            "'Explore' for searching, or 'cw:reviewer' for review."
        )
    if not agent_type.endswith("worker"):
        return ALLOW
    with slots_locked(payload) as slots:
        active = sorted(slots.iterdir(), key=lambda p: p.stat().st_mtime)
        if len(active) >= max_workers():
            block(
                f"{len(active)} workers already running (limit {max_workers()}). Wait for one to "
                "finish, or batch this work into an existing worker's task."
            )
        (slots / uuid.uuid4().hex).touch()
    return ALLOW


def on_stop(payload: dict[str, Any]) -> int:
    """Release the oldest worker slot when a worker finishes.

    Args:
        payload: SubagentStop payload.

    Returns:
        Always 0.
    """
    if not str(payload.get("agent_type", "")).endswith("worker"):
        return ALLOW
    with slots_locked(payload) as slots:
        active = sorted(slots.iterdir(), key=lambda p: p.stat().st_mtime)
        if active:
            active[0].unlink(missing_ok=True)
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
    if mode == "pre":
        return on_pre(payload)
    if mode == "stop":
        return on_stop(payload)
    return ALLOW


if __name__ == "__main__":
    sys.exit(main())
