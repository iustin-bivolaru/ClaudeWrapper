"""Shared helpers for remi hook scripts.

Hooks receive a JSON payload on stdin and communicate back through the exit
code: ``0`` allows the action, ``2`` blocks it and feeds stderr back to Claude.
Only the standard library is used so hooks run in any project.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

ALLOW = 0
BLOCK = 2

# Files that may hold credentials. Template files (.env.example etc.) are
# matched separately and allowed, because they must never contain real values.
SECRET_PATH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern_text, re.IGNORECASE)
    for pattern_text in (
        r"(^|/)\.env($|\.)",  # .env, .env.local, .env.production ...
        r"(^|/)\.envrc$",
        r"\.(pem|key|p12|pfx|jks|keystore|truststore|kdbx|ovpn|ppk|asc|gpg)$",
        r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$",
        r"(^|/)\.ssh/",
        r"(^|/)\.aws/(credentials|config)$",
        r"(^|/)\.azure/",
        r"(^|/)\.config/gcloud/",
        r"(^|/)\.kube/config$",
        r"(^|/)\.docker/config\.json$",
        r"(^|/)\.netrc$",
        r"(^|/)\.pgpass$",
        r"(^|/)\.npmrc$",
        r"(^|/)\.pypirc$",
        r"(^|/)\.git-credentials$",
        r"(^|/)credentials(\.json|\.ya?ml|\.xml)?$",
        r"(^|/)secrets?(\.[\w-]+)?\.(json|ya?ml|toml|ini|properties|txt)$",
        r"(^|/)service[-_]account.*\.json$",
        r"\.tfstate(\.backup)?$",
        r"(^|/)terraform\.tfvars$",
        r"(^|/)vault[-_]?token",
    )
)

TEMPLATE_SUFFIX = re.compile(r"\.(example|sample|template|dist|defaults?|schema)$", re.IGNORECASE)


def read_payload() -> dict[str, Any]:
    """Read and parse the hook payload from stdin.

    Returns:
        The decoded JSON object, or an empty dict when stdin is empty.

    Raises:
        ValueError: If stdin holds something other than a JSON object.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hook payload is not a JSON object")
    return data


def block(reason: str) -> NoReturn:
    """Block the current action and explain why to Claude.

    Args:
        reason: Human-readable explanation, shown to the model via stderr.
    """
    print(f"[remi] BLOCKED: {reason}", file=sys.stderr)
    sys.exit(BLOCK)


def is_secret_path(path: str) -> bool:
    """Tell whether a path points at a file that may contain secrets.

    Args:
        path: Absolute or relative file path, possibly containing globs.

    Returns:
        True if the path matches a secret pattern and is not a template file.
    """
    normalized = path.replace("\\", "/").strip().strip("'\"")
    if not normalized:
        return False
    if TEMPLATE_SUFFIX.search(normalized):
        return False
    return any(secret_pattern.search(normalized) for secret_pattern in SECRET_PATH_PATTERNS)


def working_dir(payload: dict[str, Any]) -> Path:
    """Return the directory the tool call runs in.

    Args:
        payload: The hook payload.

    Returns:
        The payload's ``cwd``, else the directory Claude Code was started in.
    """
    return Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def repository_root(start: Path) -> Path:
    """Find the top level of the git repository that contains ``start``.

    Args:
        start: A directory inside the repository.

    Returns:
        The repository's top level, or ``start`` itself when it isn't in a repository.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return start
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else start


def project_dir(payload: dict[str, Any]) -> Path:
    """Resolve the repository the tool call works in.

    remi may be launched in a parent folder (for example to create a new project), so the
    repository around the call's ``cwd`` wins over the launch directory.

    Args:
        payload: The hook payload.

    Returns:
        The top level of the repository around the call's working directory.
    """
    return repository_root(working_dir(payload))


def state_dir(payload: dict[str, Any]) -> Path:
    """Return a per-session scratch directory for hook state.

    Args:
        payload: The hook payload; its ``session_id`` scopes the directory.

    Returns:
        An existing directory under ``REMI_STATE_DIR`` (default: ``$TMPDIR/remi``).
    """
    base = Path(os.environ.get("REMI_STATE_DIR") or Path(os.environ.get("TMPDIR", "/tmp")) / "remi")
    session = re.sub(r"[^\w-]", "_", str(payload.get("session_id") or "default"))
    path = base / session
    path.mkdir(parents=True, exist_ok=True)
    return path


def edits_log(payload: dict[str, Any]) -> Path:
    """Return the file that lists the Python files an agent has edited.

    Args:
        payload: Hook payload; its ``agent_id`` names the subagent, none means the main session.

    Returns:
        Path of the edit log (``main.txt`` for the main session).
    """
    edits_directory = state_dir(payload) / "edits"
    edits_directory.mkdir(exist_ok=True)
    agent_name = re.sub(r"[^\w-]", "_", str(payload.get("agent_id") or "main"))
    return edits_directory / f"{agent_name}.txt"


def record_edit(payload: dict[str, Any]) -> None:
    """Remember the Python file a Write/Edit is about to change, for the end-of-turn gates.

    Args:
        payload: PreToolUse payload for Write, Edit or MultiEdit.
    """
    file_path = str((payload.get("tool_input") or {}).get("file_path") or "")
    if not file_path.endswith(".py"):
        return
    with edits_log(payload).open("a", encoding="utf-8") as log_file:
        log_file.write(f"{Path(file_path).resolve()}\n")
