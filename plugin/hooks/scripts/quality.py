"""Quality gate: format, lint and test, wired into several hook events.

Handlers, called by ``dispatch.py``:

* ``stop``   - Stop (main session): when the orchestrator itself edited Python this
               turn, fix + format + lint those files, then run the tests if no
               worker is running. Nothing runs per edit; edits are only recorded.
* ``commit`` - PreToolUse on Bash: before ``git commit`` scan the staged diff
               for secrets and run the full gate; block the commit on failure.
* ``worker`` - SubagentStop: ``ruff check --fix`` + ``ruff format`` on the files
               this worker edited, then lint them; run the tests when no other
               worker is still running. A worker may not finish while red. For a
               reviewer, it records the verdict for the merge gate (``merge_gate.py``).
* ``review`` - PreToolUse on Agent: before ``remi:reviewer`` starts, every worker
               must be done, and fix + format + lint + tests must pass on the
               whole change. A red gate refuses the review.

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

from _common import ALLOW, BLOCK, block, edits_log, is_secret_path, project_dir, repository_root  # noqa: E402
from agent_policy import _slot_name, request_check_due, slots_locked  # noqa: E402
from agent_policy import on_stop as release_worker_slot  # noqa: E402
from claude_md import count_lines, is_claude_md, max_lines  # noqa: E402
from merge_gate import record_review  # noqa: E402
from naming import short_name_violations  # noqa: E402
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
        display_name = repository_path(root, file_path) or str(file_path)
        problems += [
            f"{display_name}:{line_number}: {message}"
            for line_number, message in short_name_violations(file_path)
            if scope.covers(line_number)
        ]
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


def edited_by_agent(payload: dict[str, Any]) -> list[Path]:
    """Return the Python files the agent (or, without ``agent_id``, the main session) edited.

    Args:
        payload: Stop or SubagentStop payload.

    Returns:
        Unique file paths, possibly empty.
    """
    log_path = edits_log(payload)
    if not log_path.is_file():
        return []
    return sorted({Path(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()})


def running_workers(payload: dict[str, Any], *, excluding_self: bool) -> int:
    """Count the workers that are pending or still running in this session.

    Args:
        payload: Hook payload (session scope and, optionally, ``agent_id``).
        excluding_self: Don't count the worker that sent ``payload``.

    Returns:
        The number of running or pending workers.
    """
    with slots_locked(payload) as (pending_directory, active_directory):
        active_names = {entry.name for entry in active_directory.iterdir()}
        if excluding_self:
            active_names.discard(_slot_name(payload))
        return len(active_names) + sum(1 for _ in pending_directory.iterdir())


def run_steps(root: Path, wanted: set[str]) -> str | None:
    """Run only some gate steps (for example just ``test`` and ``typecheck``).

    Args:
        root: Project root.
        wanted: Names of the steps to run.

    Returns:
        Failure report, or None when they all passed.
    """
    for step, cmd in gate_commands(root):
        if step not in wanted:
            continue
        exit_code, output = run(cmd, root)
        if step == "test" and exit_code == 5:  # pytest: no tests collected
            continue
        if exit_code != 0:
            return f"{step} failed (`{cmd}`):\n{tail(output)}"
    return None


def apply_project_fixes(root: Path, files: list[Path]) -> None:
    """Apply autofix and formatting to the change before a full gate.

    Uses ``[tool.remi] fix`` when the project sets it, otherwise ruff on the given files.

    Args:
        root: Project root.
        files: Changed Python files.
    """
    override = load_overrides(root).get("fix")
    if override:
        run(override, root)
        return
    lint_files(root, files, fix=True)


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


def staged_claude_md_problems(root: Path) -> list[str]:
    """Find staged CLAUDE.md files over the line limit.

    Args:
        root: Repository root.

    Returns:
        One message per file that is too long.
    """
    problems: list[str] = []
    _, names = run("git diff --cached --name-only --diff-filter=ACMR", root, timeout=30)
    for name in names.splitlines():
        if is_claude_md(name):
            _, staged_text = run(f"git show :{json.dumps(name)}", root, timeout=30)
            staged_lines = count_lines(staged_text)
            if staged_lines > max_lines():
                problems.append(f"{name} has {staged_lines} lines (limit {max_lines()})")
    return problems


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
    waiting_reviewer = request_check_due(payload)
    if waiting_reviewer:
        block(
            f"the reviewer ({waiting_reviewer}) hasn't checked the change against the user's request yet. Send it "
            "the user's messages verbatim in <request>...</request> with SendMessage, handle its request "
            "findings, then commit."
        )
    root = project_dir(payload)
    too_long = staged_claude_md_problems(root)
    if too_long:
        block(
            "CLAUDE.md is over the line limit: " + "; ".join(too_long) + ". Keep only rules Claude can't infer, "
            "move long procedures into skills or docs and link to them, then re-stage."
        )
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
    if str(payload.get("agent_type", "")).endswith("reviewer"):
        record_review(payload)
        return ALLOW
    code = _worker_gate(payload)
    if code == ALLOW:
        release_worker_slot(payload)
    return code


def _worker_gate(payload: dict[str, Any]) -> int:
    """Decide whether a stopping worker must keep going.

    It fixes, formats and lints only the files this worker edited, so a worker is never
    blocked by another worker's half-finished files. It runs the tests only when no
    other worker is still running. Otherwise the pre-review gate runs them on the whole
    change.

    Args:
        payload: SubagentStop payload.

    Returns:
        Exit code; 2 when the worker's own gate is red.
    """
    if payload.get("stop_hook_active"):
        return ALLOW  # already sent back once; the orchestrator decides what's next
    if not str(payload.get("agent_type", "")).endswith("worker"):
        return ALLOW
    root = project_dir(payload)
    others_running = running_workers(payload, excluding_self=True) > 0
    own_files = edited_by_agent(payload)
    if not own_files and not others_running:
        own_files = changed_python_files(root, staged_only=False)
    problems = lint_files(root, own_files, fix=True)
    if problems:
        print(
            "Your files still have lint problems (ruff --fix and format were already applied). Fix them:\n"
            + tail("\n".join(problems), 40),
            file=sys.stderr,
        )
        return BLOCK
    if others_running:
        return ALLOW
    _, status = run("git status --porcelain", root, timeout=30)
    if not status.strip():
        return ALLOW
    failure = run_steps(root, {"typecheck", "test"})
    if failure:
        print(
            "Tests are red. Fix them if the failure is in your files. If it's outside them, "
            f"stop and report it to the orchestrator.\n{failure}",
            file=sys.stderr,
        )
        return BLOCK
    return ALLOW


def on_stop(payload: dict[str, Any]) -> int:
    """End-of-turn gate for the main session: check the Python files it edited itself.

    Turns without Python edits cost nothing. Workers have their own gate on stop, and
    the pre-review gate covers the whole change, so this only catches XS work the
    orchestrator did by hand. Tests wait while a worker is running.

    Args:
        payload: Stop payload.

    Returns:
        Exit code; 2 sends the problems back so the turn continues once.
    """
    if payload.get("stop_hook_active"):
        return ALLOW  # already sent back once; don't loop
    edited_files = edited_by_agent(payload)
    if not edited_files:
        return ALLOW
    files_by_root: dict[Path, list[Path]] = {}
    for file_path in edited_files:
        files_by_root.setdefault(repository_root(file_path.parent), []).append(file_path)
    problems = [problem for root, files in files_by_root.items() for problem in lint_files(root, files, fix=True)]
    if problems:
        print(
            "Lint problems in the files you edited (ruff --fix and format were already applied). Fix them:\n"
            + tail("\n".join(problems), 40),
            file=sys.stderr,
        )
        return BLOCK
    if not running_workers(payload, excluding_self=False):
        for root in files_by_root:
            failure = run_steps(root, {"typecheck", "test"})
            if failure:
                print(f"Tests are red after your edits. Fix them:\n{failure}", file=sys.stderr)
                return BLOCK
    edits_log(payload).unlink(missing_ok=True)
    return ALLOW


def on_review(payload: dict[str, Any]) -> int:
    """Gate the reviewer: all workers are done and the whole change is green.

    The steps are fix + format, then naming, format check, lint, typecheck and tests,
    all on the whole uncommitted change.

    Args:
        payload: PreToolUse payload for the Agent tool.

    Returns:
        Exit code (blocking exits with 2 inside ``block``).
    """
    agent_type = str((payload.get("tool_input") or {}).get("subagent_type") or "")
    if not agent_type.endswith("reviewer"):
        return ALLOW
    still_running = running_workers(payload, excluding_self=False)
    if still_running:
        block(f"{still_running} worker(s) still running. Wait until every worker has finished, then start the review.")
    root = project_dir(payload)
    apply_project_fixes(root, changed_python_files(root, staged_only=False))
    failure = run_gate(root, staged_only=False)
    if failure:
        block(
            "the change isn't green, so the review is refused. Send each failure to the worker that owns "
            f"the file (SendMessage), then start the review again.\n{failure}"
        )
    return ALLOW
