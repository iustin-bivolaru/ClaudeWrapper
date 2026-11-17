"""Which lines of a file a change touched, so legacy code outside the change is left alone.

A file that is new (not in ``HEAD``) or outside git is checked as a whole. For a file
that already exists in ``HEAD``, only the lines added or modified since ``HEAD`` (or
since the index, for the commit gate) count as the change.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ChangeScope:
    """The part of one file that a change touched.

    Attributes:
        whole_file: True for new files and files outside git: every line counts as changed.
        changed_lines: 1-based line numbers added or modified by the change.
    """

    whole_file: bool
    changed_lines: frozenset[int]

    def covers(self, line_number: int) -> bool:
        """Tell whether a line belongs to the change.

        Args:
            line_number: 1-based line number in the current file.

        Returns:
            True if the line was added or modified, or the whole file is in scope.
        """
        return self.whole_file or line_number in self.changed_lines

    @property
    def is_empty(self) -> bool:
        """True when the file has no added or modified lines at all."""
        return not self.whole_file and not self.changed_lines

    def ranges(self) -> list[tuple[int, int]]:
        """Group the changed lines into contiguous ``(first, last)`` ranges.

        Returns:
            Inclusive line ranges in ascending order; empty for a whole-file scope.
        """
        grouped: list[tuple[int, int]] = []
        for line_number in sorted(self.changed_lines):
            if grouped and line_number == grouped[-1][1] + 1:
                grouped[-1] = (grouped[-1][0], line_number)
            else:
                grouped.append((line_number, line_number))
        return grouped


WHOLE_FILE = ChangeScope(whole_file=True, changed_lines=frozenset())


def _git(root: Path, *arguments: str) -> tuple[int, str]:
    """Run a git command and return its exit code and stdout.

    Args:
        root: Directory to run in.
        arguments: Git arguments.

    Returns:
        Exit code (128 when git can't run) and stdout.
    """
    try:
        completed = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return 128, ""
    return completed.returncode, completed.stdout


def repository_path(root: Path, file_path: Path) -> str | None:
    """Return a file's path relative to the top of its git repository.

    Args:
        root: Any directory inside the repository.
        file_path: The file.

    Returns:
        The repository-relative path, or None when the file isn't inside a git repository.
    """
    exit_code, top_level = _git(
        file_path.resolve().parent if file_path.exists() else root, "rev-parse", "--show-toplevel"
    )
    if exit_code != 0 or not top_level.strip():
        return None
    try:
        return file_path.resolve().relative_to(Path(top_level.strip()).resolve()).as_posix()
    except ValueError:
        return None


def original_text(root: Path, file_path: Path) -> str | None:
    """Return the file's content at ``HEAD``.

    Args:
        root: Repository root.
        file_path: The file.

    Returns:
        The committed content, or None for new files and files outside git.
    """
    relative = repository_path(root, file_path)
    if relative is None:
        return None
    exit_code, content = _git(file_path.resolve().parent, "show", f"HEAD:{relative}")
    return content if exit_code == 0 else None


def change_scope(root: Path, file_path: Path, *, staged: bool = False) -> ChangeScope:
    """Work out which lines of a file the current change touched.

    Args:
        root: Repository root.
        file_path: The file.
        staged: Compare the index with ``HEAD`` (commit gate) instead of the working tree.

    Returns:
        The change scope: the whole file for new files, otherwise the added or modified lines.
    """
    relative = repository_path(root, file_path)
    if relative is None or original_text(root, file_path) is None:
        return WHOLE_FILE
    comparison = ["--cached", "HEAD"] if staged else ["HEAD"]
    exit_code, diff = _git(
        file_path.resolve().parent, "diff", "-U0", "--no-color", "--no-ext-diff", *comparison, "--", relative
    )
    if exit_code != 0:
        return WHOLE_FILE
    changed: set[int] = set()
    for match in HUNK_HEADER.finditer(diff):
        first_line = int(match.group(1))
        line_count = int(match.group(2)) if match.group(2) is not None else 1
        changed.update(range(first_line, first_line + line_count))
    return ChangeScope(whole_file=False, changed_lines=frozenset(changed))
