import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "scripts"


def run_hook(script: str, payload: dict, *args: str, cwd: Path | None = None, env: dict | None = None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("x\n")
    git(tmp_path, "add", "a.txt")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def tool(name: str, cwd: Path | None = None, **tool_input) -> dict:
    return {"tool_name": name, "tool_input": tool_input, "cwd": str(cwd or "/tmp"), "session_id": "test"}


# --- secret guard -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "app/.env",
        ".env.local",
        ".env.production",
        "/x/.envrc",
        "certs/server.pem",
        "/home/u/.ssh/id_ed25519",
        "/home/u/.aws/credentials",
        "config/secrets.yaml",
        "prod.tfstate",
    ],
)
def test_read_secret_blocked(path):
    result = run_hook("guard.py", tool("Read", file_path=path))
    assert result.returncode == 2, path
    assert "BLOCKED" in result.stderr


@pytest.mark.parametrize("path", [".env.example", "app/.env.sample", "src/settings.py", "envs/dev.py", "README.md"])
def test_read_normal_allowed(path):
    assert run_hook("guard.py", tool("Read", file_path=path)).returncode == 0


def test_write_env_blocked_but_example_allowed():
    assert run_hook("guard.py", tool("Write", file_path=".env", content="X=1")).returncode == 2
    assert run_hook("guard.py", tool("Edit", file_path=".env.example")).returncode == 0


def test_grep_glob_on_secrets_blocked():
    assert run_hook("guard.py", tool("Grep", pattern="PASS", path=".env")).returncode == 2
    assert run_hook("guard.py", tool("Grep", pattern="PASS", glob="**/.env*")).returncode == 2
    assert run_hook("guard.py", tool("Glob", pattern="**/*.pem")).returncode == 2
    assert run_hook("guard.py", tool("Grep", pattern="def ", path="src")).returncode == 0


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "cat .env|wc -l",
        "source .env && echo",
        "cp .env /tmp/x",
        "docker run --env-file=.env img",
        "printenv",
        "env",
        "env | grep KEY",
        "export -p",
        "cat /proc/self/environ",
        "python -c 'import os; print(os.environ)'",
        "git add .env",
        "less ~/.aws/credentials",
        "security find-generic-password -s x",
        "head -1 deploy/key.pem",
        "sudo ls",
    ],
)
def test_bash_secret_access_blocked(command):
    assert run_hook("guard.py", tool("Bash", command=command)).returncode == 2, command


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "uv run pytest -q",
        "cat .env.example",
        "env FOO=1 python x.py",
        "grep -rn os.environ src/",
        "ruff check .",
        "git status --short",
    ],
)
def test_bash_normal_allowed(command, repo):
    assert run_hook("guard.py", tool("Bash", cwd=repo, command=command)).returncode == 0, command


def test_malformed_payload_fails_closed():
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "guard.py")], input="not json", capture_output=True, text=True, check=False
    )
    assert result.returncode == 2


# --- git policy -------------------------------------------------------------


def test_commit_on_main_blocked(repo):
    assert run_hook("guard.py", tool("Bash", cwd=repo, command="git commit -m x")).returncode == 2


def test_commit_after_branch_switch_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    result = run_hook("guard.py", tool("Bash", cwd=repo, command="git commit -m 'feat: add x'"))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push origin HEAD:master",
        "git push --force origin feat/x",
        "git push -f",
        "git commit --no-verify -m x",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "gh repo create acme/x --public --source . --push",
        "gh repo edit acme/x --visibility public",
    ],
)
def test_dangerous_git_blocked(repo, command):
    git(repo, "switch", "-qc", "feat/x")
    assert run_hook("guard.py", tool("Bash", cwd=repo, command=command)).returncode == 2, command


def test_push_feature_branch_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    for command in ("git push -u origin HEAD", "git push --force-with-lease origin feat/x"):
        assert run_hook("guard.py", tool("Bash", cwd=repo, command=command)).returncode == 0, command


def test_private_repo_create_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    for command in ("gh repo create acme/x --private --source . --push", "gh repo create acme/x --internal"):
        assert run_hook("guard.py", tool("Bash", cwd=repo, command=command)).returncode == 0, command


# --- commit gate --------------------------------------------------------------


def test_commit_gate_requires_separate_staging(repo):
    git(repo, "switch", "-qc", "feat/x")
    result = run_hook("quality.py", tool("Bash", cwd=repo, command="git add -A && git commit -m x"), "commit")
    assert result.returncode == 2


@pytest.mark.parametrize(
    "content",
    [
        'API_KEY = "abcd1234efgh5678"\n',
        "aws = 'AKIAABCDEFGHIJKLMNOP'\n",
        "card = '4111 1111 1111 1111'\n",
        "url = 'postgresql://user:hunter22@db:5432/x'\n",
        "iban = 'RO49AAAA1B31007593840000'\n",
    ],
)
def test_commit_gate_finds_secrets(repo, content):
    git(repo, "switch", "-qc", "feat/x")
    (repo / "leak.txt").write_text(content)
    git(repo, "add", "leak.txt")
    result = run_hook("quality.py", tool("Bash", cwd=repo, command="git commit -m x"), "commit")
    assert result.returncode == 2, content
    assert "secrets" in result.stderr


def test_commit_gate_allow_marker_and_clean_commit(repo):
    git(repo, "switch", "-qc", "feat/x")
    (repo / "fixture.txt").write_text("card = '4111111111111111'  # remi: allow-secret\nplain text\n")
    git(repo, "add", "fixture.txt")
    result = run_hook("quality.py", tool("Bash", cwd=repo, command="git commit -m x"), "commit")
    assert result.returncode == 0, result.stderr


def test_commit_gate_runs_project_override(repo):
    git(repo, "switch", "-qc", "feat/x")
    (repo / "pyproject.toml").write_text('[tool.remi]\ntest = "exit 3"\n')
    git(repo, "add", "pyproject.toml")
    result = run_hook("quality.py", tool("Bash", cwd=repo, command="git commit -m x"), "commit")
    assert result.returncode == 2
    assert "test failed" in result.stderr


# --- agent policy -------------------------------------------------------------


def test_general_purpose_agent_redirected(tmp_path):
    payload = tool("Agent", subagent_type="general-purpose", prompt="x")
    payload["session_id"] = tmp_path.name
    result = run_hook("agent_policy.py", payload, "pre")
    assert result.returncode == 2
    assert "remi:worker" in result.stderr


def worker_event(session: str, agent_id: str, event: str) -> dict:
    return {"session_id": session, "agent_type": "remi:worker", "agent_id": agent_id, "hook_event_name": event}


def test_worker_cap(tmp_path):
    session = f"cap-{tmp_path.name}"
    pre = tool("Agent", subagent_type="remi:worker", prompt="x")
    pre["session_id"] = session
    env = {"TMPDIR": str(tmp_path), "REMI_MAX_WORKERS": "2", "PATH": "/usr/bin:/bin"}
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 0
    assert (
        run_hook("agent_policy.py", worker_event(session, "agent-one", "SubagentStart"), "start", env=env).returncode
        == 0
    )
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 0  # pending, not started yet
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 2  # 1 active + 1 pending = limit
    run_hook("agent_policy.py", worker_event(session, "agent-two", "SubagentStart"), "start", env=env)
    run_hook("agent_policy.py", worker_event(session, "agent-one", "SubagentStop"), "stop", env=env)
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 0


def test_resumed_worker_does_not_free_other_slots(tmp_path):
    session = f"resume-{tmp_path.name}"
    pre = tool("Agent", subagent_type="remi:worker", prompt="x")
    pre["session_id"] = session
    env = {"TMPDIR": str(tmp_path), "REMI_MAX_WORKERS": "2", "PATH": "/usr/bin:/bin"}
    for agent_id in ("agent-one", "agent-two"):
        run_hook("agent_policy.py", pre, "pre", env=env)
        run_hook("agent_policy.py", worker_event(session, agent_id, "SubagentStart"), "start", env=env)
    # agent-one finishes, then is resumed with SendMessage for review fixes (no PreToolUse)
    run_hook("agent_policy.py", worker_event(session, "agent-one", "SubagentStop"), "stop", env=env)
    run_hook("agent_policy.py", worker_event(session, "agent-one", "SubagentStart"), "start", env=env)
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 2  # still 2 running
    run_hook("agent_policy.py", worker_event(session, "agent-one", "SubagentStop"), "stop", env=env)
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 0


def test_explore_and_reviewer_allowed(tmp_path):
    for agent in ("remi:explorer", "remi:reviewer"):
        payload = tool("Agent", subagent_type=agent, prompt="x")
        payload["session_id"] = tmp_path.name
        assert run_hook("agent_policy.py", payload, "pre").returncode == 0


# --- reviewer: blind code review, then the verbatim request ---------------------------

REQUEST = "Reject transfers over the daily limit,\nexcept for internal accounts."
BASE_LINE = "Review the changes against `main`."
CHECK_LINE = "Check the change against the user's request."


def review_env(tmp_path: Path) -> dict:
    return {"REMI_STATE_DIR": str(tmp_path / "state"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}


def reviewer_launch(prompt: str, session: str, env: dict):
    payload = tool("Agent", subagent_type="remi:reviewer", prompt=prompt)
    payload["session_id"] = session
    return run_hook("agent_policy.py", payload, "pre", env=env)


def record_prompt(message: str, session: str, env: dict) -> None:
    payload = {"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": message}
    assert run_hook("agent_policy.py", payload, "prompt", env=env).returncode == 0


def start_reviewer(session: str, agent_id: str, env: dict) -> None:
    payload = {"session_id": session, "agent_type": "remi:reviewer", "agent_id": agent_id}
    assert run_hook("agent_policy.py", payload, "start", env=env).returncode == 0


def send_message(to: str, message: str, session: str, env: dict):
    payload = tool("SendMessage", to=to, message=message)
    payload["session_id"] = session
    return run_hook("agent_policy.py", payload, "message", env=env)


def commit_payload(repo: Path, session: str) -> dict:
    payload = tool("Bash", cwd=repo, command="git commit -m x")
    payload["session_id"] = session
    return payload


def test_blind_reviewer_launch_allowed(tmp_path):
    env = review_env(tmp_path)
    record_prompt(REQUEST, "blind", env)
    result = reviewer_launch(BASE_LINE, "blind", env)
    assert result.returncode == 0, result.stderr


def test_reviewer_launch_with_request_blocked(tmp_path):
    env = review_env(tmp_path)
    record_prompt(REQUEST, "early", env)
    result = reviewer_launch(f"{BASE_LINE}\n<request>{REQUEST}</request>", "early", env)
    assert result.returncode == 2
    assert "blind" in result.stderr


@pytest.mark.parametrize(
    "prompt",
    [
        f"{BASE_LINE}\nThe worker added a daily limit in transfers.py.",
        f"{BASE_LINE} " + "Context: " * 40,
    ],
)
def test_reviewer_launch_with_context_blocked(tmp_path, prompt):
    result = reviewer_launch(prompt, "context", review_env(tmp_path))
    assert result.returncode == 2
    assert "no context" in result.stderr


def test_request_check_with_verbatim_quote_allowed(tmp_path):
    env = review_env(tmp_path)
    record_prompt(REQUEST, "verbatim", env)
    record_prompt("go", "verbatim", env)
    start_reviewer("verbatim", "reviewer-one", env)
    rewrapped = "Reject transfers over the daily limit, except\n  for internal accounts."
    message = f"{CHECK_LINE}\n\n<request>\n{rewrapped}\n</request>\n<request>go</request>"
    result = send_message("reviewer-one", message, "verbatim", env)
    assert result.returncode == 0, result.stderr


def test_request_check_with_paraphrase_blocked(tmp_path):
    env = review_env(tmp_path)
    record_prompt(REQUEST, "paraphrase", env)
    start_reviewer("paraphrase", "reviewer-one", env)
    result = send_message("reviewer-one", f"{CHECK_LINE}\n<request>Add a daily limit.</request>", "paraphrase", env)
    assert result.returncode == 2
    assert "verbatim" in result.stderr


def test_request_check_with_context_blocked(tmp_path):
    env = review_env(tmp_path)
    record_prompt(REQUEST, "reasons", env)
    start_reviewer("reasons", "reviewer-one", env)
    message = f"{CHECK_LINE}\n<request>{REQUEST}</request>\nWe read 'daily' as per transaction."
    result = send_message("reviewer-one", message, "reasons", env)
    assert result.returncode == 2
    assert "no context" in result.stderr


def test_messages_to_workers_not_checked(tmp_path):
    env = review_env(tmp_path)
    record_prompt(REQUEST, "worker-msg", env)
    result = send_message("worker-one", "Fix these findings:\n- a.py:3\n<request>made up</request>", "worker-msg", env)
    assert result.returncode == 0, result.stderr


def test_commit_refused_until_request_check_sent(repo, tmp_path):
    env = review_env(tmp_path)
    git(repo, "switch", "-qc", "feat/x")
    (repo / "notes.txt").write_text("plain text\n")
    git(repo, "add", "notes.txt")
    record_prompt(REQUEST, "gate", env)
    start_reviewer("gate", "reviewer-one", env)
    result = run_hook("quality.py", commit_payload(repo, "gate"), "commit", env=env)
    assert result.returncode == 2
    assert "reviewer-one" in result.stderr
    start_reviewer("gate", "reviewer-one", env)  # resumed by SendMessage: not a new review
    assert send_message("reviewer-one", f"{CHECK_LINE}\n<request>{REQUEST}</request>", "gate", env).returncode == 0
    result = run_hook("quality.py", commit_payload(repo, "gate"), "commit", env=env)
    assert result.returncode == 0, result.stderr
    start_reviewer("gate", "reviewer-two", env)  # second review: due again
    assert run_hook("quality.py", commit_payload(repo, "gate"), "commit", env=env).returncode == 2


def test_commit_allowed_without_review_or_user_messages(repo, tmp_path):
    env = review_env(tmp_path)
    git(repo, "switch", "-qc", "feat/x")
    (repo / "notes.txt").write_text("plain text\n")
    git(repo, "add", "notes.txt")
    start_reviewer("silent", "reviewer-one", env)  # no recorded user message: nothing to check against
    result = run_hook("quality.py", commit_payload(repo, "silent"), "commit", env=env)
    assert result.returncode == 0, result.stderr


# --- attribution ----------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "feat: x" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"',
        "gh pr create --title x --body 'Generated with [Claude Code](https://claude.com/claude-code)'",
    ],
)
def test_claude_attribution_blocked(repo, command):
    git(repo, "switch", "-qc", "feat/x")
    result = run_hook("guard.py", tool("Bash", cwd=repo, command=command))
    assert result.returncode == 2
    assert "attribution" in result.stderr


def test_plain_pr_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    command = 'gh pr create --title "feat: add fx rates" --body "## What\n- rates"'
    assert run_hook("guard.py", tool("Bash", cwd=repo, command=command)).returncode == 0


# --- naming -----------------------------------------------------------------------

BAD_NAMES_SOURCE = """
import numpy as n


def f(x):
    total = 0
    for i in range(x):
        total += i
    try:
        pass
    except ValueError as e:
        raise
    return [v for v in range(3)], lambda q: q
"""

GOOD_NAMES_SOURCE = """
from typing import TypeVar

T = TypeVar("T")


def sum_attempts(max_attempts: int) -> int:
    total_attempts = 0
    for attempt_number in range(max_attempts):
        total_attempts += attempt_number
    for _ in range(2):
        pass
    return total_attempts
"""


def test_naming_flags_single_letters(tmp_path):
    source_file = tmp_path / "bad.py"
    source_file.write_text(BAD_NAMES_SOURCE)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "naming.py"), str(source_file)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    for name in ("'n'", "'f'", "'x'", "'i'", "'e'", "'v'", "'q'"):
        assert name in result.stdout, name


def test_naming_accepts_descriptive_names(tmp_path):
    source_file = tmp_path / "good.py"
    source_file.write_text(GOOD_NAMES_SOURCE)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "naming.py"), str(source_file)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout


def test_edit_hook_reports_single_letters(tmp_path):
    source_file = tmp_path / "service.py"
    source_file.write_text('def compute(x: int) -> int:\n    """Double it."""\n    return x * 2\n')
    payload = tool("Edit", cwd=tmp_path, file_path=str(source_file))
    result = run_hook("quality.py", payload, "edit", cwd=tmp_path)
    assert result.returncode == 2
    assert "'x'" in result.stderr


def test_commit_gate_blocks_single_letters(repo):
    git(repo, "switch", "-qc", "feat/x")
    (repo / "module.py").write_text("for i in range(3):\n    print(i)\n")
    git(repo, "add", "module.py")
    result = run_hook("quality.py", tool("Bash", cwd=repo, command="git commit -m x"), "commit")
    assert result.returncode == 2
    assert "single-letter variable 'i'" in result.stderr


def test_project_defined_agents_blocked(tmp_path):
    for agent in ("layering-reviewer", "migration-reviewer", "agent-prompt-reviewer"):
        payload = tool("Agent", subagent_type=agent, prompt="x")
        payload["session_id"] = tmp_path.name
        result = run_hook("agent_policy.py", payload, "pre")
        assert result.returncode == 2, agent
        assert "not part of remi" in result.stderr


# --- pipeline: worker stop and pre-review gates ------------------------------------


def pipeline_env(tmp_path: Path) -> dict:
    return {"TMPDIR": str(tmp_path / "state"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}


def start_worker(session: str, agent_id: str, env: dict) -> None:
    pre = tool("Agent", subagent_type="remi:worker", prompt="x")
    pre["session_id"] = session
    run_hook("agent_policy.py", pre, "pre", env=env)
    run_hook("agent_policy.py", worker_event(session, agent_id, "SubagentStart"), "start", env=env)


def worker_stop(repo: Path, session: str, agent_id: str) -> dict:
    payload = worker_event(session, agent_id, "SubagentStop")
    payload["cwd"] = str(repo)
    return payload


def test_worker_gate_defers_tests_while_others_run(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.remi]\ntest = "exit 3"\n')
    (repo / "tests").mkdir()
    start_worker("deferred", "agent-one", env)
    start_worker("deferred", "agent-two", env)
    result = run_hook("quality.py", worker_stop(repo, "deferred", "agent-one"), "worker", env=env)
    assert result.returncode == 0, result.stderr  # agent-two still running: tests wait for the review gate


def test_last_worker_runs_tests(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.remi]\ntest = "exit 3"\n')
    (repo / "tests").mkdir()
    start_worker("last", "agent-one", env)
    result = run_hook("quality.py", worker_stop(repo, "last", "agent-one"), "worker", env=env)
    assert result.returncode == 2
    assert "Tests are red" in result.stderr


def test_review_refused_while_workers_run(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    start_worker("busy", "agent-one", env)
    payload = tool("Agent", cwd=repo, subagent_type="remi:reviewer", prompt="Review main...feat/x")
    payload["session_id"] = "busy"
    result = run_hook("quality.py", payload, "review", env=env)
    assert result.returncode == 2
    assert "still running" in result.stderr


def test_review_runs_fix_then_gate(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.remi]\nfix = "touch fixed.marker"\ntest = "test -f fixed.marker"\n')
    (repo / "tests").mkdir()
    payload = tool("Agent", cwd=repo, subagent_type="remi:reviewer", prompt="Review main...feat/x")
    payload["session_id"] = "green"
    result = run_hook("quality.py", payload, "review", env=env)
    assert result.returncode == 0, result.stderr
    assert (repo / "fixed.marker").exists()


def test_review_refused_when_tests_red(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.remi]\nfix = "true"\ntest = "exit 3"\n')
    (repo / "tests").mkdir()
    payload = tool("Agent", cwd=repo, subagent_type="remi:reviewer", prompt="Review main...feat/x")
    payload["session_id"] = "red"
    result = run_hook("quality.py", payload, "review", env=env)
    assert result.returncode == 2
    assert "review is refused" in result.stderr


def test_default_worker_cap_is_three(tmp_path):
    session = f"default-cap-{tmp_path.name}"
    pre = tool("Agent", subagent_type="remi:worker", prompt="x")
    pre["session_id"] = session
    env = {"TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    for _ in range(3):
        assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 0
    result = run_hook("agent_policy.py", pre, "pre", env=env)
    assert result.returncode == 2
    assert "limit 3" in result.stderr


# --- conventional commits ---------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'feat: add compound interest'",
        "git commit -m 'fix(payments): reject negative amounts' -m 'Amounts below zero were booked as credits.'",
        "git commit -m 'feat(api)!: drop the v1 statements endpoint'",
        "git commit --message='docs: describe the outbox retry policy'",
        "git commit -m 'chore(deps): bump httpx to 0.28'",
        "git commit -m 'refactor: rename IBAN validator'",
        "git commit --amend --no-edit",
        """git commit -m "$(cat <<'EOF'\nfeat: add fx rates\n\nWhy it matters.\nEOF\n)\"""",
    ],
)
def test_conventional_commit_allowed(repo, command):
    git(repo, "switch", "-qc", "feat/x")
    result = run_hook("guard.py", tool("Bash", cwd=repo, command=command))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("git commit -m 'add compound interest'", "isn't '<type>"),
        ("git commit -m 'feature: add compound interest'", "must be one of"),
        ("git commit -m 'Feat: add compound interest'", "isn't '<type>"),
        ("git commit -m 'feat:add compound interest'", "isn't '<type>"),
        ("git commit -m 'feat(Payments): add transfers'", "isn't '<type>"),
        ("git commit -m 'fix: reject negative amounts.'", "period"),
        ("git commit -m 'fix: Rejected negative amounts'", "lowercase"),
        ("git commit -m 'feat: " + "x" * 120 + "'", "characters"),
        ("git commit", "pass the commit message with -m"),
        ("git commit -F message.txt", "pass the commit message with -m"),
    ],
)
def test_non_conventional_commit_blocked(repo, command, reason):
    git(repo, "switch", "-qc", "feat/x")
    result = run_hook("guard.py", tool("Bash", cwd=repo, command=command))
    assert result.returncode == 2, command
    assert reason in result.stderr, result.stderr


@pytest.mark.parametrize(
    "title",
    ["Add SEPA instant payments to transfers", "Fix rounding in long account statements"],
)
def test_plain_pr_title_allowed(repo, title):
    git(repo, "switch", "-qc", "feat/x")
    command = f"gh pr create --base main --title '{title}' --body-file /tmp/body.md"
    assert run_hook("guard.py", tool("Bash", cwd=repo, command=command)).returncode == 0


def test_plan_subagent_redirected_to_orchestrator(tmp_path):
    payload = tool("Agent", subagent_type="Plan", prompt="design it")
    payload["session_id"] = tmp_path.name
    result = run_hook("agent_policy.py", payload, "pre", env={"REMI_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 2
    assert "designs the plan itself" in result.stderr
    assert "ExitPlanMode" in result.stderr


# --- CLAUDE.md line limit -------------------------------------------------------------


def lines_of(count: int) -> str:
    return "".join(f"- rule {number}\n" for number in range(count))


@pytest.mark.parametrize(
    "path", ["CLAUDE.md", "/repo/CLAUDE.md", "/repo/.claude/CLAUDE.md", "/repo/api/CLAUDE.local.md"]
)
def test_claude_md_write_over_limit_blocked(path):
    result = run_hook("claude_md.py", tool("Write", file_path=path, content=lines_of(201)))
    assert result.returncode == 2
    assert "201 lines; the limit is 200" in result.stderr


def test_claude_md_write_at_limit_allowed():
    assert run_hook("claude_md.py", tool("Write", file_path="CLAUDE.md", content=lines_of(200))).returncode == 0


def test_other_markdown_not_limited():
    assert run_hook("claude_md.py", tool("Write", file_path="README.md", content=lines_of(500))).returncode == 0
    assert (
        run_hook("claude_md.py", tool("Write", file_path="docs/CLAUDE.md.bak", content=lines_of(500))).returncode == 0
    )


def test_claude_md_edit_computes_result(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(lines_of(199))
    growing = tool("Edit", file_path=str(claude_md), old_string="- rule 0\n", new_string="- rule 0\n- a\n- b\n")
    assert run_hook("claude_md.py", growing).returncode == 2
    shrinking = tool("Edit", file_path=str(claude_md), old_string="- rule 0\n- rule 1\n", new_string="- rules 0-1\n")
    assert run_hook("claude_md.py", shrinking).returncode == 0


def test_claude_md_multiedit(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(lines_of(200))
    edits = [{"old_string": "- rule 1\n", "new_string": "- rule 1\n- extra\n"}]
    assert run_hook("claude_md.py", tool("MultiEdit", file_path=str(claude_md), edits=edits)).returncode == 2


def test_claude_md_limit_configurable():
    env = {"REMI_CLAUDE_MD_MAX_LINES": "50", "PATH": "/usr/bin:/bin"}
    result = run_hook("claude_md.py", tool("Write", file_path="CLAUDE.md", content=lines_of(51)), env=env)
    assert result.returncode == 2


@pytest.mark.parametrize(
    "command",
    [
        "echo '- rule' >> CLAUDE.md",
        "cat notes.md > CLAUDE.md",
        "printf x | tee -a .claude/CLAUDE.md",
        "sed -i '' 's/a/b/' CLAUDE.md",
        "cp ../other/CLAUDE.md CLAUDE.md",
    ],
)
def test_claude_md_shell_writes_blocked(command):
    assert run_hook("claude_md.py", tool("Bash", command=command)).returncode == 2


@pytest.mark.parametrize(
    "command",
    [
        "cat CLAUDE.md",
        "wc -l CLAUDE.md",
        "grep -n rule CLAUDE.md",
        "git add CLAUDE.md",
        "cp CLAUDE.md /tmp/backup.md",
    ],
)
def test_claude_md_shell_reads_allowed(command):
    assert run_hook("claude_md.py", tool("Bash", command=command)).returncode == 0


def test_commit_gate_blocks_long_claude_md(repo):
    git(repo, "switch", "-qc", "feat/x")
    (repo / "CLAUDE.md").write_text(lines_of(250))
    git(repo, "add", "CLAUDE.md")
    result = run_hook("quality.py", tool("Bash", cwd=repo, command="git commit -m 'docs: add claude.md'"), "commit")
    assert result.returncode == 2
    assert "CLAUDE.md has 250 lines (limit 200)" in result.stderr


def test_builtin_explore_redirected_to_cheap_explorer(tmp_path):
    payload = tool("Agent", subagent_type="Explore", prompt="find the settings module")
    payload["session_id"] = tmp_path.name
    result = run_hook("agent_policy.py", payload, "pre", env={"REMI_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 2
    assert "remi:explorer" in result.stderr
    assert "Grep and Read yourself" in result.stderr


def test_explorer_does_not_take_a_worker_slot(tmp_path):
    env = {"REMI_STATE_DIR": str(tmp_path), "REMI_MAX_WORKERS": "1", "PATH": "/usr/bin:/bin"}
    explorer = tool("Agent", subagent_type="remi:explorer", prompt="x")
    worker = tool("Agent", subagent_type="remi:worker", prompt="x")
    for payload in (explorer, worker):
        payload["session_id"] = "slots"
    assert run_hook("agent_policy.py", explorer, "pre", env=env).returncode == 0
    assert run_hook("agent_policy.py", explorer, "pre", env=env).returncode == 0
    assert run_hook("agent_policy.py", worker, "pre", env=env).returncode == 0


# --- legacy-safe checks ------------------------------------------------------------

LEGACY_SOURCE = """def f(x):
    for i in range(x):
        print(i)
    return {  'a':1 }
"""

GOOD_ADDITION = '''

def describe_balance(balance_amount: int) -> str:
    """Describe a balance for a statement line."""
    return f"balance {balance_amount}"
'''


@pytest.fixture
def legacy_repo(repo: Path) -> Path:
    (repo / "legacy.py").write_text(LEGACY_SOURCE)
    git(repo, "add", "legacy.py")
    git(repo, "commit", "-qm", "chore: add legacy module")
    git(repo, "switch", "-qc", "feat/x")
    return repo


def edit_payload(repo: Path, file_name: str) -> dict:
    return tool("Edit", cwd=repo, file_path=str(repo / file_name), old_string="x", new_string="y")


def test_change_scope_ranges():
    sys.path.insert(0, str(SCRIPTS))
    from scope import ChangeScope

    scope = ChangeScope(whole_file=False, changed_lines=frozenset({3, 4, 5, 9, 11, 12}))
    assert scope.ranges() == [(3, 5), (9, 9), (11, 12)]
    assert scope.covers(4) and not scope.covers(6)


def test_edit_in_legacy_file_ignores_untouched_legacy_code(legacy_repo):
    legacy_file = legacy_repo / "legacy.py"
    legacy_file.write_text(LEGACY_SOURCE + GOOD_ADDITION)
    result = run_hook("quality.py", edit_payload(legacy_repo, "legacy.py"), "edit", cwd=legacy_repo)
    assert result.returncode == 0, result.stderr
    content = legacy_file.read_text()
    assert "def f(x):" in content and "{  'a':1 }" in content  # legacy lines untouched, not reformatted
    assert "def describe_balance(balance_amount: int) -> str:" in content


def test_edit_in_legacy_file_flags_only_new_violations(legacy_repo):
    legacy_file = legacy_repo / "legacy.py"
    legacy_file.write_text(
        LEGACY_SOURCE + '\n\ndef add_fee(a: int) -> int:\n    """Add the fee."""\n    return a + 1\n'
    )
    result = run_hook("quality.py", edit_payload(legacy_repo, "legacy.py"), "edit", cwd=legacy_repo)
    assert result.returncode == 2
    assert "'a'" in result.stderr
    assert "'x'" not in result.stderr and "'i'" not in result.stderr  # pre-existing names aren't reported


def test_edit_in_legacy_file_formats_only_changed_range(legacy_repo):
    legacy_file = legacy_repo / "legacy.py"
    legacy_file.write_text(LEGACY_SOURCE + "\nFEES = {  'card':2 }\n")
    result = run_hook("quality.py", edit_payload(legacy_repo, "legacy.py"), "edit", cwd=legacy_repo)
    assert result.returncode == 0, result.stderr
    content = legacy_file.read_text()
    assert 'FEES = {"card": 2}' in content  # the changed line is formatted
    assert "{  'a':1 }" in content  # the legacy line is not


def test_new_file_is_checked_in_full(legacy_repo):
    (legacy_repo / "fresh.py").write_text("def compute(x):\n    return x\n")
    result = run_hook("quality.py", edit_payload(legacy_repo, "fresh.py"), "edit", cwd=legacy_repo)
    assert result.returncode == 2
    assert "D103" in result.stderr and "'x'" in result.stderr


def test_commit_gate_passes_clean_change_in_legacy_file(legacy_repo):
    (legacy_repo / "legacy.py").write_text(LEGACY_SOURCE + GOOD_ADDITION)
    git(legacy_repo, "add", "legacy.py")
    result = run_hook("quality.py", tool("Bash", cwd=legacy_repo, command="git commit -m 'feat: add x'"), "commit")
    assert result.returncode == 0, result.stderr


def test_commit_gate_refuses_unformatted_change_in_legacy_file(legacy_repo):
    (legacy_repo / "legacy.py").write_text(LEGACY_SOURCE + "\nFEES = {  'card':2 }\n")
    git(legacy_repo, "add", "legacy.py")
    result = run_hook("quality.py", tool("Bash", cwd=legacy_repo, command="git commit -m 'feat: add x'"), "commit")
    assert result.returncode == 2
    assert "format failed" in result.stderr and "legacy.py" in result.stderr


# --- remi onboard -----------------------------------------------------------------


@pytest.fixture
def onboarding_repo(repo: Path) -> Path:
    (repo / "legacy.py").write_text(LEGACY_SOURCE)
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("DB_PASSWORD=sentinel-secret-value\n")
    (repo / ".env.production").write_text("API_TOKEN=committed-secret-value\n")
    (repo / "CLAUDE.md").write_text(lines_of(250))
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "layering-reviewer.md").write_text("---\nname: layering-reviewer\n---\n")
    (repo / ".claude" / "skills" / "naming").mkdir(parents=True)
    (repo / ".claude" / "skills" / "naming" / "SKILL.md").write_text("---\nname: naming\n---\n")
    hooks = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit",
                    "hooks": [{"type": "command", "command": '"$CLAUDE_PROJECT_DIR"/.claude/hooks/ruff_on_edit.sh'}],
                }
            ]
        }
    }
    (repo / ".claude" / "settings.json").write_text(json.dumps(hooks))
    (repo / "pyproject.toml").write_text(
        '[[tool.uv.index]]\nname = "corp"\nurl = "https://user@nexus.corp.example/simple"\n'
    )
    git(repo, "add", "legacy.py", ".gitignore", ".env.production", "CLAUDE.md", ".claude", "pyproject.toml")
    git(repo, "commit", "-qm", "chore: legacy project")
    return repo


def run_onboard(repo: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "onboard.py"), *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def test_onboard_reports_project_state(onboarding_repo):
    result = run_onboard(onboarding_repo, "--no-prompt")
    assert result.returncode == 0, result.stderr
    report = result.stdout
    assert "CLAUDE.md: 250 lines (limit 200)" in report
    assert "layering-reviewer: blocked under remi" in report
    assert "same name as a remi skill: naming" in report
    assert "duplicates remi's lint/format hook" in report
    assert ".env.production is committed to git" in report
    assert "git-ignored and unreadable for remi: .env" in report
    assert "no .env.example" in report
    assert "nexus.corp.example" in report and "allowlist" in report
    assert "single-letter names: 3" in report  # f, x, i
    assert "ruff format: 1 file(s) not formatted" in report
    assert "remi --plan" in report


def test_onboard_never_reads_secret_values(onboarding_repo):
    result = run_onboard(onboarding_repo, "--no-prompt")
    combined = result.stdout + result.stderr + run_onboard(onboarding_repo, "--print-prompt").stdout
    assert "sentinel-secret-value" not in combined
    assert "committed-secret-value" not in combined


def test_onboard_prompt_carries_findings(onboarding_repo):
    prompt = run_onboard(onboarding_repo, "--print-prompt").stdout
    assert prompt.startswith("Onboard remi to this repository.")
    assert "CLAUDE.md: 250 lines" in prompt
    assert "service-architecture" in prompt and "chore/remi-onboarding" in prompt


def test_onboard_outside_git(tmp_path):
    result = run_onboard(tmp_path)
    assert result.returncode == 2
    assert "inside a git repository" in result.stderr


# --- credential commands stand alone; new repositories bootstrap themselves --------


@pytest.mark.parametrize(
    "command",
    [
        "cd ~/proj && gh auth status",
        "ls; gh repo view",
        "gh pr list | head -5",
        "cd ~/proj && git init -b main",
        "git add a.txt && git push origin HEAD",
        "GH_PAGER= gh pr view && echo done",
    ],
)
def test_chained_credential_commands_blocked(repo, command):
    result = run_hook("guard.py", tool("Bash", cwd=repo, command=command))
    assert result.returncode == 2
    assert "on its own" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "gh auth status",
        'gh pr create --title "Add limits" --body "a | b; c && d" --head feat/x',
        "git status --short && git diff --stat",
        "git log --oneline | grep push",
        "git init -b main",
    ],
)
def test_standalone_credential_commands_allowed(repo, command):
    git(repo, "switch", "-qc", "feat/x")
    result = run_hook("guard.py", tool("Bash", cwd=repo, command=command))
    assert result.returncode == 0, result.stderr


@pytest.fixture
def new_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_bootstrap_commit_on_new_repo_allowed(new_repo):
    command = 'git commit --allow-empty -m "chore: initial commit"'
    result = run_hook("guard.py", tool("Bash", cwd=new_repo, command=command))
    assert result.returncode == 0, result.stderr


def test_bootstrap_commit_with_staged_files_blocked(new_repo):
    (new_repo / "app.py").write_text("print('hi')\n")
    git(new_repo, "add", "app.py")
    command = 'git commit --allow-empty -m "chore: initial commit"'
    assert run_hook("guard.py", tool("Bash", cwd=new_repo, command=command)).returncode == 2


def test_second_empty_commit_on_main_blocked(new_repo):
    git(new_repo, "commit", "-q", "--allow-empty", "-m", "chore: initial commit")
    command = 'git commit --allow-empty -m "chore: another"'
    assert run_hook("guard.py", tool("Bash", cwd=new_repo, command=command)).returncode == 2


def test_repo_create_push_only_with_bootstrap_main(new_repo):
    command = "gh repo create svc --private --source . --remote origin --push"
    git(new_repo, "commit", "-q", "--allow-empty", "-m", "chore: initial commit")
    assert run_hook("guard.py", tool("Bash", cwd=new_repo, command=command)).returncode == 0
    (new_repo / "app.py").write_text("print('hi')\n")
    git(new_repo, "add", "app.py")
    git(new_repo, "commit", "-qm", "feat: app")
    result = run_hook("guard.py", tool("Bash", cwd=new_repo, command=command))
    assert result.returncode == 2
    assert "bootstrap" in result.stderr


# --- launcher: GitHub login is required ------------------------------------------------

LAUNCHER = SCRIPTS.parents[2] / "bin" / "remi"
SCOPED_RESPONSE = "HTTP/2.0 200 OK\nX-Oauth-Scopes: {scopes}\n\n{{}}"


def run_launcher(tmp_path: Path, gh_script: str | None):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "claude").write_text("#!/bin/sh\necho CLAUDE STARTED\n")
    (fake_bin / "claude").chmod(0o755)
    if gh_script is not None:
        (fake_bin / "gh").write_text(f"#!/bin/sh\n{gh_script}\n")
        (fake_bin / "gh").chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "REMI_NO_BANNER": "1",
    }
    return subprocess.run(["bash", str(LAUNCHER)], capture_output=True, text=True, env=env, cwd=tmp_path, check=False)


@pytest.mark.parametrize(
    ("gh_script", "reason"),
    [
        (None, "'gh' is required"),
        ("exit 1", "gh auth login"),
        (f"printf '{SCOPED_RESPONSE.format(scopes='gist, read:org')}'", "gh auth refresh"),
    ],
)
def test_launcher_requires_github(tmp_path, gh_script, reason):
    result = run_launcher(tmp_path, gh_script)
    assert result.returncode == 1
    assert reason in result.stderr
    assert "CLAUDE STARTED" not in result.stdout


@pytest.mark.parametrize("scopes", ["gist, read:org, repo", None])
def test_launcher_starts_when_github_ready(tmp_path, scopes):
    response = SCOPED_RESPONSE.format(scopes=scopes) if scopes else "HTTP/2.0 200 OK\\n\\n{}"
    result = run_launcher(tmp_path, f"printf '{response}'")
    assert result.returncode == 0, result.stderr
    assert "CLAUDE STARTED" in result.stdout


# --- merge gate: only after APPROVE + MATCHES, on exactly the reviewed files -------------


def reviewer_stops(repo: Path, session: str, agent_id: str, message: str, env: dict) -> None:
    payload = {
        "session_id": session,
        "agent_type": "remi:reviewer",
        "agent_id": agent_id,
        "cwd": str(repo),
        "last_assistant_message": message,
    }
    assert run_hook("quality.py", payload, "worker", env=env).returncode == 0


def reviewed_change(repo: Path, tmp_path: Path, verdict: str = "APPROVE", request: str = "MATCHES") -> dict:
    env = review_env(tmp_path)
    git(repo, "switch", "-qc", "feat/x")
    (repo / "service.py").write_text("ANSWER = 42\n")
    start_reviewer("merge", "reviewer-one", env)
    reviewer_stops(repo, "merge", "reviewer-one", f"VERDICT: {verdict}\n- [severity: low] ...", env)
    reviewer_stops(repo, "merge", "reviewer-one", f"Request check:\nREQUEST: {request}", env)
    git(repo, "add", "service.py")
    git(repo, "commit", "-qm", "feat: add service")
    return env


def merge(repo: Path, env: dict, extra: str = "", pin: bool = True):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    command = "gh pr merge --merge --delete-branch" + (f" --match-head-commit {head}" if pin else "") + extra
    payload = tool("Bash", cwd=repo, command=command)
    payload["session_id"] = "merge"
    return run_hook("merge_gate.py", payload, env=env)


def test_merge_after_approve_and_matches_allowed(repo, tmp_path):
    env = reviewed_change(repo, tmp_path)
    result = merge(repo, env)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("verdict", "request_result"), [("CHANGES_REQUESTED", "MATCHES"), ("APPROVE", "GAPS")])
def test_merge_without_approval_and_match_blocked(repo, tmp_path, verdict, request_result):
    env = reviewed_change(repo, tmp_path, verdict, request_result)
    result = merge(repo, env)
    assert result.returncode == 2
    assert f"VERDICT: {verdict}, REQUEST: {request_result}" in result.stderr


def test_merge_without_review_blocked(repo, tmp_path):
    result = merge(repo, review_env(tmp_path))
    assert result.returncode == 2
    assert "no finished review" in result.stderr


def test_merge_must_pin_head(repo, tmp_path):
    env = reviewed_change(repo, tmp_path)
    result = merge(repo, env, pin=False)
    assert result.returncode == 2
    assert "--match-head-commit" in result.stderr


def test_merge_with_admin_blocked(repo, tmp_path):
    env = reviewed_change(repo, tmp_path)
    assert "--admin" in merge(repo, env, extra=" --admin").stderr


def test_merge_after_change_since_review_blocked(repo, tmp_path):
    env = reviewed_change(repo, tmp_path)
    (repo / "service.py").write_text("ANSWER = 43\n")
    git(repo, "commit", "-qam", "fix: tweak after approval")
    result = merge(repo, env)
    assert result.returncode == 2
    assert "service.py" in result.stderr


def test_merge_blocked_while_newer_review_unfinished(repo, tmp_path):
    env = reviewed_change(repo, tmp_path)
    start_reviewer("merge", "reviewer-two", env)
    assert merge(repo, env).returncode == 2


def test_uncommitted_stray_file_does_not_block_merge(repo, tmp_path):
    env = review_env(tmp_path)
    git(repo, "switch", "-qc", "feat/x")
    (repo / "service.py").write_text("ANSWER = 42\n")
    (repo / "scratch.txt").write_text("notes\n")
    start_reviewer("merge", "reviewer-one", env)
    reviewer_stops(repo, "merge", "reviewer-one", "VERDICT: APPROVE", env)
    reviewer_stops(repo, "merge", "reviewer-one", "REQUEST: MATCHES", env)
    git(repo, "add", "service.py")
    git(repo, "commit", "-qm", "feat: add service")
    result = merge(repo, env)
    assert result.returncode == 0, result.stderr


def test_verdict_read_from_transcript_when_message_missing(repo, tmp_path):
    env = review_env(tmp_path)
    git(repo, "switch", "-qc", "feat/x")
    (repo / "service.py").write_text("ANSWER = 42\n")
    start_reviewer("merge", "reviewer-one", env)
    transcript = tmp_path / "agent.jsonl"
    for text in ("VERDICT: APPROVE", "REQUEST: MATCHES"):
        entry = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        transcript.write_text(json.dumps(entry) + "\n")
        payload = {"session_id": "merge", "agent_type": "remi:reviewer", "agent_id": "reviewer-one", "cwd": str(repo)}
        payload["agent_transcript_path"] = str(transcript)
        assert run_hook("quality.py", payload, "worker", env=env).returncode == 0
    git(repo, "add", "service.py")
    git(repo, "commit", "-qm", "feat: add service")
    result = merge(repo, env)
    assert result.returncode == 0, result.stderr


def test_other_commands_pass_merge_gate(repo, tmp_path):
    payload = tool("Bash", cwd=repo, command="gh pr view --json state")
    assert run_hook("merge_gate.py", payload, env=review_env(tmp_path)).returncode == 0


def test_merge_gate_uses_repo_of_current_directory(repo, tmp_path):
    env = reviewed_change(repo, tmp_path) | {"CLAUDE_PROJECT_DIR": "/"}  # remi launched outside the repo
    result = merge(repo, env)
    assert result.returncode == 0, result.stderr
