"""Quality gate: format and lint on every edit, the full gate before every commit.

Two checkpoints, called by ``dispatch.py``:

* ``on_edit``   - PostToolUse on Edit/Write: autofix and format the touched Python file and
                  report leftover lint, so the agent fixes it while the file is fresh.
* ``on_commit`` - PreToolUse on Bash: before ``git commit``, scan the staged diff for secrets
                  and client data, then run lint, format, typecheck and tests.

Legacy-safe: a file that already exists in ``HEAD`` is only held to the rules on the
lines the change added or modified. Autofix and formatting run on the whole file only
if the committed version was already clean; otherwise formatting is limited to the
changed line ranges. New files are checked in full.

Commands are auto-detected (uv / .venv / PATH). Lint and format default to ruff,
scoped as above. Every step can be replaced per project in ``pyproject.toml``
(a replaced step runs as given, over the whole project)::

    [tool.remi]
    lint = "uv run ruff check ."
    format = "uv run ruff format --check ."
    typecheck = "uv run mypy src"
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

from _common import ALLOW, BLOCK, block, is_secret_path, project_dir, repository_root  # noqa: E402
from scope import ChangeScope, change_scope, original_text, repository_path  # noqa: E402

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
ALLOW_MARKER = "remi: allow-secret"


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


def run_stdout(cmd: str, cwd: Path, *, input_text: str | None = None, timeout: int = 120) -> tuple[int, str]:
    """Run a shell command, optionally feeding stdin, and return only its stdout.

    Args:
        cmd: Shell command line.
        cwd: Working directory.
        input_text: Text to pass on stdin.
        timeout: Seconds before the command is killed.

    Returns:
        Tuple of (exit code, stdout). Stderr is discarded so JSON output stays parseable.
    """
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, input=input_text, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, proc.stdout


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
    """Read ``[tool.remi]`` command overrides from pyproject.toml.

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
    section = data.get("tool", {}).get("remi", {})
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
    del ruff  # lint and format default to the scoped ruff checks in run_gate
    defaults: dict[str, str | None] = {"format": None, "lint": None, "typecheck": None, "test": None}
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
    if staged_only:
        _, listing = run("git diff --cached --name-only --diff-filter=ACMR", root, timeout=30)
    else:
        exit_code, tracked = run("git diff --name-only --diff-filter=ACMR HEAD", root, timeout=30)
        if exit_code != 0:  # no commits yet
            _, tracked = run("git diff --cached --name-only --diff-filter=ACMR", root, timeout=30)
        _, untracked = run("git ls-files --others --exclude-standard", root, timeout=30)
        listing = f"{tracked}\n{untracked}"
    candidates = (root / line.strip() for line in listing.splitlines() if line.strip().endswith(".py"))
    return sorted({candidate for candidate in candidates if candidate.is_file()})


def run_gate(root: Path, *, staged_only: bool) -> str | None:
    """Run every gate step, stopping at the first failure.

    Lint and format use the scoped ruff checks (changed lines of existing files, all of a
    new file) unless the project replaces them in ``[tool.remi]``. Typecheck and tests
    always run on the whole project.

    Args:
        root: Project root.
        staged_only: Check the staged change (commit gate) instead of the working tree.

    Returns:
        Failure report, or None when everything passed.
    """
    files = changed_python_files(root, staged_only=staged_only)
    overrides = load_overrides(root)
    problems = lint_files(root, files, fix=False, staged=staged_only)
    if problems:
        return "lint failed on the changed lines (legacy code outside the change isn't checked):\n" + tail(
            "\n".join(problems), 40
        )
    if "format" not in overrides:
        unformatted = unformatted_files(root, files, staged=staged_only)
        if unformatted:
            return "format failed: run `ruff format` on the changed code in: " + ", ".join(unformatted)
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
    root = repository_root(path.parent)
    problems = lint_files(root, [path], fix=True)
    if problems:
        print(
            f"Lint issues remain in {path.name}. Fix them now:\n{tail(chr(10).join(problems), 30)}",
            file=sys.stderr,
        )
        return BLOCK
    return ALLOW


def _is_test_file(file_path: Path) -> bool:
    """Tell whether a file is a pytest module (docstring rules don't apply to tests).

    Args:
        file_path: The file.

    Returns:
        True for ``test_*.py`` and ``conftest.py``.
    """
    return file_path.name.startswith("test_") or file_path.name == "conftest.py"


def _committed_version_is_clean(ruff: str, root: Path, file_path: Path, original: str, *, check: str) -> bool:
    """Tell whether the ``HEAD`` version of a file already passed a ruff check.

    If it did, fixing or formatting the whole file only touches the new code. If it
    didn't, doing so would rewrite untouched legacy lines.

    Args:
        ruff: The ruff command prefix.
        root: Project root.
        file_path: The file (used for config discovery).
        original: Its content at ``HEAD``.
        check: ``"format"`` or ``"lint"`` (the project's own rules, without house rules).

    Returns:
        True when the committed version was already clean.
    """
    stdin_name = json.dumps(str(file_path))
    command = (
        f"{ruff} format --check --quiet --stdin-filename {stdin_name} -"
        if check == "format"
        else f"{ruff} check --quiet --stdin-filename {stdin_name} -"
    )
    exit_code, _ = run_stdout(command, root, input_text=original)
    return exit_code == 0


def _apply_fixes(ruff: str, root: Path, file_path: Path, scope: ChangeScope) -> None:
    """Autofix and format a file without rewriting untouched legacy lines.

    Args:
        ruff: The ruff command prefix.
        root: Project root.
        file_path: The file to fix in place.
        scope: The lines the change touched.
    """
    quoted = json.dumps(str(file_path))
    original = None if scope.whole_file else original_text(root, file_path)
    if original is None or _committed_version_is_clean(ruff, root, file_path, original, check="lint"):
        run(f"{ruff} check --fix --quiet {quoted}", root, timeout=120)
    if original is None or _committed_version_is_clean(ruff, root, file_path, original, check="format"):
        run(f"{ruff} format --quiet {quoted}", root, timeout=120)
        return
    for first_line, last_line in reversed(scope.ranges()):
        run(f"{ruff} format --quiet --range {first_line}-{last_line + 1} {quoted}", root, timeout=120)


def _scoped_ruff_problems(ruff: str, root: Path, file_path: Path, scope: ChangeScope) -> list[str]:
    """Run ruff with the house rules and keep only findings on the changed lines.

    Args:
        ruff: The ruff command prefix.
        root: Project root.
        file_path: The file to check.
        scope: The lines the change touched.

    Returns:
        ``path:line:col: CODE message`` strings.
    """
    rules = (
        f"{NAMING_RULES},{HOUSE_RULES}"
        if _is_test_file(file_path)
        else f"{DOCSTRING_RULES},{NAMING_RULES},{HOUSE_RULES}"
    )
    exit_code, output = run_stdout(
        f"{ruff} check --quiet --output-format json --extend-select {rules} {json.dumps(str(file_path))}", root
    )
    if exit_code == 0:
        return []
    try:
        findings = json.loads(output or "[]")
    except ValueError:
        return [f"{file_path}: ruff failed to run"]
    display_name = repository_path(root, file_path) or str(file_path)
    problems = []
    for finding in findings:
        row = (finding.get("location") or {}).get("row", 0)
        if scope.covers(row):
            column = (finding.get("location") or {}).get("column", 0)
            problems.append(f"{display_name}:{row}:{column}: {finding.get('code')} {finding.get('message')}")
    return problems


def lint_files(root: Path, files: list[Path], *, fix: bool, staged: bool = False) -> list[str]:
    """Optionally fix and format files, then report what is wrong in the changed lines.

    The order is ``ruff check --fix``, then ``ruff format``, then a ``ruff check`` with
    the house rules (docstrings, pep8-naming, logging, imports), then the single-letter
    name check. New files are handled in full. Existing files are only checked on the
    lines the change added or modified. They're fixed and formatted as a whole only
    when the committed version was already clean, and otherwise formatted per changed
    range. That keeps diffs and agent work focused on the change.

    Args:
        root: Project root.
        files: Python files to process.
        fix: Apply fixes and formatting before checking.
        staged: Scope to the staged change (commit gate) instead of the working tree.

    Returns:
        Problem reports; empty when the changed code is clean.
    """
    existing = [file_path for file_path in files if file_path.suffix == ".py" and file_path.is_file()]
    ruff = python_tool(root, "ruff") if existing else None
    problems: list[str] = []
    for file_path in existing:
        scope = change_scope(root, file_path, staged=staged)
        if scope.is_empty:
            continue
        if ruff and fix:
            _apply_fixes(ruff, root, file_path, scope)
            scope = change_scope(root, file_path, staged=staged)
        if ruff:
            problems += _scoped_ruff_problems(ruff, root, file_path, scope)
    return problems


def unformatted_files(root: Path, files: list[Path], *, staged: bool = False) -> list[str]:
    """List changed files whose changed code isn't ruff-formatted.

    Files whose committed version was already formatted (and new files) must be fully
    formatted. For legacy files, only the changed ranges are compared with what
    ``ruff format --range`` would produce.

    Args:
        root: Project root.
        files: Changed Python files.
        staged: Scope to the staged change.

    Returns:
        Display names of the files that need formatting.
    """
    ruff = python_tool(root, "ruff")
    if not ruff:
        return []
    unformatted: list[str] = []
    for file_path in (candidate for candidate in files if candidate.suffix == ".py" and candidate.is_file()):
        scope = change_scope(root, file_path, staged=staged)
        if scope.is_empty:
            continue
        stdin_name = json.dumps(str(file_path))
        current = file_path.read_text(encoding="utf-8")
        original = None if scope.whole_file else original_text(root, file_path)
        if original is None or _committed_version_is_clean(ruff, root, file_path, original, check="format"):
            exit_code, _ = run_stdout(
                f"{ruff} format --check --quiet --stdin-filename {stdin_name} -", root, input_text=current
            )
            needs_format = exit_code != 0
        else:
            formatted = current
            for first_line, last_line in reversed(scope.ranges()):
                _, formatted = run_stdout(
                    f"{ruff} format --quiet --range {first_line}-{last_line + 1} --stdin-filename {stdin_name} -",
                    root,
                    input_text=formatted,
                )
            needs_format = bool(formatted) and formatted != current
        if needs_format:
            unformatted.append(repository_path(root, file_path) or str(file_path))
    return unformatted


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
