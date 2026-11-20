#!/usr/bin/env python3
"""``remi ship``: commit, push, open or update the PR. ``remi merge``: merge it when the user asks.

The orchestrator writes only the text: the commit message, the PR title and the PR body.
Everything mechanical happens here, in one command:

1. Refuse on a protected branch, a non-Conventional commit header or Claude attribution.
2. If anything changed: stage it (what is already staged, else ``git add -A``), refuse
   secret files and the PR body file, run the commit gate (request check, CLAUDE.md, secret scan, format, lint,
   tests) and commit.
3. Check the branch's PR: a merged or closed PR means a push would never reach the base.
4. ``git push origin HEAD``, then ``gh pr create`` (or ``gh pr edit`` to keep it current).
5. Report whether the review would allow a merge. ``remi ship`` never merges.

``remi merge`` merges the branch's open PR with ``--match-head-commit``, only when the
user's latest message asks for a merge and the latest review said ``APPROVE`` +
``MATCHES`` on exactly this code.

Git hooks never see the ``git commit`` and ``gh pr merge`` run here, so the same gate
functions are called directly. The command runs outside the sandbox (``gh`` needs the
keychain), like the hooks themselves. The guard only allows it as a standalone command.

Usage::

    remi ship -m "feat(x): add y" [-m "<why>"] --title "Add y to x" [--body-file .remi-pr.md]
    remi merge
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import block, is_secret_path, project_dir  # noqa: E402
from conventional_commits import header_problems  # noqa: E402
from guard import PROTECTED_BRANCHES  # noqa: E402

DEFAULT_BODY_FILE = ".remi-pr.md"
ATTRIBUTION = re.compile(r"(?i)co-authored-by:\s*claude|noreply@anthropic\.com|generated\s+with\s+\[?claude")


def run(root: Path, *command: str) -> subprocess.CompletedProcess[str]:
    """Run a command in the repository without raising.

    Args:
        root: Repository directory.
        *command: The program and its arguments.

    Returns:
        The finished process with its output captured.
    """
    return subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=600, check=False)  # noqa: S603


def must(root: Path, *command: str) -> str:
    """Run a command and stop shipping if it fails.

    Args:
        root: Repository directory.
        *command: The program and its arguments.

    Returns:
        The command's stdout.
    """
    result = run(root, *command)
    if result.returncode != 0:
        block(f"`{' '.join(command[:4])}` failed:\n{(result.stderr or result.stdout).strip()}")
    return result.stdout


def parse(arguments: list[str]) -> argparse.Namespace:
    """Read the command line.

    Args:
        arguments: Arguments after ``remi ship``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(prog="remi ship", description=__doc__.split("\n\n")[0])
    parser.add_argument("-m", "--message", action="append", required=True, help="commit message paragraph")
    parser.add_argument("--title", required=True, help="PR title: a plain sentence about the outcome")
    parser.add_argument(
        "--body-file", type=Path, default=None, help=f"PR body (Markdown); default: {DEFAULT_BODY_FILE} in the repo"
    )
    parser.add_argument("--base", default="", help="base branch (default: the repository's default branch)")
    return parser.parse_args(arguments)


def check_text(options: argparse.Namespace) -> str:
    """Refuse a bad commit header, Claude attribution or an unreadable body.

    Args:
        options: The parsed options.

    Returns:
        The PR body.
    """
    problems = header_problems(options.message[0].splitlines()[0])
    if problems:
        block("commit message isn't a Conventional Commit: " + "; ".join(problems) + ".")
    if is_secret_path(str(options.body_file)):
        block(f"the PR body can't come from '{options.body_file}': it looks like a secret file.")
    try:
        body = options.body_file.read_text()
    except OSError as error:
        block(f"can't read the PR body file: {error}")
    if not body.strip() or not options.title.strip():
        block("the PR title and body can't be empty.")
    if ATTRIBUTION.search("\n".join([*options.message, options.title, body])):
        block("Claude attribution is not allowed in commits or PRs; the user is the sole author.")
    return body


def commit(root: Path, payload: dict[str, str], messages: list[str], body_file: Path) -> str:
    """Stage and commit the change through the commit gate, if there is one.

    Args:
        root: Repository directory.
        payload: Session id and working directory, as the gates expect.
        messages: Commit message paragraphs.
        body_file: The PR body file, never committed when it lies inside the repository.

    Returns:
        A line for the report.
    """
    if not run(root, "git", "diff", "--cached", "--name-only").stdout.strip():
        must(root, "git", "add", "-A")
    if body_file.resolve().is_relative_to(root.resolve()):
        run(root, "git", "reset", "-q", "--", str(body_file.resolve().relative_to(root.resolve())))
    staged = run(root, "git", "diff", "--cached", "--name-only").stdout.split()
    if not staged:
        return "nothing new to commit"
    secrets = [path for path in staged if is_secret_path(path)]
    if secrets:
        block(f"secret files are staged ({', '.join(secrets)}). Unstage them and add them to .gitignore.")
    import quality

    quality.gate_staged_commit(payload)
    arguments = [part for message in messages for part in ("-m", message)]
    must(root, "git", "commit", "-q", *arguments)
    return f"committed {run(root, 'git', 'rev-parse', '--short', 'HEAD').stdout.strip()}: {messages[0]}"


def open_pull_request(root: Path, branch: str, options: argparse.Namespace) -> tuple[str, str]:
    """Push the branch and open its PR, or update the PR it already has.

    Args:
        root: Repository directory.
        branch: The feature branch.
        options: The parsed options.

    Returns:
        The PR number and URL.
    """
    listed = must(root, "gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,state,url")
    pull_requests = json.loads(listed or "[]")
    current = pull_requests[0] if pull_requests else None
    if current and current["state"] == "MERGED":
        block(
            f"PR #{current['number']} for '{branch}' is already merged, so a push would never reach the base. "
            "Switch to the updated base branch, branch again, cherry-pick the new commits and ship again."
        )
    if current and current["state"] == "CLOSED":
        block(f"PR #{current['number']} for '{branch}' was closed. Ask the user before reopening or replacing it.")
    must(root, "git", "push", "-q", "origin", "HEAD")
    if current:
        number = str(current["number"])
        must(root, "gh", "pr", "edit", number, "--title", options.title, "--body-file", str(options.body_file))
        return number, current["url"]
    base = (
        options.base
        or must(root, "gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name").strip()
    )
    url = must(
        root,
        "gh", "pr", "create", "--base", base, "--head", branch,
        "--title", options.title, "--body-file", str(options.body_file),
    ).strip().splitlines()[-1]  # fmt: skip
    return url.rstrip("/").rsplit("/", 1)[-1], url


def merge_status(payload: dict[str, str]) -> str:
    """Say whether the PR could be merged once the user asks.

    Args:
        payload: Session id and working directory, as the gates expect.

    Returns:
        A line for the report.
    """
    import merge_gate

    _head, reason = merge_gate.reviewed_head(payload)
    if reason:
        return f"left open, not mergeable yet: {reason}"
    return 'left open: the review approved this code. remi merges only when the user says "merge".'


def session_payload() -> dict[str, str]:
    """Build the gates' payload from the session environment.

    Returns:
        Session id and working directory.
    """
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session:
        block("remi ship and remi merge run inside a remi session: CLAUDE_CODE_SESSION_ID is not set.")
    return {"session_id": session, "cwd": os.getcwd()}


def feature_branch(root: Path) -> str:
    """Return the current branch, refusing a protected branch or a detached HEAD.

    Args:
        root: Repository directory.

    Returns:
        The feature branch.
    """
    branch = run(root, "git", "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if not branch or branch in PROTECTED_BRANCHES:
        block(f"ship from a feature branch, not '{branch or 'a detached HEAD'}': git switch -c <type>/<short-slug>")
    return branch


def merge_main(arguments: list[str]) -> int:
    """Merge the current branch's open PR, when the user asked and the review allows it.

    Args:
        arguments: Arguments after ``remi merge`` (none are accepted).

    Returns:
        Process exit code: 0 when merged, 2 when refused.
    """
    import merge_gate

    if arguments:
        block("remi merge takes no arguments: it merges the current branch's open PR.")
    payload = session_payload()
    root = project_dir(payload)
    branch = feature_branch(root)
    reason = merge_gate.merge_requested(payload)
    if reason:
        block(reason)
    head, reason = merge_gate.reviewed_head(payload)
    if reason:
        block(reason)
    listed = must(root, "gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,url")
    pull_requests = json.loads(listed or "[]")
    if not pull_requests:
        block(f"'{branch}' has no open PR. Run remi ship first.")
    number, url = str(pull_requests[0]["number"]), pull_requests[0]["url"]
    must(root, "gh", "pr", "merge", number, "--merge", "--delete-branch", "--match-head-commit", head)
    (root / DEFAULT_BODY_FILE).unlink(missing_ok=True)  # kept while the PR was open, for the next ship
    print(f"[remi merge] merged PR #{number} at {head[:7]}: {url}")
    return 0


def main(arguments: list[str]) -> int:
    """Ship the current branch.

    Args:
        arguments: Arguments after ``remi ship``.

    Returns:
        Process exit code: 0 when shipped (the PR is left open), 2 when refused.
    """
    if arguments[:1] == ["merge"]:
        return merge_main(arguments[1:])
    options = parse(arguments)
    payload = session_payload()
    root = project_dir(payload)
    branch = feature_branch(root)
    options.body_file = options.body_file or root / DEFAULT_BODY_FILE
    check_text(options)
    report = [f"branch {branch}", commit(root, payload, options.message, options.body_file)]
    number, url = open_pull_request(root, branch, options)
    report.append(f"PR #{number}: {url}")
    report.append(merge_status(payload))
    print("\n".join(f"[remi ship] {line}" for line in report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
