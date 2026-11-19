#!/usr/bin/env python3
"""Naming check: flag single-letter identifiers in Python files.

Ruff's pep8-naming rules handle casing (variables must be ``snake_case``). Ruff has
no rule against names like ``x``, ``e`` or ``df``. This module covers single-letter
names in every place one can be bound. The only allowed one-character name is
``_`` (the deliberate throwaway). ``T = TypeVar("T")`` style declarations are
also allowed.

Usage: ``naming.py FILE [FILE ...]`` prints the violations and exits with 1
if there are any.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ALLOWED_NAMES = frozenset({"_"})
TYPE_VARIABLE_FACTORIES = frozenset({"TypeVar", "ParamSpec", "TypeVarTuple", "NewType"})


def _is_type_variable_declaration(assignment: ast.Assign) -> bool:
    """Tell whether an assignment declares a type variable (``T = TypeVar("T")``).

    Args:
        assignment: The assignment node to inspect.

    Returns:
        True if the assigned value is a call to a typing factory.
    """
    call = assignment.value
    if not isinstance(call, ast.Call):
        return False
    factory = call.func
    factory_name = factory.attr if isinstance(factory, ast.Attribute) else getattr(factory, "id", "")
    return factory_name in TYPE_VARIABLE_FACTORIES


class ShortNameFinder(ast.NodeVisitor):
    """Collect every single-letter identifier bound in a module.

    Args:
        file_name: Name used as the prefix in reported violations.
    """

    def __init__(self, file_name: str) -> None:
        """Initialise an empty violation list for ``file_name``."""
        self.file_name = file_name
        self.violations: list[tuple[int, str]] = []

    def _report(self, identifier: str, node: ast.AST, kind: str) -> None:
        """Record a violation if ``identifier`` is a single letter.

        Args:
            identifier: The bound name.
            node: The AST node, used for the line number.
            kind: What kind of binding this is, for the message.
        """
        if len(identifier) == 1 and identifier not in ALLOWED_NAMES:
            line_number = getattr(node, "lineno", 0)
            self.violations.append((line_number, f"single-letter {kind} '{identifier}' - use a descriptive name"))

    def visit_Assign(self, node: ast.Assign) -> None:
        """Skip type-variable declarations, check every other assignment."""
        if not _is_type_variable_declaration(node):
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Check names bound by assignment, ``for`` and ``with`` targets and comprehensions."""
        if isinstance(node.ctx, ast.Store):
            self._report(node.id, node, "variable")

    def visit_arg(self, node: ast.arg) -> None:
        """Check function and lambda parameters."""
        self._report(node.arg, node, "parameter")

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Check ``except ... as name`` bindings."""
        if node.name:
            self._report(node.name, node, "exception variable")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Check function names."""
        self._report(node.name, node, "function name")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Check async function names."""
        self._report(node.name, node, "function name")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Check class names."""
        self._report(node.name, node, "class name")
        self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> None:
        """Check ``import x as y`` aliases (``import numpy as np`` passes; ``as n`` does not)."""
        if node.asname:
            self._report(node.asname, node, "import alias")


def short_name_violations(path: Path) -> list[tuple[int, str]]:
    """Return the single-letter identifiers in one Python file, with their line numbers.

    Args:
        path: Python source file.

    Returns:
        ``(line_number, message)`` pairs; empty if the file is clean or can't be parsed.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    finder = ShortNameFinder(str(path))
    finder.visit(tree)
    return finder.violations


def find_short_names(path: Path) -> list[str]:
    """Return the single-letter identifier violations in one Python file.

    Args:
        path: Python source file.

    Returns:
        ``path:line: message`` strings, or an empty list if the file is clean or can't be parsed.
    """
    return [f"{path}:{line_number}: {message}" for line_number, message in short_name_violations(path)]


def main(arguments: list[str]) -> int:
    """Check the given files and print the violations.

    Args:
        arguments: File paths to check.

    Returns:
        1 if any violation was found, otherwise 0.
    """
    violations = [message for argument in arguments for message in find_short_names(Path(argument))]
    for message in violations:
        print(message)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
