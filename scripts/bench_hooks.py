"""Measure what remi's hooks cost per tool call.

Reads ``plugin/hooks/hooks.json``, finds the hook commands that fire for a few typical
tool calls, and runs each one against a throwaway git repository. It reports how many
processes each call starts and how long they take: the sum (CPU spent) and the slowest
one (Claude Code runs matching hooks in parallel, so that is the wall time).

Usage: ``uv run python scripts/bench_hooks.py [--runs 10]``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"

SCENARIOS: list[tuple[str, str, dict[str, str]]] = [
    ("Read a file", "Read", {"file_path": "src/app.py"}),
    ("Grep", "Grep", {"pattern": "def ", "path": "src"}),
    ("Bash: ls", "Bash", {"command": "ls"}),
    ("Bash: git status", "Bash", {"command": "git status --short"}),
    ("Edit a .py file", "Edit", {"file_path": "src/app.py", "old_string": "1", "new_string": "2"}),
]


def matching_commands(event: str, tool_name: str) -> list[str]:
    """List the hook commands configured for one event and tool.

    Args:
        event: Hook event name, e.g. ``PreToolUse``.
        tool_name: The tool being called.

    Returns:
        The commands, with ``${CLAUDE_PLUGIN_ROOT}`` expanded.
    """
    config = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
    commands = []
    for group in config["hooks"].get(event, []):
        matcher = group.get("matcher", "")
        if matcher and not re.fullmatch(matcher, tool_name):
            continue
        for hook in group["hooks"]:
            commands.append(hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT)))
    return commands


def make_repository(root: Path) -> None:
    """Create a small git repository on a feature branch.

    Args:
        root: Empty directory to initialise.
    """
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text('"""App."""\n\nVALUE = 1\n')
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "bench@example.com"],
        ["config", "user.name", "bench"],
        ["add", "."],
        ["commit", "-qm", "chore: initial commit"],
        ["switch", "-qc", "feat/bench"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def time_command(command: str, payload: dict, environment: dict[str, str], cwd: Path) -> float:
    """Run one hook command and return its duration in milliseconds.

    Args:
        command: The hook command line.
        payload: JSON payload sent on stdin.
        environment: Process environment.
        cwd: Working directory.

    Returns:
        Elapsed milliseconds.
    """
    started = time.perf_counter()
    subprocess.run(
        shlex.split(command),
        input=json.dumps(payload),
        text=True,
        env=environment,
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    return (time.perf_counter() - started) * 1000


def main() -> None:
    """Run every scenario and print a table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=10, help="runs per scenario (median is reported)")
    runs = parser.parse_args().runs
    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary) / "repo"
        repository.mkdir()
        make_repository(repository)
        environment = os.environ | {
            "CLAUDE_PROJECT_DIR": str(repository),
            "REMI_STATE_DIR": str(Path(temporary) / "state"),
        }
        print(f"{'tool call':<20} {'processes':>9} {'sum ms':>8} {'slowest ms':>11}")
        for label, tool_name, tool_input in SCENARIOS:
            payload = {"tool_name": tool_name, "tool_input": tool_input, "cwd": str(repository), "session_id": "bench"}
            if tool_input.get("file_path"):
                tool_input["file_path"] = str(repository / tool_input["file_path"])
            commands = [("PreToolUse", command) for command in matching_commands("PreToolUse", tool_name)]
            commands += [("PostToolUse", command) for command in matching_commands("PostToolUse", tool_name)]
            medians = []
            for event, command in commands:
                event_payload = payload | {"hook_event_name": event}
                durations = [time_command(command, event_payload, environment, repository) for _ in range(runs)]
                medians.append(statistics.median(durations))
            print(f"{label:<20} {len(commands):>9} {sum(medians):>8.0f} {max(medians, default=0):>11.0f}")


if __name__ == "__main__":
    main()
