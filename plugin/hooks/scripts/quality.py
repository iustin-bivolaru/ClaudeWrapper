#!/usr/bin/env python3
"""Quality gate: format, lint and test, wired into several hook events.

Modes (first CLI argument):

* ``edit``   - PostToolUse on Edit/Write: format + autofix the touched Python
               file and report leftover lint (incl. missing docstrings).
* ``commit`` - PreToolUse on Bash: before ``git commit`` scan the staged diff
               for secrets and run the full gate; block the commit on failure.
* ``worker`` - SubagentStop: a worker may not finish while the gate is red.

Commands are auto-detected (uv / .venv / PATH) and can be overridden per
project in ``pyproject.toml``::

    [tool.cw]
    format = "uv run ruff format ."
    lint = "uv run ruff check ."
    test = "uv run pytest -q -x"
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, BLOCK, block, is_secret_path, project_dir, read_payload  # noqa: E402
from agent_policy import on_stop as release_worker_slot  # noqa: E402
from naming import find_short_names  # noqa: E402

MAX_OUTPUT_LINES = 60  # keep feedback short: every line goes into the context window
DOCSTRING_RULES = "D101,D102,D103,D106,D419"  # style-neutral: works for reST and Google
NAMING_RULES = "N801,N802,N803,N806,N815,N816"  # pep8-naming: snake_case variables
HOUSE_RULES = "G004,TID252"  # no f-strings in log calls, no relative imports

SECRET_CONTENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern_text), reason)
    for pattern_text, reason in (
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
        (r"\b(AKIA|ASIA)[0-9A-Z]{16}\b", "AWS access key"),
        (r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", "GitHub token"),
        (r"\bxox[abpr]-[A-Za-z0-9-]{10,}", "Slack token"),
        (r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}", "API secret key"),
        (r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.", "JWT"),
        (
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]",
            "hard-coded credential",
        ),
        (
            r"(?i)\b(postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp|mssql)://[^:\s/]+:[^@\s]+@",
            "connection string with password",
        ),
        (r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b", "IBAN (possible client data)"),
    )
)
CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
ALLOW_MARKER = "cw: allow-secret"


def run(cmd: str, cwd: Path, timeout: int = 900) -> tuple[int, str]:
    """Run a shell command and capture combined output.

    Args:
        cmd: Shell command line.
        cwd: Working directory.
        timeout: Seconds before the command is killed.

    Returns:
        Tuple of (exit code, combined stdout/stderr).
    """
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {cmd}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def tail(text: str, limit: int = MAX_OUTPUT_LINES) -> str:
    """Keep only the last ``limit`` lines of tool output.

    Args:
        text: Raw output.
        limit: Maximum number of lines to keep.

    Returns:
        The trimmed text, prefixed with a marker when lines were dropped.
    """
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return f"... ({len(lines) - limit} lines trimmed)\n" + "\n".join(lines[-limit:])


def load_overrides(root: Path) -> dict[str, str]:
    """Read ``[tool.cw]`` command overrides from pyproject.toml.

    Args:
        root: Project root.

    Returns:
        Mapping of step name (format/lint/test) to shell command.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    try:
        data = tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("tool", {}).get("cw", {})
    return {step_name: str(command) for step_name, command in section.items() if isinstance(command, str)}


def python_tool(root: Path, name: str) -> str | None:
    """Find the best way to invoke a Python dev tool for this project.

    Args:
        root: Project root.
        name: Executable name, e.g. ``ruff`` or ``pytest``.

    Returns:
        A command prefix, or None when the tool is unavailable.
    """
    venv_bin = root / ".venv" / "bin" / name
    if venv_bin.exists():
        return str(venv_bin)
    if (root / "uv.lock").exists() and shutil.which("uv"):
        code, _ = run(f"uv run --frozen --no-sync {name} --version", root, timeout=60)
        if code == 0:
            return f"uv run --frozen --no-sync {name}"
    if shutil.which(name):
        return name
    if name == "ruff" and shutil.which("uvx"):
        return "uvx ruff"
    return None


def has_tests(root: Path) -> bool:
    """Tell whether the project contains any pytest-style tests.

    Args:
        root: Project root.

    Returns:
        True if a tests directory or test module exists.
    """
    if (root / "tests").is_dir():
        return True
    return any(
        test_file
        for test_file in root.glob("**/test_*.py")
        if ".venv" not in test_file.parts and "node_modules" not in test_file.parts
    )


def gate_commands(root: Path) -> list[tuple[str, str]]:
    """Build the ordered list of quality-gate steps for the project.

    Args:
        root: Project root.

    Returns:
        List of (step name, shell command) pairs.
    """
    overrides = load_overrides(root)
    steps: list[tuple[str, str]] = []
    ruff = python_tool(root, "ruff")
    defaults = {
        "format": f"{ruff} format --check ." if ruff else None,
        "lint": f"{ruff} check ." if ruff else None,
        "typecheck": None,
        "test": None,
    }
    if has_tests(root):
        pytest = python_tool(root, "pytest")
        defaults["test"] = f"{pytest} -q -x --no-header -p no:cacheprovider" if pytest else None

    package_json = root / "package.json"
    if package_json.is_file() and not (root / "pyproject.toml").exists():
        try:
            scripts = json.loads(package_json.read_text()).get("scripts", {})
        except (OSError, ValueError):
            scripts = {}
        for step in ("format", "lint", "typecheck", "test"):
            key = {"format": "format:check"}.get(step, step)
            if key in scripts:
                defaults[step] = f"npm run --silent {key}"

    for step in ("format", "lint", "typecheck", "test"):
        cmd = overrides.get(step, defaults[step])
        if cmd:
            steps.append((step, cmd))
    return steps


def changed_python_files(root: Path, *, staged_only: bool) -> list[Path]:
    """List the Python files touched by the pending change.

    Args:
        root: Repository root.
        staged_only: Only staged files (commit gate) instead of every modified or untracked file.

    Returns:
        Existing ``.py`` files, as absolute paths.
    """
    command = (
        "git diff --cached --name-only --diff-filter=ACMR"
        if staged_only
        else "git ls-files --modified --others --exclude-standard"
    )
    _, listing = run(command, root, timeout=30)
    candidates = (root / line.strip() for line in listing.splitlines() if line.strip().endswith(".py"))
    return sorted({candidate for candidate in candidates if candidate.is_file()})


def naming_report(files: list[Path]) -> str | None:
    """Check the given files for single-letter identifiers.

    Args:
        files: Python files to check.

    Returns:
        Failure report, or None when every name is descriptive.
    """
    violations = [message for file_path in files for message in find_short_names(file_path)]
    if not violations:
        return None
    return "naming failed (single-letter names; see the naming skill):\n" + tail("\n".join(violations), 30)


def run_gate(root: Path, *, staged_only: bool) -> str | None:
    """Run every gate step, stopping at the first failure.

    Args:
        root: Project root.
        staged_only: Check names only in staged files (commit) rather than in every changed file.

    Returns:
        Failure report, or None when everything passed.
    """
    naming_failure = naming_report(changed_python_files(root, staged_only=staged_only))
    if naming_failure:
        return naming_failure
    for step, cmd in gate_commands(root):
        code, out = run(cmd, root)
        if step == "test" and code == 5:  # pytest: no tests collected
            continue
        if code != 0:
            return f"{step} failed (`{cmd}`):\n{tail(out)}"
    return None


def on_edit(payload: dict[str, Any]) -> int:
    """Format and lint a single edited Python file.

    Args:
        payload: PostToolUse payload.

    Returns:
        Exit code; 2 sends remaining lint problems back to Claude.
    """
    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    path = Path(file_path)
    if path.suffix != ".py" or not path.is_file():
        return ALLOW
    root = project_dir(payload)
    problems: list[str] = []
    ruff = python_tool(root, "ruff")
    if ruff:
        quoted = json.dumps(str(path))
        run(f"{ruff} format --quiet {quoted}", root, timeout=60)
        is_test = path.name.startswith("test_") or path.name == "conftest.py"
        rules = f"{NAMING_RULES},{HOUSE_RULES}" if is_test else f"{DOCSTRING_RULES},{NAMING_RULES},{HOUSE_RULES}"
        lint_command = f"{ruff} check --fix --quiet --output-format concise --extend-select {rules} {quoted}"
        exit_code, lint_output = run(lint_command, root, timeout=60)
        if exit_code != 0 and lint_output:
            problems.append(lint_output)
    problems += find_short_names(path)
    if problems:
        print(
            f"Lint issues remain in {path.name}. Fix them now: public functions, methods and classes need "
            f"docstrings, and names must be descriptive snake_case words, never single letters:\n"
            f"{tail(chr(10).join(problems), 30)}",
            file=sys.stderr,
        )
        return BLOCK
    return ALLOW


def luhn_ok(digits: str) -> bool:
    """Validate a number with the Luhn checksum (payment card numbers).

    Args:
        digits: String of decimal digits.

    Returns:
        True if the checksum is valid.
    """
    total = 0
    for position, digit_character in enumerate(reversed(digits)):
        digit_value = int(digit_character)
        if position % 2 == 1:
            digit_value = digit_value * 2 - 9 if digit_value > 4 else digit_value * 2
        total += digit_value
    return total % 10 == 0


def scan_staged(root: Path) -> list[str]:
    """Scan staged files and added lines for secrets and client data.

    Args:
        root: Repository root.

    Returns:
        Human-readable findings; empty when the staged diff is clean.
    """
    findings: list[str] = []
    _, names = run("git diff --cached --name-only --diff-filter=ACMR", root, timeout=30)
    for name in names.splitlines():
        if is_secret_path(name):
            findings.append(f"{name}: secret file is staged (unstage it: git restore --staged {name})")

    if shutil.which("gitleaks"):
        code, out = run("gitleaks git --staged --no-banner --redact -v", root, timeout=120)
        if code not in (0, 127) and "unknown command" not in out:
            findings.append(f"gitleaks:\n{tail(out, 30)}")

    _, diff = run("git diff --cached -U0 --no-color", root, timeout=60)
    current = "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if not line.startswith("+") or ALLOW_MARKER in line:
            continue
        for pattern, why in SECRET_CONTENT_PATTERNS:
            if pattern.search(line):
                findings.append(f"{current}: {why}")
        for match in CARD_CANDIDATE.finditer(line):
            digits = re.sub(r"\D", "", match.group())
            if 13 <= len(digits) <= 19 and len(set(digits)) > 1 and luhn_ok(digits):
                findings.append(f"{current}: possible payment card number (Luhn-valid)")
    return sorted(set(findings))


def on_commit(payload: dict[str, Any]) -> int:
    """Gate ``git commit``: secret scan of the staged diff, then the full gate.

    Args:
        payload: PreToolUse payload for a Bash call.

    Returns:
        Exit code (blocking exits with 2 inside ``block``).
    """
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not re.search(r"\bgit\s+commit\b", command):
        return ALLOW
    if re.search(r"\bgit\s+(add|rm|mv)\b", command) or re.search(r"\bgit\s+commit\b.*\s-[a-zA-Z]*a", command):
        block(
            "stage files in a separate command first (git add <paths>), then run `git commit` "
            "alone, so the gate checks exactly what will be committed. Do not use `commit -a`."
        )
    root = project_dir(payload)
    findings = scan_staged(root)
    if findings:
        block(
            "possible secrets / client data in staged changes:\n- "
            + "\n- ".join(findings)
            + f"\nRemove them. For a deliberate test fixture, add the comment '{ALLOW_MARKER}' on that line."
        )
    failure = run_gate(root, staged_only=True)
    if failure:
        block(f"quality gate failed, commit refused.\n{failure}")
    return ALLOW


def on_worker_stop(payload: dict[str, Any]) -> int:
    """Keep a worker running until the gate passes, then free its slot.

    Args:
        payload: SubagentStop payload.

    Returns:
        Exit code; 2 makes the worker continue fixing.
    """
    code = _worker_gate(payload)
    if code == ALLOW:
        release_worker_slot(payload)
    return code


def _worker_gate(payload: dict[str, Any]) -> int:
    """Decide whether a stopping worker must keep going.

    Args:
        payload: SubagentStop payload.

    Returns:
        Exit code; 2 when the quality gate is red.
    """
    if payload.get("stop_hook_active"):
        return ALLOW  # already sent back once; let the orchestrator decide
    if not str(payload.get("agent_type", "")).endswith("worker"):
        return ALLOW
    root = project_dir(payload)
    _, status = run("git status --porcelain", root, timeout=30)
    if not status.strip():
        return ALLOW
    failure = run_gate(root, staged_only=False)
    if failure:
        print(f"Quality gate is red - fix it before finishing.\n{failure}", file=sys.stderr)
        return BLOCK
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
        return BLOCK if mode == "commit" else ALLOW
    handlers = {"edit": on_edit, "commit": on_commit, "worker": on_worker_stop}
    handler = handlers.get(mode)
    return handler(payload) if handler else ALLOW


if __name__ == "__main__":
    sys.exit(main())
