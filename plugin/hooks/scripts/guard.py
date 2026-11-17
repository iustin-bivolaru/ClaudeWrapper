#!/usr/bin/env python3
"""PreToolUse guard: keeps secrets out of reach and enforces git policy.

Blocks (exit 2) any tool call that would read, write, search or shell out to
secret material (.env files, keys, cloud credentials, environment dumps) and
any git operation that bypasses the branch workflow (commits on main, force
pushes, ``--no-verify``). The guard fails closed: a malformed payload blocks.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, block, is_secret_path, project_dir, read_payload  # noqa: E402

PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "release", "production"})

# Shell constructs that dump the environment or read secrets indirectly.
ENV_DUMP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), why)
    for p, why in (
        (r"(^|[;&|(`]\s*|\$\(\s*)printenv\b", "printenv dumps environment secrets"),
        (r"(^|[;&|(`]\s*|\$\(\s*)env\s*($|[;&|>)])", "bare `env` dumps environment secrets"),
        (r"\bexport\s+-p\b|\bdeclare\s+-x\b|\bcompgen\s+-v\b", "dumps exported variables"),
        (r"(^|[;&|]\s*)set\s*($|[;&|>])", "bare `set` dumps shell variables"),
        (r"/proc/[^/\s]+/environ", "reads a process environment"),
        (
            r"\bpython[\d.]*\s+-c\b.*\b(os\.environ|os\.getenv|load_dotenv|dotenv_values)\b",
            "reads env/dotenv from inline code",
        ),
        (r"\bdotenv\s+(get|list)\b", "reads a .env file"),
        (r"\bsecurity\s+find-(generic|internet)-password\b", "reads the macOS keychain"),
        (r"\b(aws\s+configure\s+(get|export)|gcloud\s+auth\s+print-access-token)\b", "prints cloud credentials"),
        (r"\b(vault\s+(read|kv\s+get)|op\s+(read|item\s+get)|bw\s+get)\b", "reads a secrets manager"),
        (r"\bkubectl\s+get\s+secrets?\b.*(-o|--output)", "dumps Kubernetes secrets"),
        (r"\bgit\s+config\s+.*credential", "touches git credential config"),
    )
)

DESTRUCTIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), why)
    for p, why in (
        (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME)(\s|$)", "recursive delete of / or home"),
        (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", "pipes a download into a shell"),
        (r"\bsudo\b", "sudo is not allowed"),
        (r"\bgit\s+clean\s+-[a-zA-Z]*f", "git clean deletes untracked work"),
        (r"\bgit\s+reset\s+--hard\b", "git reset --hard discards work"),
        (r"\bgit\s+.*--no-verify\b|\bgit\s+commit\b.*\s-n(\s|$)", "--no-verify skips the quality gate"),
        (
            r"\bgit\s+push\b.*(\s--force(?!-with-lease)|\s-f(\s|$)|\s\+\S)",
            "force push (use --force-with-lease on your own branch)",
        ),
        (r"\bgit\s+(filter-branch|filter-repo)\b", "history rewriting"),
        (r"\bgit\s+push\b.*\s--(mirror|all|delete)\b", "bulk/deleting push"),
    )
)


def check_path(path: str | None, action: str) -> None:
    """Block the call if ``path`` points at secret material.

    Args:
        path: The path (or glob) a file tool is about to touch.
        action: Verb used in the block message, e.g. ``"read"``.
    """
    if not path:
        return
    unglobbed = re.sub(r"[*?\[\]{}]", "", path)  # "**/.env*" -> "/.env"
    if is_secret_path(path) or is_secret_path(unglobbed):
        block(
            f"{action} of '{path}' is forbidden: it may contain client credentials. "
            "Use the matching .env.example / settings class instead, or ask the user."
        )


def shell_words(command: str) -> list[str]:
    """Split a shell command into words, tolerating unbalanced quotes.

    Args:
        command: Raw shell command line.

    Returns:
        Words with shell operators split off, so ``cat .env|wc`` yields ``.env``.
    """
    spaced = re.sub(r"([;&|<>()`]|\$\()", r" \1 ", command)
    try:
        lexer = shlex.shlex(spaced, posix=True, punctuation_chars=False)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return spaced.split()


def current_branch(cwd: Path) -> str | None:
    """Return the checked-out branch name, or None outside a git repo.

    Args:
        cwd: Directory to run git in.

    Returns:
        Branch name, or None if it cannot be determined.
    """
    try:
        out = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def check_git_policy(command: str, cwd: Path) -> None:
    """Enforce the feature-branch workflow for git commands.

    Args:
        command: Raw shell command line.
        cwd: Directory the command runs in.
    """
    if not re.search(r"\bgit\b", command):
        return
    switches_branch = re.search(r"\bgit\s+(checkout\s+-[bB]|switch\s+-[cC])\b", command)
    branch = current_branch(cwd)

    if re.search(r"\bgit\s+commit\b", command) and not switches_branch:
        if branch in PROTECTED_BRANCHES:
            block(
                f"committing directly on '{branch}' is not allowed. Create a branch first: "
                "git switch -c <type>/<short-slug>"
            )

    push = re.search(r"\bgit\s+push\b(.*)", command)
    if push:
        targets = set(re.findall(r"(?:^|\s|:)([\w./-]+)", push.group(1)))
        hit = targets & PROTECTED_BRANCHES
        if hit or (branch in PROTECTED_BRANCHES and not switches_branch):
            block(
                f"pushing to protected branch '{', '.join(sorted(hit)) or branch}' is not allowed; "
                "push your feature branch and open a PR."
            )


def check_bash(command: str, cwd: Path) -> None:
    """Run all shell-command checks.

    Args:
        command: Raw shell command line.
        cwd: Directory the command runs in.
    """
    for word in shell_words(command):
        # Also catch paths embedded in options such as --env-file=.env
        for candidate in (word, word.split("=", 1)[-1]):
            if is_secret_path(candidate) or is_secret_path(candidate.rstrip("*")):
                block(
                    f"command references secret file '{candidate}'. Secrets must never be "
                    "read, copied, staged or sourced by the agent."
                )
    for pattern, why in ENV_DUMP_PATTERNS:
        if pattern.search(command):
            block(f"{why}. Environment variables may hold client credentials.")
    for pattern, why in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            block(f"{why}. Ask the user to run it themselves if it is really needed.")
    check_git_policy(command, cwd)


def main() -> int:
    """Entry point: inspect the pending tool call and allow or block it.

    Returns:
        Process exit code (0 allows; blocking exits with 2 inside ``block``).
    """
    try:
        payload = read_payload()
    except ValueError as exc:
        block(f"guard could not parse hook payload ({exc}); failing closed.")

    tool = str(payload.get("tool_name", ""))
    args: dict[str, Any] = payload.get("tool_input") or {}

    if tool == "Read":
        check_path(args.get("file_path"), "read")
    elif tool in {"Edit", "Write", "MultiEdit"}:
        check_path(args.get("file_path"), "write")
    elif tool == "NotebookEdit":
        check_path(args.get("notebook_path"), "write")
    elif tool == "Grep":
        check_path(args.get("path"), "search")
        check_path(args.get("glob"), "search")
    elif tool == "Glob":
        check_path(args.get("pattern"), "listing")
        check_path(args.get("path"), "listing")
    elif tool == "Bash":
        check_bash(str(args.get("command", "")), project_dir(payload))
    return ALLOW


if __name__ == "__main__":
    sys.exit(main())
