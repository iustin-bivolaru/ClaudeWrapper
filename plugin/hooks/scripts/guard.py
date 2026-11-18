"""PreToolUse guard: keeps secrets out of reach and enforces git policy.

Blocks (exit 2) any tool call that would read, write, search or shell out to
secret material (.env files, keys, cloud credentials, environment dumps) and
any git operation that bypasses the branch workflow (commits on main, force
pushes, ``--no-verify``). The guard fails closed: a malformed payload blocks.

Commands that need the user's credentials (``gh``, ``git push/pull/fetch``) and
``git init`` run outside the sandbox only when they stand alone, so the guard
refuses them inside a chain or pipe. A brand-new repository may get exactly one
empty bootstrap commit on its protected branch, published with
``gh repo create --push``, so that feature branches have a base to open PRs on.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, block, is_secret_path, working_dir  # noqa: E402

PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "release", "production"})
SHELL_OPERATORS = frozenset({";", "&", "|"})
UNSANDBOXED_GIT_COMMANDS = frozenset({"push", "pull", "fetch", "init"})

# Shell constructs that dump the environment or read secrets indirectly.
ENV_DUMP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern_text), reason)
    for pattern_text, reason in (
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
    (re.compile(pattern_text), reason)
    for pattern_text, reason in (
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
        (
            r"\bgh\s+repo\s+(create|edit)\b.*(\s--public\b|\s--visibility[=\s]+public\b)",
            "public repositories are not allowed for client code (use --private or --internal)",
        ),
        (
            r"(?is)\b(git\s+commit|gh\s+pr\s+(create|edit)|git\s+notes)\b.*"
            r"(co-authored-by:\s*claude|noreply@anthropic\.com|generated\s+with\s+\[?claude)",
            "Claude attribution is not allowed in commits or PRs; the user is the sole author",
        ),
    )
)


ALLOWED_AGENT_TYPES = frozenset({"remi:worker", "remi:reviewer", "remi:explorer"})
"""Built-in subagents (general-purpose, Plan, Explore) inherit the expensive session model."""


def check_agent(subagent_type: str) -> None:
    """Allow only remi's own subagents.

    Args:
        subagent_type: The Agent call's ``subagent_type`` (empty means general-purpose).
    """
    if subagent_type not in ALLOWED_AGENT_TYPES:
        block(
            f"subagent '{subagent_type or 'general-purpose'}' would run on the expensive session model or isn't "
            "part of remi. Use remi:worker (implementation), remi:explorer (broad search) or remi:reviewer (review); "
            "for a targeted lookup, Grep and Read yourself."
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


def _git_output(cwd: Path, *args: str) -> tuple[int, str]:
    """Run a read-only git command.

    Args:
        cwd: Directory to run git in.
        *args: Git arguments.

    Returns:
        ``(exit code, stdout)``; ``(1, "")`` when git can't run.
    """
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return out.returncode, out.stdout.strip()


def is_bootstrap_commit(command: str, cwd: Path) -> bool:
    """Tell whether a commit is the empty first commit of a brand-new repository.

    Args:
        command: Raw shell command line.
        cwd: Directory the command runs in.

    Returns:
        True when HEAD has no commits yet, nothing is staged and the commit is ``--allow-empty``.
    """
    if not re.search(r"\s--allow-empty\b", command):
        return False
    head_code, _ = _git_output(cwd, "rev-parse", "-q", "--verify", "HEAD")
    staged_code, staged = _git_output(cwd, "diff", "--cached", "--name-only")
    return head_code != 0 and staged_code == 0 and not staged


def is_bootstrap_head(cwd: Path) -> bool:
    """Tell whether the branch holds only the empty bootstrap commit.

    Args:
        cwd: Directory of the repository.

    Returns:
        True when HEAD is a single root commit that changes no files.
    """
    count_code, count = _git_output(cwd, "rev-list", "--count", "HEAD")
    files_code, files = _git_output(cwd, "show", "--name-only", "--format=", "HEAD")
    return count_code == 0 and count == "1" and files_code == 0 and not files


def check_standalone(command: str) -> None:
    """Refuse credential commands inside a chain or pipe.

    The sandbox runs ``gh``, ``git push/pull/fetch`` and ``git init`` outside it only
    when the whole command starts with them. Chained, they run sandboxed: ``gh`` can't
    read its token from the keychain (so it reports being logged out) and ``git init``
    can't write ``.git/config``.

    Args:
        command: Raw shell command line.
    """
    segments: list[list[str]] = [[]]
    for word in shell_words(command):
        if word in SHELL_OPERATORS:
            segments.append([])
        else:
            segments[-1].append(word)
    segments = [segment for segment in segments if segment]
    if len(segments) < 2:
        return
    for segment in segments:
        words = [word for word in segment if not re.match(r"^\w+=", word)]
        if not words:
            continue
        subcommand = next((word for word in words[1:] if not word.startswith("-")), "")
        if words[0] == "gh" or (words[0] == "git" and subcommand in UNSANDBOXED_GIT_COMMANDS):
            block(
                f"run `{' '.join(words[:3])}` on its own: no `&&`, `;` or pipe, and `cd` in a separate call first. "
                "Only a standalone command runs outside the sandbox with the user's credentials. Chained, `gh` "
                "can't read its token (it looks logged out) and `git init` can't write .git/config."
            )


def check_git_policy(command: str, cwd: Path) -> None:
    """Enforce the feature-branch workflow for git commands.

    Args:
        command: Raw shell command line.
        cwd: Directory the command runs in.
    """
    if re.search(r"\bgh\s+repo\s+create\b.*\s--push\b", command):
        pushed_branch = current_branch(cwd)
        if pushed_branch in PROTECTED_BRANCHES and not is_bootstrap_head(cwd):
            block(
                f"`gh repo create --push` would publish '{pushed_branch}' with real commits. It may only push the "
                "empty bootstrap commit of a new repository; push your work on a feature branch and open a PR."
            )

    if not re.search(r"\bgit\b", command):
        return
    switches_branch = re.search(r"\bgit\s+(checkout\s+-[bB]|switch\s+-[cC])\b", command)
    branch = current_branch(cwd)

    if re.search(r"\bgit\s+commit\b", command) and not switches_branch:
        if branch in PROTECTED_BRANCHES and not is_bootstrap_commit(command, cwd):
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
    check_standalone(command)
    check_git_policy(command, cwd)


def check(payload: dict[str, Any]) -> int:
    """Inspect a pending tool call and allow or block it.

    Args:
        payload: PreToolUse payload.

    Returns:
        0 to allow (blocking exits with 2 inside ``block``).
    """
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
        check_bash(str(args.get("command", "")), working_dir(payload))
    elif tool == "Agent":
        check_agent(str(args.get("subagent_type") or ""))
    return ALLOW
