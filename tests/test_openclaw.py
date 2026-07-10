from agenticiam import openclaw


def test_mcp_add_commands_quote_token_per_shell():
    commands = openclaw.mcp_add_commands("boss", "/usr/local/bin/agenticiam", ["mcp"], "aiam_secret123")
    assert "openclaw mcp add boss" in commands["bash"]
    assert "--command \"/usr/local/bin/agenticiam\"" in commands["bash"]
    assert "--arg \"mcp\"" in commands["bash"]
    assert "AGENTICIAM_TOKEN='aiam_secret123'" in commands["bash"]
    assert "AGENTICIAM_TOKEN='aiam_secret123'" in commands["powershell"]
    # cmd.exe has no single-quote string syntax — must use double quotes there
    assert "AGENTICIAM_TOKEN=\"aiam_secret123\"" in commands["cmd"]


def test_mcp_add_commands_supports_multiple_args():
    commands = openclaw.mcp_add_commands("boss", "agenticiam", ["mcp", "--verbose"], "tok")
    assert '--arg "mcp"' in commands["bash"]
    assert '--arg "--verbose"' in commands["bash"]


def test_agent_add_command_uses_provider_slash_model_ref():
    commands = openclaw.agent_add_command("boss", "ollama", "llama3.1:8b")
    for variant in commands.values():
        assert '--model "ollama/llama3.1:8b"' in variant
        assert "--non-interactive" in variant
        assert "--workspace" in variant


def test_agent_add_command_slugifies_name():
    commands = openclaw.agent_add_command("Marketing Boss", "anthropic", "claude-sonnet-5")
    assert "openclaw agents add marketing-boss" in commands["bash"]


def test_workspace_path_is_slug_based():
    path = openclaw.workspace_path("Marketing Boss")
    assert path.name == "workspace-marketing-boss"
    assert path.parent.name == ".openclaw"


def test_manager_soul_md_contains_dispatch_guide():
    content = openclaw.manager_soul_md("boss")
    assert content.startswith("# boss — Manager")
    assert "iamDispatchToAgent" in content
    assert content.endswith(openclaw.MANAGER_SYSTEM_PROMPT)


def test_write_manager_soul_writes_file_under_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(openclaw, "workspace_path", lambda name: tmp_path / "workspace-boss")
    path = openclaw.write_manager_soul("boss")
    assert path == tmp_path / "workspace-boss" / "SOUL.md"
    assert path.exists()
    assert "iamDispatchToAgent" in path.read_text(encoding="utf-8")


def test_install_commands_has_all_three_shells():
    commands = openclaw.install_commands()
    assert "openclaw.ai/install.sh" in commands["bash"]
    assert "openclaw onboard" in commands["bash"]
    assert "openclaw.ai/install.ps1" in commands["powershell"]
    assert "cmd" in commands
