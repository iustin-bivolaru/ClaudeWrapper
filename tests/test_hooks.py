import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "scripts"


def run_hook(event: str, payload: dict, cwd: Path | None = None, env: dict | None = None):
    hook_names = {
        "pre-tool": "PreToolUse",
        "post-tool": "PostToolUse",
        "subagent-start": "SubagentStart",
        "subagent-stop": "SubagentStop",
        "prompt": "UserPromptSubmit",
    }
    payload = {"hook_event_name": hook_names[event]} | payload
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "dispatch.py"), event],
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
    result = run_hook("pre-tool", tool("Read", file_path=path))
    assert result.returncode == 2, path
    assert "BLOCKED" in result.stderr


@pytest.mark.parametrize("path", [".env.example", "app/.env.sample", "src/settings.py", "envs/dev.py", "README.md"])
def test_read_normal_allowed(path):
    assert run_hook("pre-tool", tool("Read", file_path=path)).returncode == 0


def test_write_env_blocked_but_example_allowed():
    assert run_hook("pre-tool", tool("Write", file_path=".env", content="X=1")).returncode == 2
    assert run_hook("pre-tool", tool("Edit", file_path=".env.example")).returncode == 0


def test_grep_glob_on_secrets_blocked():
    assert run_hook("pre-tool", tool("Grep", pattern="PASS", path=".env")).returncode == 2
    assert run_hook("pre-tool", tool("Grep", pattern="PASS", glob="**/.env*")).returncode == 2
    assert run_hook("pre-tool", tool("Glob", pattern="**/*.pem")).returncode == 2
    assert run_hook("pre-tool", tool("Grep", pattern="def ", path="src")).returncode == 0


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
    assert run_hook("pre-tool", tool("Bash", command=command)).returncode == 2, command


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
    assert run_hook("pre-tool", tool("Bash", cwd=repo, command=command)).returncode == 0, command


def dispatch_raw(*arguments: str, stdin: str):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "dispatch.py"), *arguments], input=stdin, capture_output=True, text=True
    )


def test_malformed_payload_fails_closed():
    assert dispatch_raw("pre-tool", stdin="not json").returncode == 2


def test_malformed_payload_ignored_outside_pre_tool():
    assert dispatch_raw("post-tool", stdin="not json").returncode == 0
    assert dispatch_raw("subagent-stop", stdin="[1]").returncode == 0


def test_unknown_event_blocks():
    result = dispatch_raw("pre-everything", stdin="{}")
    assert result.returncode == 2
    assert "unknown hook event" in result.stderr


def test_first_blocking_check_wins(repo):
    # The guard blocks `printenv` before the commit and merge gates run.
    result = run_hook("pre-tool", tool("Bash", repo, command="printenv && git commit -m 'feat: x'"))
    assert result.returncode == 2
    assert "printenv" in result.stderr


def test_plain_calls_pass_every_check(repo):
    for payload in (
        tool("Read", repo, file_path=str(repo / "a.txt")),
        tool("Bash", repo, command="ls"),
        tool("Write", repo, file_path=str(repo / "notes.md"), content="hi\n"),
    ):
        assert run_hook("pre-tool", payload).returncode == 0, payload["tool_name"]


def test_hooks_json_uses_only_the_dispatcher():
    config = json.loads((SCRIPTS.parent / "hooks.json").read_text())
    commands = [hook["command"] for groups in config["hooks"].values() for group in groups for hook in group["hooks"]]
    assert commands
    assert all('/hooks/scripts/dispatch.py" ' in command for command in commands)


# --- git policy -------------------------------------------------------------


def test_commit_on_main_blocked(repo):
    assert run_hook("pre-tool", tool("Bash", cwd=repo, command="git commit -m 'feat: x'")).returncode == 2


def test_commit_after_branch_switch_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    result = run_hook("pre-tool", tool("Bash", cwd=repo, command="git commit -m 'feat: add x'"))
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
    assert run_hook("pre-tool", tool("Bash", cwd=repo, command=command)).returncode == 2, command


def test_push_feature_branch_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    for command in ("git push -u origin HEAD", "git push --force-with-lease origin feat/x"):
        assert run_hook("pre-tool", tool("Bash", cwd=repo, command=command)).returncode == 0, command


def test_private_repo_create_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    for command in ("gh repo create acme/x --private --source . --push", "gh repo create acme/x --internal"):
        assert run_hook("pre-tool", tool("Bash", cwd=repo, command=command)).returncode == 0, command


# --- commit gate --------------------------------------------------------------


def test_commit_gate_requires_separate_staging(repo):
    git(repo, "switch", "-qc", "feat/x")
    result = run_hook("pre-tool", tool("Bash", cwd=repo, command="git add -A && git commit -m 'feat: x'"))
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
    result = run_hook("pre-tool", tool("Bash", cwd=repo, command="git commit -m 'feat: x'"))
    assert result.returncode == 2, content
    assert "secrets" in result.stderr


@pytest.mark.parametrize("subdirectory", ["", "src"])
def test_commit_gate_checks_the_repository_it_runs_in(repo, tmp_path, subdirectory):
    # remi launched in a parent folder: the gate must check the repository around the
    # command's cwd, not CLAUDE_PROJECT_DIR (where it used to pass without checking anything).
    git(repo, "switch", "-qc", "feat/x")
    (repo / "src").mkdir()
    (repo / "src" / "leak.txt").write_text("aws = 'AKIAABCDEFGHIJKLMNOP'\n")
    git(repo, "add", "src/leak.txt")
    payload = tool("Bash", cwd=repo / subdirectory, command="git commit -m 'feat: x'")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path.parent)}
    result = run_hook("pre-tool", payload, env=env)
    assert result.returncode == 2
    assert "AWS access key" in result.stderr


def test_commit_gate_allow_marker_and_clean_commit(repo):
    git(repo, "switch", "-qc", "feat/x")
    (repo / "fixture.txt").write_text("card = '4111111111111111'  # remi: allow-secret\nplain text\n")
    git(repo, "add", "fixture.txt")
    result = run_hook("pre-tool", tool("Bash", cwd=repo, command="git commit -m 'feat: x'"))
    assert result.returncode == 0, result.stderr


def test_commit_gate_runs_project_override(repo):
    git(repo, "switch", "-qc", "feat/x")
    (repo / "pyproject.toml").write_text('[tool.remi]\ntest = "exit 3"\n')
    git(repo, "add", "pyproject.toml")
    result = run_hook("pre-tool", tool("Bash", cwd=repo, command="git commit -m 'feat: x'"))
    assert result.returncode == 2
    assert "test failed" in result.stderr


# --- agent policy -------------------------------------------------------------


def test_general_purpose_agent_blocked(tmp_path):
    payload = tool("Agent", subagent_type="general-purpose", prompt="x")
    payload["session_id"] = tmp_path.name
    result = run_hook("pre-tool", payload)
    assert result.returncode == 2
    assert "remi:worker" in result.stderr and "general-purpose" in result.stderr


def test_explore_and_reviewer_allowed(tmp_path, repo):
    for agent in ("remi:explorer", "remi:reviewer"):
        payload = tool("Agent", repo, subagent_type=agent, prompt="x")
        payload["session_id"] = tmp_path.name
        assert run_hook("pre-tool", payload).returncode == 0


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
    result = run_hook("pre-tool", tool("Bash", cwd=repo, command=command))
    assert result.returncode == 2
    assert "attribution" in result.stderr


def test_plain_pr_allowed(repo):
    git(repo, "switch", "-qc", "feat/x")
    command = 'gh pr create --title "feat: add fx rates" --body "## What\n- rates"'
    assert run_hook("pre-tool", tool("Bash", cwd=repo, command=command)).returncode == 0


def test_project_defined_agents_blocked(tmp_path):
    for agent in ("layering-reviewer", "migration-reviewer", "agent-prompt-reviewer"):
        payload = tool("Agent", subagent_type=agent, prompt="x")
        payload["session_id"] = tmp_path.name
        result = run_hook("pre-tool", payload)
        assert result.returncode == 2, agent
        assert "part of remi" in result.stderr


@pytest.mark.parametrize(
    "title",
    ["Add SEPA instant payments to transfers", "Fix rounding in long account statements"],
)
def test_plain_pr_title_allowed(repo, title):
    git(repo, "switch", "-qc", "feat/x")
    command = f"gh pr create --base main --title '{title}' --body-file /tmp/body.md"
    assert run_hook("pre-tool", tool("Bash", cwd=repo, command=command)).returncode == 0


def test_plan_subagent_blocked(tmp_path):
    payload = tool("Agent", subagent_type="Plan", prompt="design it")
    payload["session_id"] = tmp_path.name
    result = run_hook("pre-tool", payload, env={"REMI_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 2
    assert "expensive session model" in result.stderr


# --- CLAUDE.md line limit -------------------------------------------------------------


def lines_of(count: int) -> str:
    return "".join(f"- rule {number}\n" for number in range(count))


@pytest.mark.parametrize(
    "path", ["CLAUDE.md", "/repo/CLAUDE.md", "/repo/.claude/CLAUDE.md", "/repo/api/CLAUDE.local.md"]
)
def test_claude_md_write_over_limit_blocked(path):
    result = run_hook("pre-tool", tool("Write", file_path=path, content=lines_of(201)))
    assert result.returncode == 2
    assert "201 lines; the limit is 200" in result.stderr


def test_claude_md_write_at_limit_allowed():
    assert run_hook("pre-tool", tool("Write", file_path="CLAUDE.md", content=lines_of(200))).returncode == 0


def test_other_markdown_not_limited():
    assert run_hook("pre-tool", tool("Write", file_path="README.md", content=lines_of(500))).returncode == 0
    assert run_hook("pre-tool", tool("Write", file_path="docs/CLAUDE.md.bak", content=lines_of(500))).returncode == 0


def test_claude_md_edit_computes_result(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(lines_of(199))
    growing = tool("Edit", file_path=str(claude_md), old_string="- rule 0\n", new_string="- rule 0\n- a\n- b\n")
    assert run_hook("pre-tool", growing).returncode == 2
    shrinking = tool("Edit", file_path=str(claude_md), old_string="- rule 0\n- rule 1\n", new_string="- rules 0-1\n")
    assert run_hook("pre-tool", shrinking).returncode == 0


def test_claude_md_multiedit(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(lines_of(200))
    edits = [{"old_string": "- rule 1\n", "new_string": "- rule 1\n- extra\n"}]
    assert run_hook("pre-tool", tool("MultiEdit", file_path=str(claude_md), edits=edits)).returncode == 2


def test_claude_md_limit_configurable():
    env = {"REMI_CLAUDE_MD_MAX_LINES": "50", "PATH": "/usr/bin:/bin"}
    result = run_hook("pre-tool", tool("Write", file_path="CLAUDE.md", content=lines_of(51)), env=env)
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
    assert run_hook("pre-tool", tool("Bash", command=command)).returncode == 2


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
    assert run_hook("pre-tool", tool("Bash", command=command)).returncode == 0


def test_builtin_explore_blocked(tmp_path):
    payload = tool("Agent", subagent_type="Explore", prompt="find the settings module")
    payload["session_id"] = tmp_path.name
    result = run_hook("pre-tool", payload, env={"REMI_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 2
    assert "remi:explorer" in result.stderr
    assert "Grep and Read yourself" in result.stderr


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
    result = run_hook("post-tool", edit_payload(legacy_repo, "legacy.py"), cwd=legacy_repo)
    assert result.returncode == 0, result.stderr
    content = legacy_file.read_text()
    assert "def f(x):" in content and "{  'a':1 }" in content  # legacy lines untouched, not reformatted
    assert "def describe_balance(balance_amount: int) -> str:" in content


def test_edit_in_legacy_file_flags_only_new_violations(legacy_repo):
    legacy_file = legacy_repo / "legacy.py"
    legacy_file.write_text(LEGACY_SOURCE + "\n\ndef add_fee(amount: int) -> int:\n    return amount + 1\n")
    result = run_hook("post-tool", edit_payload(legacy_repo, "legacy.py"), cwd=legacy_repo)
    assert result.returncode == 2
    assert "legacy.py:7:5: D103" in result.stderr  # the new function has no docstring
    assert "legacy.py:1:" not in result.stderr  # the legacy one isn't reported


def test_edit_in_legacy_file_formats_only_changed_range(legacy_repo):
    legacy_file = legacy_repo / "legacy.py"
    legacy_file.write_text(LEGACY_SOURCE + "\nFEES = {  'card':2 }\n")
    result = run_hook("post-tool", edit_payload(legacy_repo, "legacy.py"), cwd=legacy_repo)
    assert result.returncode == 0, result.stderr
    content = legacy_file.read_text()
    assert 'FEES = {"card": 2}' in content  # the changed line is formatted
    assert "{  'a':1 }" in content  # the legacy line is not


def test_new_file_is_checked_in_full(legacy_repo):
    (legacy_repo / "fresh.py").write_text("def compute(x):\n    return x\n")
    result = run_hook("post-tool", edit_payload(legacy_repo, "fresh.py"), cwd=legacy_repo)
    assert result.returncode == 2
    assert "D103" in result.stderr


def test_commit_gate_passes_clean_change_in_legacy_file(legacy_repo):
    (legacy_repo / "legacy.py").write_text(LEGACY_SOURCE + GOOD_ADDITION)
    git(legacy_repo, "add", "legacy.py")
    result = run_hook("pre-tool", tool("Bash", cwd=legacy_repo, command="git commit -m 'feat: add x'"))
    assert result.returncode == 0, result.stderr


def test_commit_gate_refuses_unformatted_change_in_legacy_file(legacy_repo):
    (legacy_repo / "legacy.py").write_text(LEGACY_SOURCE + "\nFEES = {  'card':2 }\n")
    git(legacy_repo, "add", "legacy.py")
    result = run_hook("pre-tool", tool("Bash", cwd=legacy_repo, command="git commit -m 'feat: add x'"))
    assert result.returncode == 2
    assert "format failed" in result.stderr and "legacy.py" in result.stderr


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
    result = run_hook("pre-tool", tool("Bash", cwd=repo, command=command))
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
    result = run_hook("pre-tool", tool("Bash", cwd=repo, command=command))
    assert result.returncode == 0, result.stderr


@pytest.fixture
def new_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_bootstrap_commit_on_new_repo_allowed(new_repo):
    command = 'git commit --allow-empty -m "chore: initial commit"'
    result = run_hook("pre-tool", tool("Bash", cwd=new_repo, command=command))
    assert result.returncode == 0, result.stderr


def test_bootstrap_commit_with_staged_files_blocked(new_repo):
    (new_repo / "app.py").write_text("print('hi')\n")
    git(new_repo, "add", "app.py")
    command = 'git commit --allow-empty -m "chore: initial commit"'
    assert run_hook("pre-tool", tool("Bash", cwd=new_repo, command=command)).returncode == 2


def test_second_empty_commit_on_main_blocked(new_repo):
    git(new_repo, "commit", "-q", "--allow-empty", "-m", "chore: initial commit")
    command = 'git commit --allow-empty -m "chore: another"'
    assert run_hook("pre-tool", tool("Bash", cwd=new_repo, command=command)).returncode == 2


def test_repo_create_push_only_with_bootstrap_main(new_repo):
    command = "gh repo create svc --private --source . --remote origin --push"
    git(new_repo, "commit", "-q", "--allow-empty", "-m", "chore: initial commit")
    assert run_hook("pre-tool", tool("Bash", cwd=new_repo, command=command)).returncode == 0
    (new_repo / "app.py").write_text("print('hi')\n")
    git(new_repo, "add", "app.py")
    git(new_repo, "commit", "-qm", "feat: app")
    result = run_hook("pre-tool", tool("Bash", cwd=new_repo, command=command))
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


def review_env(tmp_path: Path) -> dict:
    return {"REMI_STATE_DIR": str(tmp_path / "state"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}


def reviewer_stops(repo: Path, session: str, agent_id: str, message: str, env: dict) -> None:
    payload = {
        "session_id": session,
        "agent_type": "remi:reviewer",
        "agent_id": agent_id,
        "cwd": str(repo),
        "last_assistant_message": message,
    }
    assert run_hook("subagent-stop", payload, env=env).returncode == 0


def reviewed_change(repo: Path, tmp_path: Path, verdict: str = "APPROVE", request: str = "MATCHES") -> dict:
    env = review_env(tmp_path)
    git(repo, "switch", "-qc", "feat/x")
    (repo / "service.py").write_text("ANSWER = 42\n")
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
    return run_hook("pre-tool", payload, env=env)


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


def test_uncommitted_stray_file_does_not_block_merge(repo, tmp_path):
    env = review_env(tmp_path)
    git(repo, "switch", "-qc", "feat/x")
    (repo / "service.py").write_text("ANSWER = 42\n")
    (repo / "scratch.txt").write_text("notes\n")
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
    transcript = tmp_path / "agent.jsonl"
    for text in ("VERDICT: APPROVE", "REQUEST: MATCHES"):
        entry = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        transcript.write_text(json.dumps(entry) + "\n")
        payload = {"session_id": "merge", "agent_type": "remi:reviewer", "agent_id": "reviewer-one", "cwd": str(repo)}
        payload["agent_transcript_path"] = str(transcript)
        assert run_hook("subagent-stop", payload, env=env).returncode == 0
    git(repo, "add", "service.py")
    git(repo, "commit", "-qm", "feat: add service")
    result = merge(repo, env)
    assert result.returncode == 0, result.stderr


def test_other_commands_pass_merge_gate(repo, tmp_path):
    payload = tool("Bash", cwd=repo, command="gh pr view --json state")
    assert run_hook("pre-tool", payload, env=review_env(tmp_path)).returncode == 0


def test_merge_gate_uses_repo_of_current_directory(repo, tmp_path):
    env = reviewed_change(repo, tmp_path) | {"CLAUDE_PROJECT_DIR": "/"}  # remi launched outside the repo
    result = merge(repo, env)
    assert result.returncode == 0, result.stderr
