#!/usr/bin/env python3
"""``remi onboard``: a read-only readiness report for using remi in an existing project.

It checks git and GitHub, the lint/format/test tooling, how far the code is from the
house style, CLAUDE.md length, the project's own ``.claude/`` setup, secret files (names
only, never contents) and package indexes the sandbox would block. Then it offers to
start a plan-mode session that writes the project's CLAUDE.md.

Usage: ``remi onboard [--print-prompt] [--no-prompt]``
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import is_secret_path  # noqa: E402
from claude_md import count_lines, max_lines  # noqa: E402
from naming import short_name_violations  # noqa: E402
from quality import has_tests, load_overrides, python_tool, run, run_stdout  # noqa: E402

OK, WARN, FAIL, INFO = "✓", "⚠", "✗", "·"
HOUSE_RULE_GROUPS = {
    "missing docstrings": "D101,D102,D103,D106,D419",
    "naming (pep8)": "N801,N802,N803,N806,N815,N816",
    "f-strings in log calls": "G004",
    "relative imports": "TID252",
}
SKIPPED_DIRECTORIES = {".venv", "venv", "node_modules", ".git", "__pycache__", "build", "dist"}


class Report:
    """Collects report lines and the facts worth passing to the onboarding session."""

    def __init__(self) -> None:
        """Start an empty report."""
        self.lines: list[str] = []
        self.findings: list[str] = []
        self.next_steps: list[str] = []

    def section(self, title: str) -> None:
        """Start a new titled section.

        Args:
            title: Section heading.
        """
        self.lines.append(f"\n{title}")

    def add(self, mark: str, text: str, *, finding: bool = False) -> None:
        """Add one line to the report.

        Args:
            mark: One of the status marks (OK, WARN, FAIL, INFO).
            text: The line text.
            finding: Also pass this line to the onboarding session as a fact.
        """
        self.lines.append(f"  {mark} {text}")
        if finding or mark in {WARN, FAIL}:
            self.findings.append(f"{mark} {text}")


def git(root: Path, *arguments: str) -> tuple[int, str]:
    """Run a git command and return its exit code and trimmed stdout.

    Args:
        root: Repository directory.
        arguments: Git arguments.

    Returns:
        Exit code and stdout.
    """
    exit_code, output = run_stdout("git " + " ".join(json.dumps(argument) for argument in arguments), root)
    return exit_code, output.strip()


def tracked_python_files(root: Path) -> list[Path]:
    """List tracked Python files, skipping virtualenvs and build output.

    Args:
        root: Repository root.

    Returns:
        Absolute paths.
    """
    _, listing = git(root, "ls-files", "*.py")
    return [
        root / name
        for name in listing.splitlines()
        if name and not SKIPPED_DIRECTORIES.intersection(Path(name).parts) and (root / name).is_file()
    ]


def check_git(root: Path, report: Report) -> None:
    """Report on the repository, branch, remote and GitHub CLI.

    Args:
        root: Repository root.
        report: Report to add to.
    """
    report.section("Git & GitHub")
    _, branch = git(root, "branch", "--show-current")
    exit_code, default_ref = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    default_branch = default_ref.removeprefix("origin/") if exit_code == 0 else "unknown"
    report.add(INFO, f"branch: {branch or 'detached'} · default branch: {default_branch}", finding=True)
    exit_code, remote = git(root, "remote", "get-url", "origin")
    if exit_code == 0:
        report.add(OK, f"remote origin: {remote}")
    else:
        report.add(WARN, "no 'origin' remote: remi can commit but not push or open PRs")
    _, status = git(root, "status", "--porcelain")
    changed = len([line for line in status.splitlines() if line.strip()])
    if changed:
        report.add(WARN, f"{changed} uncommitted change(s): commit or stash them before remi starts a task")
    else:
        report.add(OK, "working tree clean")
    if not shutil.which("gh"):
        report.add(WARN, "GitHub CLI 'gh' not installed: remi can't open PRs (brew install gh)")
        report.next_steps.append("brew install gh && gh auth login")
    elif subprocess.run(["gh", "auth", "status"], capture_output=True, check=False).returncode != 0:
        report.add(WARN, "gh is not logged in: run 'gh auth login' so remi can open PRs")
        report.next_steps.append("gh auth login")
    else:
        report.add(OK, "gh is installed and logged in")


def makefile_targets(root: Path) -> set[str]:
    """Return the Makefile targets that look like quality commands.

    Args:
        root: Repository root.

    Returns:
        Target names among test, lint, format, typecheck and check.
    """
    makefile = root / "Makefile"
    if not makefile.is_file():
        return set()
    names = re.findall(r"^([A-Za-z][\w-]*):", makefile.read_text(errors="replace"), flags=re.MULTILINE)
    return set(names) & {"test", "tests", "lint", "format", "fmt", "typecheck", "mypy", "check"}


def check_tooling(root: Path, report: Report) -> str | None:
    """Report on detected lint, format, test and typecheck commands.

    Args:
        root: Repository root.
        report: Report to add to.

    Returns:
        The ruff command prefix, or None when ruff isn't available.
    """
    report.section("Tooling")
    pyproject = root / "pyproject.toml"
    report.add(
        OK if pyproject.is_file() else WARN, "pyproject.toml found" if pyproject.is_file() else "no pyproject.toml"
    )
    overrides = load_overrides(root)
    if overrides:
        report.add(
            OK,
            "[tool.remi] overrides: " + ", ".join(f"{step}={command!r}" for step, command in overrides.items()),
            finding=True,
        )
    ruff = python_tool(root, "ruff")
    report.add(
        OK if ruff else WARN,
        f"ruff: {ruff}" if ruff else "ruff not found (add it as a dev dependency)",
        finding=not ruff,
    )
    if has_tests(root):
        pytest = python_tool(root, "pytest")
        report.add(
            OK if pytest else WARN,
            f"tests found, runner: {pytest}" if pytest else "tests found but pytest isn't installed",
            finding=True,
        )
    else:
        report.add(WARN, "no tests found: the gate can't run any")
    mypy = python_tool(root, "mypy")
    if mypy and "typecheck" not in overrides:
        report.add(INFO, f'mypy is available but not used: add  typecheck = "{mypy} ."  to [tool.remi]', finding=True)
    targets = makefile_targets(root)
    if targets:
        report.add(
            INFO,
            "Makefile targets: "
            + ", ".join(sorted(targets))
            + " (use them in [tool.remi] if they differ from the defaults)",
            finding=True,
        )
    return ruff


def count_rule_findings(ruff: str, root: Path, files: list[Path], rules: str | None) -> int:
    """Count ruff findings over files, with the project's config or explicit rules.

    Args:
        ruff: The ruff command prefix.
        root: Repository root.
        files: Files to check.
        rules: A ``--select`` list, or None for the project's own configuration.

    Returns:
        The number of findings.
    """
    selection = f"--select {rules} " if rules else ""
    quoted = " ".join(json.dumps(str(file_path)) for file_path in files)
    _, output = run_stdout(
        f"{ruff} check --quiet --no-cache --exit-zero --output-format json {selection}{quoted}", root, timeout=300
    )
    try:
        return len(json.loads(output or "[]"))
    except ValueError:
        return 0


def check_house_style(root: Path, ruff: str | None, report: Report) -> None:
    """Measure how far the existing code is from the house style.

    Args:
        root: Repository root.
        ruff: The ruff command prefix, or None.
        report: Report to add to.
    """
    report.section("Code baseline (legacy-safe: existing code is only checked on lines you change)")
    files = tracked_python_files(root)
    if not files:
        report.add(INFO, "no Python files")
        return
    report.add(INFO, f"{len(files)} Python files")
    if ruff:
        quoted = " ".join(json.dumps(str(file_path)) for file_path in files)
        _, format_output = run(f"{ruff} format --check --no-cache {quoted}", root, timeout=300)
        unformatted = len(
            set(re.findall(r"^Would reformat: (.+)$", format_output, flags=re.MULTILINE))
            | set(re.findall(r"^\s*--> (.+?):\d+:\d+$", format_output, flags=re.MULTILINE))
        )  # older ruff prints "Would reformat: path", newer prints a diagnostic with "--> path:line:col"
        mark = OK if unformatted == 0 else INFO
        report.add(
            mark,
            f"ruff format: {unformatted} file(s) not formatted"
            + (" (fine: remi formats only changed ranges in those)" if unformatted else ""),
            finding=bool(unformatted),
        )
        project_findings = count_rule_findings(ruff, root, files, None)
        report.add(
            OK if project_findings == 0 else INFO,
            f"project lint rules: {project_findings} existing finding(s)",
            finding=bool(project_findings),
        )
        for label, rules in HOUSE_RULE_GROUPS.items():
            findings = count_rule_findings(ruff, root, files, rules)
            report.add(OK if findings == 0 else INFO, f"{label}: {findings}", finding=bool(findings))
    single_letters = sum(len(short_name_violations(file_path)) for file_path in files)
    report.add(
        OK if single_letters == 0 else INFO, f"single-letter names: {single_letters}", finding=bool(single_letters)
    )


def check_claude_md(root: Path, report: Report) -> None:
    """Report every CLAUDE.md and whether it fits the line limit.

    Args:
        root: Repository root.
        report: Report to add to.
    """
    report.section("CLAUDE.md")
    _, listing = git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    found = [name for name in listing.splitlines() if re.search(r"(^|/)CLAUDE(\.local)?\.md$", name)]
    if not found:
        report.add(WARN, "no CLAUDE.md yet: the onboarding session below writes one")
        return
    for name in found:
        lines = count_lines((root / name).read_text(errors="replace"))
        if lines > max_lines():
            report.add(FAIL, f"{name}: {lines} lines (limit {max_lines()}): remi refuses edits until it's shortened")
        else:
            report.add(OK, f"{name}: {lines} lines")


def remi_skill_names() -> set[str]:
    """Return the names of the skills remi ships.

    Returns:
        Skill directory names from the plugin.
    """
    skills_directory = Path(__file__).resolve().parents[2] / "skills"
    return {path.name for path in skills_directory.iterdir() if path.is_dir()} if skills_directory.is_dir() else set()


def check_project_claude_dir(root: Path, report: Report) -> None:
    """Report the project's own agents, skills and hooks and how they interact with remi.

    Args:
        root: Repository root.
        report: Report to add to.
    """
    report.section("Project .claude/ setup")
    claude_directory = root / ".claude"
    if not claude_directory.is_dir():
        report.add(OK, "no project .claude/ directory")
        return
    agents = sorted(path.stem for path in (claude_directory / "agents").glob("*.md"))
    if agents:
        report.add(INFO, f"subagents {', '.join(agents)}: blocked under remi (only remi:* agents run)", finding=True)
    skills = (
        sorted(path.name for path in (claude_directory / "skills").iterdir() if path.is_dir())
        if (claude_directory / "skills").is_dir()
        else []
    )
    if skills:
        overlapping = sorted(set(skills) & remi_skill_names())
        report.add(INFO, f"skills: {', '.join(skills)} (they load alongside remi's)", finding=True)
        if overlapping:
            report.add(
                INFO,
                f"same name as a remi skill: {', '.join(overlapping)} - the project's version reflects local conventions",
                finding=True,
            )
    for settings_name in ("settings.json", "settings.local.json"):
        settings_path = claude_directory / settings_name
        if not settings_path.is_file():
            continue
        try:
            hooks = json.loads(settings_path.read_text()).get("hooks", {})
        except ValueError:
            report.add(WARN, f".claude/{settings_name} isn't valid JSON")
            continue
        for event, entries in hooks.items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    command = str(hook.get("command", ""))
                    duplicate = (
                        " - duplicates remi's lint/format hook, consider removing"
                        if re.search(r"ruff|black|isort|autopep8|yapf|flake8|pylint", command, re.IGNORECASE)
                        else ""
                    )
                    report.add(WARN if duplicate else INFO, f"hook {event}: {command[:70]}{duplicate}", finding=True)


def check_secrets(root: Path, report: Report) -> None:
    """Report secret files by name (never opening them) and whether git ignores them.

    Args:
        root: Repository root.
        report: Report to add to.
    """
    report.section("Secrets (names only, never read)")
    _, tracked = git(root, "ls-files")
    _, untracked = git(root, "ls-files", "--others", "--exclude-standard")
    _, ignored = git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory")

    def secret_names(listing: str) -> list[str]:
        return [
            name
            for name in listing.splitlines()
            if name and is_secret_path(name) and not SKIPPED_DIRECTORIES.intersection(Path(name).parts)
        ]

    committed, unignored, safe = secret_names(tracked), secret_names(untracked), secret_names(ignored)
    for name in committed:
        report.add(FAIL, f"{name} is committed to git: remove it from history and rotate its secrets")
    for name in unignored:
        report.add(WARN, f"{name} isn't git-ignored: add it to .gitignore")
    if safe:
        report.add(OK, f"git-ignored and unreadable for remi: {', '.join(safe)}")
    if not (committed or unignored or safe):
        report.add(OK, "no secret files found")
    if not (root / ".env.example").exists() and (safe or unignored):
        report.add(WARN, "no .env.example: remi documents new settings there, so create one with placeholder values")


def check_package_indexes(root: Path, report: Report) -> None:
    """Report package indexes the sandbox's network allowlist would block.

    Args:
        root: Repository root.
        report: Report to add to.
    """
    report.section("Sandbox network")
    hosts: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            tool = tomllib.loads(pyproject.read_text()).get("tool", {})
        except tomllib.TOMLDecodeError:
            tool = {}
        for index in tool.get("uv", {}).get("index", []) + tool.get("poetry", {}).get("source", []):
            hosts.add(re.sub(r"^\w+://(?:[^@/]*@)?([^/:]+).*$", r"\1", str(index.get("url", ""))))
    settings_path = (
        Path(os.environ.get("REMI_HOME", Path(__file__).resolve().parents[3])) / "settings" / "settings.json"
    )
    try:
        allowed = json.loads(settings_path.read_text())["sandbox"]["network"]["allowedDomains"]
    except (OSError, ValueError, KeyError):
        allowed = []

    def is_allowed(host: str) -> bool:
        return any(host == domain or (domain.startswith("*.") and host.endswith(domain[1:])) for domain in allowed)

    blocked = sorted(host for host in hosts if host and not is_allowed(host))
    if blocked:
        report.add(
            WARN,
            f"package index host(s) not in the sandbox allowlist: {', '.join(blocked)} - add them to sandbox.network.allowedDomains in remi's settings/settings.json",
        )
    else:
        report.add(OK, "all configured package indexes are reachable" if hosts else "no custom package indexes")


def onboarding_prompt(report: Report) -> str:
    """Build the prompt for the plan-mode onboarding session.

    Args:
        report: The completed report.

    Returns:
        The prompt text.
    """
    facts = "\n".join(f"- {line}" for line in report.findings) or "- no issues found"
    return (
        "Onboard remi to this repository. `remi onboard` found:\n"
        f"{facts}\n\n"
        "Plan (don't change anything until I approve):\n"
        "1. Map the architecture against the `service-architecture` skill (and `agent-orchestration-architecture` "
        "if there is an LLM agent layer). Use Grep/Read, and remi:explorer only for broad sweeps.\n"
        "2. Confirm the real lint, format, typecheck and test commands. Propose a `[tool.remi]` block in pyproject.toml "
        "only where the defaults don't fit.\n"
        f"3. Draft CLAUDE.md (at most {max_lines()} lines): commands, layout, conventions that differ from our house "
        "style, gotchas, and which commands need real secrets (so I run them with `!`). Leave out what linters and "
        "hooks already enforce.\n"
        "4. List fixes for the warnings above, in order of importance.\n"
        "After approval: branch `chore/remi-onboarding`, write CLAUDE.md (and `[tool.remi]` if needed), commit, push and open a PR."
    )


def main(arguments: list[str]) -> int:
    """Run the report and offer the onboarding session.

    Args:
        arguments: Command-line flags (``--print-prompt``, ``--no-prompt``).

    Returns:
        Process exit code.
    """
    exit_code, top_level = run_stdout("git rev-parse --show-toplevel", Path.cwd())
    if exit_code != 0:
        print("remi onboard: run this inside a git repository.", file=sys.stderr)
        return 2
    root = Path(top_level.strip())
    report = Report()
    check_git(root, report)
    ruff = check_tooling(root, report)
    check_house_style(root, ruff, report)
    check_claude_md(root, report)
    check_project_claude_dir(root, report)
    check_secrets(root, report)
    check_package_indexes(root, report)
    prompt = onboarding_prompt(report)
    if "--print-prompt" in arguments:
        print(prompt)
        return 0
    print(f"remi onboard · {root}")
    print("\n".join(report.lines))
    for step in report.next_steps:
        print(f"\n  → {step}")
    remi_launcher = Path(os.environ.get("REMI_HOME", Path(__file__).resolve().parents[3])) / "bin" / "remi"
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and "--no-prompt" not in arguments
    if interactive:
        answer = input("\nStart a plan-mode session that maps the project and writes CLAUDE.md? [y/N] ").strip().lower()
        if answer in {"y", "yes"}:
            os.execv(str(remi_launcher), [str(remi_launcher), "--plan", prompt])  # noqa: S606 - hand over to our own launcher
    print('\nTo start the onboarding session later:  remi --plan "$(remi onboard --print-prompt)"')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
