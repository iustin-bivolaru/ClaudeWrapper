#!/usr/bin/env python3
"""Status line: model, branch, session cost and context usage at a glance, plus the occasional hamster."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zlib

CLEAR_HINT_PERCENT = 60
"""Context usage at which the status line suggests ``/clear`` once the current task is done."""

HAMSTER_FRAMES = ("∴(°ᴥ°)", "·(°ᴥ°)")
"""Running-right sprite; the dust trail alternates every refresh."""

HAMSTER_SPEED = 2
"""Columns the hamster moves per second (the status line refreshes every second)."""

HAMSTER_TRACK = 30
"""Default track width when Claude Code does not report the terminal width."""

HAMSTER_EVERY_MINUTES = 20
"""Default gap between runs; override with ``REMI_HAMSTER_EVERY`` (minutes), disable with ``REMI_HAMSTER=0``."""


def hamster(now: float, session_id: str, track: int, every_minutes: int) -> str:
    """Return the hamster's track for this moment, or ``""`` between runs.

    The hamster crosses ``track`` columns once every ``every_minutes``. Each session
    gets its own offset so parallel sessions do not run in lockstep.

    Args:
        now: Current Unix time in seconds.
        session_id: Claude Code session id, used to offset the schedule.
        track: Width of the track in columns.
        every_minutes: Minutes between the starts of two runs.

    Returns:
        The track with the hamster on it, or ``""`` when it is resting.
    """
    sprite_width = len(HAMSTER_FRAMES[0])
    if track <= sprite_width or every_minutes <= 0:
        return ""
    period = every_minutes * 60
    second = int(now + zlib.crc32(session_id.encode()) % period) % period
    position = second * HAMSTER_SPEED
    if position > track - sprite_width:
        return ""
    sprite = HAMSTER_FRAMES[second % len(HAMSTER_FRAMES)]
    return (" " * position + sprite).ljust(track)


def hamster_track(data: dict, line: str) -> str:
    """Return the hamster segment to append to ``line``, honouring the ``REMI_HAMSTER*`` settings.

    Args:
        data: The status line payload from Claude Code.
        line: The status line rendered so far.

    Returns:
        The segment to append (with its separator), or ``""``.
    """
    if os.environ.get("REMI_HAMSTER", "1") == "0":
        return ""
    try:
        every = int(os.environ.get("REMI_HAMSTER_EVERY", HAMSTER_EVERY_MINUTES))
    except ValueError:
        every = HAMSTER_EVERY_MINUTES
    columns = data.get("columns")
    track = min(HAMSTER_TRACK, columns - len(line) - 4) if isinstance(columns, int) else HAMSTER_TRACK
    run = hamster(time.time(), str(data.get("session_id", "")), track, every)
    return f"  {run}" if run else ""


def branch(cwd: str) -> str:
    """Return the current git branch, or an empty string.

    Args:
        cwd: Directory to query.

    Returns:
        Branch name or ``""``.
    """
    try:
        out = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def main() -> int:
    """Render one status line from the JSON Claude Code sends on stdin.

    Returns:
        Always 0.
    """
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    model = (data.get("model") or {}).get("display_name", "?")
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd", "")
    cost = float((data.get("cost") or {}).get("total_cost_usd") or 0)
    ctx = (data.get("context_window") or {}).get("used_percentage")
    br = branch(cwd)
    warn = " ⚠ on protected branch" if br in {"main", "master"} else ""
    parts = [f"🧠 {model}", f"🌿 {br or '-'}{warn}", f"💰 ${cost:.2f}"]
    if isinstance(ctx, (int, float)):
        hint = " · /clear when done" if ctx >= CLEAR_HINT_PERCENT else ""
        parts.append(f"ctx {ctx:.0f}%{hint}")
    line = "  │  ".join(parts)
    print(line + hamster_track(data, line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
