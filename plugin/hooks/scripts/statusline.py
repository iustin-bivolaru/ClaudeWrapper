#!/usr/bin/env python3
"""Status line: model, branch, session cost and context usage at a glance."""

from __future__ import annotations

import json
import subprocess
import sys


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
        parts.append(f"ctx {ctx:.0f}%")
    print("  │  ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
