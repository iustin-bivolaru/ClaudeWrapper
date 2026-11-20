"""Merge gate: remi merges a PR only when the user asks, and only after an approving, matching review.

* ``record_review`` - called from ``quality.py worker`` when a reviewer stops. The
  code review's ``VERDICT`` is stored with a snapshot (path -> git blob id) of every
  non-ignored file the reviewer saw; the request check's ``REQUEST`` is stored next
  to it. A new reviewer starts a fresh record.
* ``record_handback`` - called on UserPromptSubmit: the same report reaches the main
  session as a hand-back prompt, which catches a request check that SubagentStop missed.
* PreToolUse on Bash - ``gh pr merge`` is allowed only when the user's latest message
  asks for a merge (``merge_requested``), the latest reviewer said
  ``VERDICT: APPROVE`` and ``REQUEST: MATCHES`` (or the user said "merge anyway",
  which overrules the verdict but nothing else), every file in HEAD is exactly what it
  reviewed, and the merge is pinned to HEAD with ``--match-head-commit``. A file
  that matches the base branch came from it (a rebase onto or merge of the base) and
  needs no new review. ``--admin`` is never allowed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import ALLOW, block, is_secret_path, project_dir, state_dir  # noqa: E402
from agent_policy import REVIEWERS_DIRECTORY, _slot_name  # noqa: E402

REVIEW_STATE_FILE = "review-state.json"
VERDICT_PATTERN = re.compile(r"VERDICT:\s*(APPROVE|CHANGES_REQUESTED)")
REQUEST_PATTERN = re.compile(r"REQUEST:\s*(MATCHES|GAPS)")
HANDBACK_TOOL = "SubagentHandback"
HANDBACK_SENDER = re.compile(r'\A(?:Another Claude session sent a message:\s*)?<agent-message from="([\w-]+)"')
MERGE_COMMAND = re.compile(r"\bgh\s+pr\s+merge\b")
MERGE_WORD = re.compile(r"(?i)\bmerge\b")
MERGE_OVERRULE = re.compile(r"(?i)\banyways?\b")
MERGE_REFUSED = re.compile(r"(?i)\b(?:don[’']?t|do\s+not|never|not|no|without)\s+(?:\w+\s+){0,2}merg")
MAX_LISTED_PATHS = 5
BASE_REFS = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


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


def _last_message(payload: dict[str, Any]) -> str:
    """Return the stopping subagent's final message.

    Args:
        payload: SubagentStop payload.

    A subagent may hand its report back through a ``SubagentHandback`` tool call instead of a
    final text message, so the transcript's last report is read too: the last assistant text or
    handback message, whichever came later.

    Returns:
        ``last_assistant_message`` and the last report in the agent's transcript, joined; or "".
    """
    message = payload.get("last_assistant_message")
    message = message if isinstance(message, str) else ""
    transcript = payload.get("agent_transcript_path")
    if not transcript:
        return message
    last_report = ""
    try:
        lines = Path(str(transcript)).read_text().splitlines()
    except OSError:
        return message
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if entry.get("type") == "assistant" else None
        if not isinstance(content, list):
            continue
        reports = [_report_text(part) for part in content if isinstance(part, dict)]
        if any(reports):
            last_report = "\n".join(report for report in reports if report)
    return "\n".join(text for text in (message, last_report) if text)


def _report_text(part: dict[str, Any]) -> str:
    """Return the report text carried by one assistant content part.

    Args:
        part: A content part from an assistant transcript entry.

    Returns:
        The text of a text part or of a ``SubagentHandback`` call, else "".
    """
    if part.get("type") == "text":
        return str(part.get("text") or "")
    if part.get("type") == "tool_use" and part.get("name") == HANDBACK_TOOL:
        return str((part.get("input") or {}).get("message") or "")
    return ""


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


def head_tree(root: Path, ref: str = "HEAD") -> dict[str, str]:
    """List the files committed at a ref.

    Args:
        root: Repository directory.
        ref: The commit to list.

    Returns:
        ``path -> blob id`` for every file in the ref's tree.
    """
    files = {}
    for entry in _git(root, "ls-tree", "-r", "-z", ref).split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        _, object_type, object_id = meta.split()
        if object_type == "blob":
            files[path] = object_id
    return files


def base_tree(root: Path) -> dict[str, str]:
    """List the files of the base branch, so files a rebase or merge brought in aren't treated as unreviewed.

    Args:
        root: Repository directory.

    Returns:
        ``path -> blob id`` of the first base ref that exists, or an empty dict.
    """
    for ref in BASE_REFS:
        try:
            _git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
            return head_tree(root, ref)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            continue
    return {}


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
    _record(payload, _slot_name(payload), _last_message(payload), refresh_snapshot=True)


def record_handback(payload: dict[str, Any]) -> None:
    """Store the verdict or request result from a reviewer's hand-back to the main session.

    A reviewer resumed by SendMessage can stop before its last ``SubagentHandback`` reaches
    its transcript, so SubagentStop may miss the request check. The same report also reaches
    the main session as a prompt (``<agent-message from="<id>">``); it counts only for the
    latest reviewer, and it never replaces a verdict's snapshot that the stop already took.

    Args:
        payload: UserPromptSubmit payload.
    """
    sender = HANDBACK_SENDER.match(str(payload.get("prompt") or ""))
    if not sender or sender.group(1) != latest_reviewer(payload):
        return
    _record(payload, sender.group(1), str(payload.get("prompt")), refresh_snapshot=False)


def _record(payload: dict[str, Any], reviewer: str, message: str, *, refresh_snapshot: bool) -> None:
    """Store the verdict and request result found in one reviewer report.

    Args:
        payload: Hook payload (scopes state and finds the repository).
        reviewer: The reviewer's agent id.
        message: The report text.
        refresh_snapshot: Retake the snapshot even when this reviewer's verdict is already recorded.
    """
    state = _load_state(payload)
    if state.get("reviewer") != reviewer:
        state = {"reviewer": reviewer}
    verdict = VERDICT_PATTERN.search(message)
    if verdict and (refresh_snapshot or "verdict" not in state):
        state["verdict"] = verdict.group(1)
        try:
            state["snapshot"], state["tracked"] = working_tree_snapshot(project_dir(payload))
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
            state["snapshot"], state["tracked"] = None, []
    request = REQUEST_PATTERN.search(message)
    if request:
        state["request"] = request.group(1)
    (state_dir(payload) / REVIEW_STATE_FILE).write_text(json.dumps(state))


def unreviewed_paths(
    snapshot: dict[str, str], tracked: list[str], committed: dict[str, str], base: dict[str, str] | None = None
) -> list[str]:
    """Compare what HEAD holds with what the reviewer saw.

    Args:
        snapshot: ``path -> blob id`` of the reviewed working tree.
        tracked: Paths that were tracked when the review ran.
        committed: ``path -> blob id`` at HEAD.
        base: ``path -> blob id`` of the base branch. A path that matches it came from the base.

    Returns:
        Paths that changed, appeared or disappeared after the review and don't match the base.
    """
    base = base or {}
    changed = [path for path, blob_id in committed.items() if snapshot.get(path) != blob_id]
    removed = [path for path in tracked if path not in committed]
    return sorted(path for path in changed + removed if not base or base.get(path) != committed.get(path))


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
    head, reason = may_merge(payload)
    if reason:
        block(reason)
    pinned = re.search(r"--match-head-commit[=\s]+([0-9a-f]{40})\b", command)
    if not pinned or pinned.group(1) != head:
        block(
            f"pin the merge to the reviewed commit: add `--match-head-commit {head}`, so GitHub refuses the merge "
            "if the PR head differs from what was reviewed and committed here."
        )
    return ALLOW


def reviewed_head(payload: dict[str, Any], *, overrule: bool = False) -> tuple[str, str]:
    """Check HEAD against the latest review.

    Shared by the ``gh pr merge`` hook, ``remi ship`` (to report) and ``remi merge``.

    Args:
        payload: Payload with the session id and the working directory.
        overrule: The user said "merge anyway": accept any verdict, but still only the reviewed code.

    Returns:
        ``(full HEAD sha, "")`` when the merge may go ahead, else ``("", reason)``.
    """
    state = _load_state(payload)
    reviewer = latest_reviewer(payload)
    if not reviewer or state.get("reviewer") != reviewer:
        return "", (
            "no finished review covers this change, so remi doesn't merge it. Merging needs the latest "
            "reviewer's VERDICT: APPROVE and REQUEST: MATCHES. Leave the PR open for the user."
        )
    if not overrule and (state.get("verdict") != "APPROVE" or state.get("request") != "MATCHES"):
        return "", (
            f"the latest review said VERDICT: {state.get('verdict', 'none')}, "
            f"REQUEST: {state.get('request', 'none')}. remi merges only after VERDICT: APPROVE and REQUEST: MATCHES, "
            'unless the user says "merge anyway". Leave the PR open and tell the user why.'
        )
    root = project_dir(payload)
    try:
        head = _git(root, "rev-parse", "HEAD").strip()
        committed = head_tree(root)
        base = base_tree(root)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return "", f"can't read HEAD to check it against the review: {error}"
    snapshot = state.get("snapshot")
    if not isinstance(snapshot, dict):
        return "", "the review didn't record which files it saw, so the merge can't be checked. Leave the PR open."
    unreviewed = unreviewed_paths(snapshot, state.get("tracked") or [], committed, base)
    if unreviewed:
        listed = ", ".join(unreviewed[:MAX_LISTED_PATHS]) + (" …" if len(unreviewed) > MAX_LISTED_PATHS else "")
        return "", (
            f"HEAD differs from what the reviewer approved ({listed}). Changes after an approval need a new "
            "review (both steps) before remi merges."
        )
    return head, ""


def merge_requested(payload: dict[str, Any]) -> tuple[bool, str]:
    """Check that the user's latest message asks for a merge.

    remi never merges on its own: the user's most recent message (recorded by the
    prompt hook, hand-backs excluded) must say "merge", and not "don't merge".
    "merge anyway" also overrules a review that didn't approve.

    Args:
        payload: Payload with the session id.

    Returns:
        ``(overrule, "")`` when the user asked for the merge, else ``(False, reason)``.
    """
    from agent_policy import _recorded_requests

    recorded = _recorded_requests(payload)
    latest = recorded[-1] if recorded else ""
    if MERGE_WORD.search(latest) and not MERGE_REFUSED.search(latest):
        return bool(MERGE_OVERRULE.search(latest)), ""
    return False, (
        "remi merges only when the user's latest message asks for it, and it doesn't. Leave the PR open "
        'and tell the user to say "merge" when they want it merged.'
    )


def may_merge(payload: dict[str, Any]) -> tuple[str, str]:
    """Check the user's request and the review before a merge.

    Args:
        payload: Payload with the session id and the working directory.

    Returns:
        ``(full HEAD sha, "")`` when the merge may go ahead, else ``("", reason)``.
    """
    overrule, reason = merge_requested(payload)
    if reason:
        return "", reason
    return reviewed_head(payload, overrule=overrule)
