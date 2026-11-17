"""Shared helpers for remi hook scripts.

Hooks receive a JSON payload on stdin and communicate back through the exit
code: ``0`` allows the action, ``2`` blocks it and feeds stderr back to Claude.
Only the standard library is used so hooks run in any project.
"""

from __future__ import annotations

import json
import os
import re
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


def project_dir(payload: dict[str, Any]) -> Path:
    """Resolve the project root for the running session.

    Args:
        payload: The hook payload (its ``cwd`` is used as a fallback).

    Returns:
        The directory Claude Code was started in.
    """
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    return Path(root)


def state_dir(payload: dict[str, Any]) -> Path:
    """Return a per-session scratch directory for hook state.

    Args:
        payload: The hook payload; its ``session_id`` scopes the directory.

    Returns:
        An existing directory under the system temp folder.
    """
    base = Path(os.environ.get("TMPDIR", "/tmp")) / "remi"
    session = re.sub(r"[^\w-]", "_", str(payload.get("session_id") or "default"))
    path = base / session
    path.mkdir(parents=True, exist_ok=True)
    return path
