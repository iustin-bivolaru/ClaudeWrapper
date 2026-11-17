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
    assert run_hook("guard.py", tool("Bash", cwd=repo, command="git commit -m x")).returncode == 0


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


def test_worker_cap(tmp_path):
    session = f"cap-{tmp_path.name}"
    pre = tool("Agent", subagent_type="cw:worker", prompt="x")
    pre["session_id"] = session
    env = {"TMPDIR": str(tmp_path), "CW_MAX_WORKERS": "2", "PATH": "/usr/bin:/bin"}
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 0
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 0
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 2
    stop = {"session_id": session, "agent_type": "cw:worker", "hook_event_name": "SubagentStop"}
    assert run_hook("agent_policy.py", stop, "stop", env=env).returncode == 0
    assert run_hook("agent_policy.py", pre, "pre", env=env).returncode == 0


def test_explore_and_reviewer_allowed(tmp_path):
    for agent in ("Explore", "cw:reviewer"):
        payload = tool("Agent", subagent_type=agent, prompt="x")
        payload["session_id"] = tmp_path.name
        assert run_hook("agent_policy.py", payload, "pre").returncode == 0
