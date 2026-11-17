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
    ],
)
def test_dangerous_git_blocked(repo, command):
    git(repo, "switch", "-qc", "feat/x")
    assert run_hook("guard.py", tool("Bash", cwd=repo, command=command)).returncode == 2, command


def test_push_feature_branch_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    for command in ("git push -u origin HEAD", "git push --force-with-lease origin feat/x"):
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
    (repo / "fixture.txt").write_text("card = '4111111111111111'  # cw: allow-secret\nplain text\n")
    git(repo, "add", "fixture.txt")
    result = run_hook("quality.py", tool("Bash", cwd=repo, command="git commit -m x"), "commit")
    assert result.returncode == 0, result.stderr


def test_commit_gate_runs_project_override(repo):
    git(repo, "switch", "-qc", "feat/x")
    (repo / "pyproject.toml").write_text('[tool.cw]\ntest = "exit 3"\n')
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
    assert "cw:worker" in result.stderr


def worker_event(session: str, agent_id: str, event: str) -> dict:
    return {"session_id": session, "agent_type": "cw:worker", "agent_id": agent_id, "hook_event_name": event}


def test_worker_cap(tmp_path):
    session = f"cap-{tmp_path.name}"
    pre = tool("Agent", subagent_type="cw:worker", prompt="x")
    pre["session_id"] = session
    env = {"TMPDIR": str(tmp_path), "CW_MAX_WORKERS": "2", "PATH": "/usr/bin:/bin"}
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
    pre = tool("Agent", subagent_type="cw:worker", prompt="x")
    pre["session_id"] = session
    env = {"TMPDIR": str(tmp_path), "CW_MAX_WORKERS": "2", "PATH": "/usr/bin:/bin"}
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
    for agent in ("Explore", "cw:reviewer"):
        payload = tool("Agent", subagent_type=agent, prompt="x")
        payload["session_id"] = tmp_path.name
        assert run_hook("agent_policy.py", payload, "pre").returncode == 0


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
    assert "naming failed" in result.stderr


def test_project_defined_agents_blocked(tmp_path):
    for agent in ("layering-reviewer", "migration-reviewer", "agent-prompt-reviewer"):
        payload = tool("Agent", subagent_type=agent, prompt="x")
        payload["session_id"] = tmp_path.name
        result = run_hook("agent_policy.py", payload, "pre")
        assert result.returncode == 2, agent
        assert "not part of cw" in result.stderr


# --- pipeline: worker stop and pre-review gates ------------------------------------


def pipeline_env(tmp_path: Path) -> dict:
    return {"TMPDIR": str(tmp_path / "state"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}


def start_worker(session: str, agent_id: str, env: dict) -> None:
    pre = tool("Agent", subagent_type="cw:worker", prompt="x")
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
    (repo / "pyproject.toml").write_text('[tool.cw]\ntest = "exit 3"\n')
    (repo / "tests").mkdir()
    start_worker("deferred", "agent-one", env)
    start_worker("deferred", "agent-two", env)
    result = run_hook("quality.py", worker_stop(repo, "deferred", "agent-one"), "worker", env=env)
    assert result.returncode == 0, result.stderr  # agent-two still running: tests wait for the review gate


def test_last_worker_runs_tests(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.cw]\ntest = "exit 3"\n')
    (repo / "tests").mkdir()
    start_worker("last", "agent-one", env)
    result = run_hook("quality.py", worker_stop(repo, "last", "agent-one"), "worker", env=env)
    assert result.returncode == 2
    assert "Tests are red" in result.stderr


def test_review_refused_while_workers_run(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    start_worker("busy", "agent-one", env)
    payload = tool("Agent", cwd=repo, subagent_type="cw:reviewer", prompt="Review main...feat/x")
    payload["session_id"] = "busy"
    result = run_hook("quality.py", payload, "review", env=env)
    assert result.returncode == 2
    assert "still running" in result.stderr


def test_review_runs_fix_then_gate(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.cw]\nfix = "touch fixed.marker"\ntest = "test -f fixed.marker"\n')
    (repo / "tests").mkdir()
    payload = tool("Agent", cwd=repo, subagent_type="cw:reviewer", prompt="Review main...feat/x")
    payload["session_id"] = "green"
    result = run_hook("quality.py", payload, "review", env=env)
    assert result.returncode == 0, result.stderr
    assert (repo / "fixed.marker").exists()


def test_review_refused_when_tests_red(repo, tmp_path):
    (tmp_path / "state").mkdir()
    env = pipeline_env(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.cw]\nfix = "true"\ntest = "exit 3"\n')
    (repo / "tests").mkdir()
    payload = tool("Agent", cwd=repo, subagent_type="cw:reviewer", prompt="Review main...feat/x")
    payload["session_id"] = "red"
    result = run_hook("quality.py", payload, "review", env=env)
    assert result.returncode == 2
    assert "review is refused" in result.stderr


def test_default_worker_cap_is_three(tmp_path):
    session = f"default-cap-{tmp_path.name}"
    pre = tool("Agent", subagent_type="cw:worker", prompt="x")
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
    result = run_hook("agent_policy.py", payload, "pre", env={"CW_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 2
    assert "designs the plan itself" in result.stderr
    assert "ExitPlanMode" in result.stderr
