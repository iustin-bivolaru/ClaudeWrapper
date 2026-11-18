#!/usr/bin/env python3
"""Merge gate: remi merges its own PR only after an approving, matching review.

* ``record_review`` - called from ``quality.py worker`` when a reviewer stops. The
  code review's ``VERDICT`` is stored with a snapshot (path -> git blob id) of every
  non-ignored file the reviewer saw; the request check's ``REQUEST`` is stored next
  to it. A new reviewer starts a fresh record.
* PreToolUse on Bash - ``gh pr merge`` is allowed only when the latest reviewer said
  ``VERDICT: APPROVE`` and ``REQUEST: MATCHES``, every file in HEAD is exactly what it
  reviewed, and the merge is pinned to HEAD with ``--match-head-commit``. ``--admin``
  is never allowed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, block, is_secret_path, project_dir, read_payload, state_dir  # noqa: E402
from agent_policy import REVIEWERS_DIRECTORY, _slot_name  # noqa: E402

REVIEW_STATE_FILE = "review-state.json"
VERDICT_PATTERN = re.compile(r"VERDICT:\s*(APPROVE|CHANGES_REQUESTED)")
REQUEST_PATTERN = re.compile(r"REQUEST:\s*(MATCHES|GAPS)")
MERGE_COMMAND = re.compile(r"\bgh\s+pr\s+merge\b")
MAX_LISTED_PATHS = 5


def _git(root: Path, *args: str, stdin: str | None = None) -> str:
    """Run a read-only git command and return its stdout.

    Args:
        root: Repository directory.
        *args: Git arguments.
        stdin: Text to feed on stdin.

    Returns:
        The command's stdout.

    Raises:
        RuntimeError: If git fails.
    """
    result = subprocess.run(
        ["git", *args], cwd=root, input=stdin, capture_output=True, text=True, timeout=300, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout


def repository_root(payload: dict[str, Any]) -> Path:
    """Find the repository the session is working in.

    The current directory comes first: remi may run from a parent folder (for example
    to create a new project), where the launch directory is not the repository.

    Args:
        payload: Hook payload.

    Returns:
        The top level of the repository containing the payload's ``cwd``, else the project directory.
    """
    working_directory = payload.get("cwd")
    if working_directory:
        try:
            return Path(_git(Path(str(working_directory)), "rev-parse", "--show-toplevel").strip())
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass
    return project_dir(payload)


def _last_message(payload: dict[str, Any]) -> str:
    """Return the stopping subagent's final message.

    Args:
        payload: SubagentStop payload.

    Returns:
        ``last_assistant_message``, or the last assistant text in the agent's transcript, or "".
    """
    message = payload.get("last_assistant_message")
    if isinstance(message, str) and message:
        return message
    transcript = payload.get("agent_transcript_path")
    if not transcript:
        return ""
    last_text = ""
    try:
        lines = Path(str(transcript)).read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if entry.get("type") == "assistant" else None
        if isinstance(content, list):
            texts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
            if any(texts):
                last_text = "\n".join(texts)
    return last_text


def working_tree_snapshot(root: Path) -> tuple[dict[str, str], list[str]]:
    """Hash every non-ignored file in the working tree the way git would store it.

    Args:
        root: Repository directory.

    Returns:
        ``(path -> blob id, tracked paths)`` for files that exist; secret files are left out.
    """
    listed = [path for path in _git(root, "ls-files", "-co", "--exclude-standard", "-z").split("\0") if path]
    tracked = {path for path in _git(root, "ls-files", "-z").split("\0") if path}
    existing = sorted({path for path in listed if (root / path).is_file() and not is_secret_path(path)})
    if not existing:
        return {}, []
    blob_ids = _git(root, "hash-object", "--stdin-paths", stdin="\n".join(existing) + "\n").split()
    return dict(zip(existing, blob_ids, strict=True)), sorted(tracked.intersection(existing))


def head_tree(root: Path) -> dict[str, str]:
    """List the files committed at HEAD.

    Args:
        root: Repository directory.

    Returns:
        ``path -> blob id`` for every file in HEAD's tree.
    """
    files = {}
    for entry in _git(root, "ls-tree", "-r", "-z", "HEAD").split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        _, object_type, object_id = meta.split()
        if object_type == "blob":
            files[path] = object_id
    return files


def _load_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Load the recorded review state.

    Args:
        payload: Hook payload (scopes state to the session).

    Returns:
        The stored state, or an empty dict.
    """
    try:
        state = json.loads((state_dir(payload) / REVIEW_STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def latest_reviewer(payload: dict[str, Any]) -> str:
    """Return the id of the reviewer that started most recently.

    Args:
        payload: Hook payload (scopes state to the session).

    Returns:
        The newest reviewer id, or "" when no reviewer ran this session.
    """
    directory = state_dir(payload) / REVIEWERS_DIRECTORY
    if not directory.is_dir():
        return ""
    markers = sorted(directory.iterdir(), key=lambda marker: marker.stat().st_mtime)
    return markers[-1].name if markers else ""


def record_review(payload: dict[str, Any]) -> None:
    """Store a stopping reviewer's verdict, request result and reviewed snapshot.

    Args:
        payload: SubagentStop payload for a reviewer.
    """
    reviewer = _slot_name(payload)
    message = _last_message(payload)
    state = _load_state(payload)
    if state.get("reviewer") != reviewer:
        state = {"reviewer": reviewer}
    verdict = VERDICT_PATTERN.search(message)
    if verdict:
        state["verdict"] = verdict.group(1)
        try:
            state["snapshot"], state["tracked"] = working_tree_snapshot(repository_root(payload))
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
            state["snapshot"], state["tracked"] = None, []
    request = REQUEST_PATTERN.search(message)
    if request:
        state["request"] = request.group(1)
    (state_dir(payload) / REVIEW_STATE_FILE).write_text(json.dumps(state))


def unreviewed_paths(snapshot: dict[str, str], tracked: list[str], committed: dict[str, str]) -> list[str]:
    """Compare what HEAD holds with what the reviewer saw.

    Args:
        snapshot: ``path -> blob id`` of the reviewed working tree.
        tracked: Paths that were tracked when the review ran.
        committed: ``path -> blob id`` at HEAD.

    Returns:
        Paths that changed, appeared or disappeared after the review.
    """
    changed = [path for path, blob_id in committed.items() if snapshot.get(path) != blob_id]
    removed = [path for path in tracked if path not in committed]
    return sorted(changed + removed)


def check_merge(payload: dict[str, Any]) -> int:
    """Gate ``gh pr merge`` on the latest review.

    Args:
        payload: PreToolUse payload for a Bash call.

    Returns:
        Exit code (blocking exits with 2 inside ``block``).
    """
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not MERGE_COMMAND.search(command):
        return ALLOW
    if re.search(r"\s--admin\b", command):
        block("`gh pr merge --admin` bypasses branch protection. remi never uses it.")
    state = _load_state(payload)
    reviewer = latest_reviewer(payload)
    if not reviewer or state.get("reviewer") != reviewer:
        block(
            "no finished review covers this change, so remi doesn't merge it. Merging needs the latest reviewer's "
            "VERDICT: APPROVE and REQUEST: MATCHES. Leave the PR open for the user."
        )
    if state.get("verdict") != "APPROVE" or state.get("request") != "MATCHES":
        block(
            f"the latest review said VERDICT: {state.get('verdict', 'none')}, REQUEST: {state.get('request', 'none')}. "
            "remi merges only after VERDICT: APPROVE and REQUEST: MATCHES. Leave the PR open and tell the user why."
        )
    root = repository_root(payload)
    try:
        head = _git(root, "rev-parse", "HEAD").strip()
        committed = head_tree(root)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        block(f"can't read HEAD to check it against the review: {error}")
    pinned = re.search(r"--match-head-commit[=\s]+([0-9a-f]{40})\b", command)
    if not pinned or pinned.group(1) != head:
        block(
            f"pin the merge to the reviewed commit: add `--match-head-commit {head}`, so GitHub refuses the merge "
            "if the PR head differs from what was reviewed and committed here."
        )
    snapshot = state.get("snapshot")
    if not isinstance(snapshot, dict):
        block("the review didn't record which files it saw, so the merge can't be checked. Leave the PR open.")
    unreviewed = unreviewed_paths(snapshot, state.get("tracked") or [], committed)
    if unreviewed:
        listed = ", ".join(unreviewed[:MAX_LISTED_PATHS]) + (" …" if len(unreviewed) > MAX_LISTED_PATHS else "")
        block(
            f"HEAD differs from what the reviewer approved ({listed}). Changes after an approval need a new review "
            "(both steps) before remi merges."
        )
    return ALLOW


def main() -> int:
    """Run the merge gate on a PreToolUse Bash payload.

    Returns:
        Process exit code.
    """
    try:
        payload = read_payload()
    except ValueError:
        block("malformed hook payload")
    return check_merge(payload)


if __name__ == "__main__":
    sys.exit(main())
