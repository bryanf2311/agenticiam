import json

import pytest

from agenticiam import openclaw


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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


def test_agent_add_command_bind_telegram_appends_flag():
    commands = openclaw.agent_add_command("boss", "ollama", "llama3.1:8b", bind_telegram=True)
    for variant in commands.values():
        assert variant.endswith("--bind telegram:*")


def test_agent_add_command_no_telegram_flag_by_default():
    commands = openclaw.agent_add_command("boss", "ollama", "llama3.1:8b")
    for variant in commands.values():
        assert "telegram" not in variant


def test_agent_add_command_maps_ollama_cloud_to_openclaw_provider_id():
    # AgenticIAM/Goose call it "ollama_cloud"; OpenClaw has no built-in
    # provider of that name, so the registered custom provider id
    # ("ollama-cloud") is used in the model ref instead.
    commands = openclaw.agent_add_command("boss", "ollama_cloud", "gpt-oss:120b-cloud")
    for variant in commands.values():
        assert '--model "ollama-cloud/gpt-oss:120b-cloud"' in variant
        assert "ollama_cloud" not in variant


def test_ollama_cloud_provider_command_registers_openai_compatible_provider():
    commands = openclaw.ollama_cloud_provider_command("sk-fake-cloud-key", "gpt-oss:120b-cloud")
    assert commands["bash"] == commands["powershell"]
    assert f"models.providers.{openclaw.OLLAMA_CLOUD_PROVIDER_ID}" in commands["bash"]
    assert "--strict-json --merge" in commands["bash"]

    payload_str = commands["bash"].split("--merge", 1)[0].split(" '", 1)[1].rsplit("' ", 1)[0]
    payload = json.loads(payload_str)
    assert payload["baseUrl"] == openclaw.OLLAMA_CLOUD_BASE_URL
    assert payload["apiKey"] == "sk-fake-cloud-key"
    assert payload["api"] == "openai-completions"
    assert payload["models"] == [{"id": "gpt-oss:120b-cloud"}]


def test_ollama_cloud_provider_command_cmd_variant_is_valid_json_when_unescaped():
    commands = openclaw.ollama_cloud_provider_command("sk-fake-cloud-key")
    inner = commands["cmd"].split('config set models.providers.ollama-cloud "', 1)[1].rsplit('" --strict-json', 1)[0]
    payload = json.loads(inner.replace('\\"', '"'))
    assert payload["apiKey"] == "sk-fake-cloud-key"
    assert "models" not in payload  # no model given -> no models array


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


# ---------------------------------------------------------------- direct CLI integration


def test_run_openclaw_returns_stdout(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="hello\n")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    result = openclaw._run_openclaw(["agents", "list", "--json"], openclaw_binary="openclaw")
    assert result == "hello\n"
    assert captured["cmd"] == ["openclaw", "agents", "list", "--json"]


def test_run_openclaw_closes_stdin_to_avoid_hanging_on_a_prompt(monkeypatch):
    # A real field report: `openclaw agents list --json` hung until timeout
    # with the gateway running. If the CLI ever falls back to reading a
    # prompt from stdin, an inherited stdin lets it block forever instead of
    # failing fast — stdin must always be closed for a caller with no
    # terminal to answer it.
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(returncode=0, stdout="ok")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw._run_openclaw(["agents", "list", "--json"], openclaw_binary="openclaw")
    assert captured["kwargs"]["stdin"] == openclaw.subprocess.DEVNULL


def test_run_openclaw_timeout_surfaces_partial_output(monkeypatch):
    def fake_run(*a, **k):
        raise openclaw.subprocess.TimeoutExpired(cmd="openclaw", timeout=15, output="partial stdout", stderr="partial stderr")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    with pytest.raises(openclaw.OpenClawCliError, match="partial stdout") as exc_info:
        openclaw._run_openclaw(["agents", "list", "--json"], openclaw_binary="openclaw", timeout=15)
    assert "partial stderr" in str(exc_info.value)


def test_run_openclaw_json_recovers_complete_output_from_a_timeout(monkeypatch):
    # Real field report: `openclaw agents list --json` was reported "timed
    # out after 15.0s" but the timeout's own partial-output capture showed
    # a complete, well-formed JSON array — the CLI's work was done and
    # correct, it just never exited the process on its own. Read-only
    # calls (_run_openclaw_json) must treat that as success, not failure.
    real_agents = [{"id": "main", "isDefault": True}, {"id": "autotask", "model": "ollama-cloud/deepseek-v4-pro"}]

    def fake_run(*a, **k):
        raise openclaw.subprocess.TimeoutExpired(
            cmd="openclaw", timeout=15, output=json.dumps(real_agents), stderr="[state-migrations] ..."
        )

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    result = openclaw._run_openclaw_json(["agents", "list", "--json"], openclaw_binary="openclaw", timeout=15)
    assert result == real_agents


def test_run_openclaw_json_timeout_with_incomplete_output_still_raises(monkeypatch):
    # A genuine hang (no complete JSON yet) must still surface as an error
    # rather than being swallowed by the recovery path above.
    def fake_run(*a, **k):
        raise openclaw.subprocess.TimeoutExpired(cmd="openclaw", timeout=15, output='[{"id": "main"', stderr="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    with pytest.raises(openclaw.OpenClawTimeoutError, match="timed out"):
        openclaw._run_openclaw_json(["agents", "list", "--json"], openclaw_binary="openclaw", timeout=15)


def test_list_agents_recovers_from_timeout_with_full_output(monkeypatch):
    real_agents = [{"id": "main"}, {"id": "autotask"}]

    def fake_run(*a, **k):
        raise openclaw.subprocess.TimeoutExpired(cmd="openclaw", timeout=15, output=json.dumps(real_agents), stderr="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    assert openclaw.list_agents(openclaw_binary="openclaw", timeout=15) == real_agents


def test_config_set_verified_succeeds_when_readback_matches_after_timeout(monkeypatch):
    # config set prints no output to recover from directly — the fallback
    # is a follow-up config get compared against the intended value.
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["config", "set"]:
            raise openclaw.subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        return _FakeCompletedProcess(returncode=0, stdout=json.dumps(["read", "write"]))

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw._config_set_verified("agents.list[0].tools.allow", ["read", "write"], openclaw_binary="openclaw")
    assert len(calls) == 2
    assert calls[0][1:3] == ["config", "set"]
    assert calls[1][1:3] == ["config", "get"]


def test_config_set_verified_raises_when_readback_does_not_match(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        if cmd[1:3] == ["config", "set"]:
            raise openclaw.subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        return _FakeCompletedProcess(returncode=0, stdout=json.dumps(["something-else"]))

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    with pytest.raises(openclaw.OpenClawTimeoutError):
        openclaw._config_set_verified("agents.list[0].tools.allow", ["read", "write"], openclaw_binary="openclaw")


def test_config_set_verified_no_readback_needed_on_normal_success(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw._config_set_verified("agents.list[0].tools.allow", ["read"], openclaw_binary="openclaw")
    assert len(calls) == 1


def test_run_openclaw_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="boom"):
        openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw")


def test_run_openclaw_none_stdout_and_stderr_do_not_crash(monkeypatch):
    # Same defensive fix applied to goose.run_agent_task after a real field
    # report — guard here from the start rather than waiting for a repeat.
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout=None, stderr=None),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="exited with status 1"):
        openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw")

    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=None, stderr=""),
    )
    assert openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw") == ""


def test_run_openclaw_missing_binary_raises(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    with pytest.raises(openclaw.OpenClawCliError, match="not found"):
        openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw")


def test_run_openclaw_timeout_raises(monkeypatch):
    def fake_run(*a, **k):
        raise openclaw.subprocess.TimeoutExpired(cmd="openclaw", timeout=5)

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    with pytest.raises(openclaw.OpenClawCliError, match="timed out"):
        openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw", timeout=5)


def test_run_openclaw_json_parses_output(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout='{"a": 1}\n'),
    )
    assert openclaw._run_openclaw_json(["config", "get", "x"], openclaw_binary="openclaw") == {"a": 1}


def test_run_openclaw_json_empty_output_is_none(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=""),
    )
    assert openclaw._run_openclaw_json(["config", "get", "x"], openclaw_binary="openclaw") is None


def test_run_openclaw_json_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout="not json"),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="non-JSON"):
        openclaw._run_openclaw_json(["config", "get", "x"], openclaw_binary="openclaw")


def test_list_agents_unwraps_agents_key(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=json.dumps({"agents": [{"id": "boss"}]})),
    )
    assert openclaw.list_agents(openclaw_binary="openclaw") == [{"id": "boss"}]


def test_list_agents_accepts_bare_list(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=json.dumps([{"id": "boss"}])),
    )
    assert openclaw.list_agents(openclaw_binary="openclaw") == [{"id": "boss"}]


def test_list_agents_empty_output_returns_empty_list(monkeypatch):
    monkeypatch.setattr(openclaw.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=""))
    assert openclaw.list_agents(openclaw_binary="openclaw") == []


def test_agent_index_finds_matching_id(monkeypatch):
    monkeypatch.setattr(
        openclaw, "list_agents", lambda openclaw_binary=None, timeout=openclaw.DEFAULT_CLI_TIMEOUT: [
            {"id": "intern"}, {"id": "boss"},
        ],
    )
    assert openclaw._agent_index("boss") == 1


def test_agent_index_missing_id_raises(monkeypatch):
    monkeypatch.setattr(
        openclaw, "list_agents", lambda openclaw_binary=None, timeout=openclaw.DEFAULT_CLI_TIMEOUT: [{"id": "intern"}]
    )
    with pytest.raises(openclaw.OpenClawCliError, match="no OpenClaw agent"):
        openclaw._agent_index("boss")


def test_get_agent_config_uses_index_in_path(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 2)
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout=json.dumps({"id": "boss", "tools": {"allow": ["read"]}}))

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    result = openclaw.get_agent_config("boss", openclaw_binary="openclaw")
    assert result == {"id": "boss", "tools": {"allow": ["read"]}}
    assert captured["cmd"] == ["openclaw", "config", "get", "agents.list[2]", "--json"]


def test_set_agent_tools_only_writes_provided_fields(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 0)
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.set_agent_tools("boss", allow=["read", "write"], openclaw_binary="openclaw")
    assert len(calls) == 1
    assert calls[0] == [
        "openclaw", "config", "set", "agents.list[0].tools.allow", json.dumps(["read", "write"]), "--strict-json",
    ]


def test_set_agent_tools_writes_allow_and_deny_separately(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 0)
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.set_agent_tools("boss", allow=["read"], deny=["browser"], openclaw_binary="openclaw")
    paths = [c[3] for c in calls]
    assert paths == ["agents.list[0].tools.allow", "agents.list[0].tools.deny"]


def test_set_agent_filesystem_binds(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 3)
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.set_agent_filesystem_binds("boss", ["/data:/data:ro"], openclaw_binary="openclaw")
    assert captured["cmd"][3] == "agents.list[3].sandbox.docker.binds"
    assert json.loads(captured["cmd"][4]) == ["/data:/data:ro"]


def test_set_agent_sandbox_writes_only_given_fields(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 0)
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.set_agent_sandbox("boss", network="none", openclaw_binary="openclaw")
    assert len(calls) == 1
    assert calls[0][3] == "agents.list[0].sandbox.docker.network"
    assert json.loads(calls[0][4]) == "none"


def test_get_website_allowlist_defaults_to_empty_list(monkeypatch):
    monkeypatch.setattr(openclaw.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=""))
    assert openclaw.get_website_allowlist(openclaw_binary="openclaw") == []


def test_set_website_allowlist_writes_global_path(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.set_website_allowlist(["example.com"], openclaw_binary="openclaw")
    assert captured["cmd"] == [
        "openclaw", "config", "set", "browser.ssrfPolicy.hostnameAllowlist", json.dumps(["example.com"]), "--strict-json",
    ]


def test_tool_catalog_groups_cover_expected_tools():
    assert "browser" in openclaw.TOOL_CATALOG["Web access"]
    assert "read" in openclaw.TOOL_CATALOG["File access"]
    assert "write" in openclaw.TOOL_CATALOG["File access"]


# ---------------------------------------------------------------- Telegram


def test_connect_telegram_channel_uses_token_flag_directly(monkeypatch):
    # NOT --token-file: a real field report showed it persists as a
    # `tokenFile` config pointer (not a one-time read) that takes priority
    # over `botToken` even when stale — this module previously deleted its
    # own temp file right after the command ran, orphaning that reference
    # and silently breaking the channel on the next config read/restart.
    # --token writes the token inline as `botToken`, no dangling reference.
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.connect_telegram_channel("sk-fake-bot-token", openclaw_binary="openclaw")
    assert calls[0] == ["openclaw", "channels", "add", "--channel", "telegram", "--token", "sk-fake-bot-token"]
    assert not any("--token-file" in c for c in calls)


def test_connect_telegram_channel_failure_propagates(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout="", stderr="bad token"),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="bad token"):
        openclaw.connect_telegram_channel("sk-fake-bot-token", openclaw_binary="openclaw")


def test_connect_telegram_channel_default_pairing_policy(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.connect_telegram_channel("tok", openclaw_binary="openclaw")
    dm_policy_call = next(c for c in calls if c[1:3] == ["config", "set"] and c[3] == "channels.telegram.dmPolicy")
    assert dm_policy_call[4] == json.dumps("pairing")
    assert not any(c[3] == "channels.telegram.allowFrom" for c in calls if c[1:3] == ["config", "set"])


def test_connect_telegram_channel_open_policy_sets_allow_from_wildcard(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.connect_telegram_channel("tok", dm_policy="open", openclaw_binary="openclaw")
    dm_policy_call = next(c for c in calls if c[1:3] == ["config", "set"] and c[3] == "channels.telegram.dmPolicy")
    allow_from_call = next(c for c in calls if c[1:3] == ["config", "set"] and c[3] == "channels.telegram.allowFrom")
    assert dm_policy_call[4] == json.dumps("open")
    assert allow_from_call[4] == json.dumps(["*"])


def test_connect_telegram_channel_rejects_invalid_dm_policy():
    with pytest.raises(ValueError, match="dm_policy"):
        openclaw.connect_telegram_channel("tok", dm_policy="whatever")


def test_get_telegram_status_configured_with_dm_policy(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(
            returncode=0, stdout=json.dumps({"enabled": True, "botToken": "sk-xxx", "dmPolicy": "open"})
        ),
    )
    assert openclaw.get_telegram_status(openclaw_binary="openclaw") == {"configured": True, "dm_policy": "open"}


def test_get_telegram_status_not_configured(monkeypatch):
    monkeypatch.setattr(openclaw.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=""))
    assert openclaw.get_telegram_status(openclaw_binary="openclaw") == {"configured": False, "dm_policy": "pairing"}


def test_get_telegram_status_defaults_dm_policy_to_pairing_when_configured_but_unset(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=json.dumps({"botToken": "sk-xxx"})),
    )
    assert openclaw.get_telegram_status(openclaw_binary="openclaw") == {"configured": True, "dm_policy": "pairing"}


def test_bind_agent_to_telegram(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    openclaw.bind_agent_to_telegram("boss", openclaw_binary="openclaw")
    assert captured["cmd"] == ["openclaw", "agents", "bind", "--agent", "boss", "--bind", "telegram:*"]
