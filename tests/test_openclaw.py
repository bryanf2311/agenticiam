import json
import subprocess

import pytest

from agenticiam import openclaw


class _FakePopen:
    """Stands in for subprocess.Popen. communicate() mirrors the real
    contract _run_openclaw relies on: pass timeout_then_output to make
    the FIRST communicate() call raise TimeoutExpired (as a real timeout
    would) and the SECOND (the post-kill drain _run_openclaw does) return
    that tuple — mirrors subprocess.run's own kill-then-drain behavior on
    a real timeout, which raw Popen.communicate() doesn't do for you."""

    def __init__(self, returncode=0, stdout="", stderr="", timeout_then_output=None):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = 12345
        self._timeout_then_output = timeout_then_output
        self._communicate_calls = 0

    def communicate(self, timeout=None):
        self._communicate_calls += 1
        if self._timeout_then_output is not None and self._communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd="openclaw", timeout=timeout)
        if self._timeout_then_output is not None:
            return self._timeout_then_output
        return self._stdout, self._stderr


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


def test_agent_add_command_bind_telegram_appends_specific_account_flag():
    commands = openclaw.agent_add_command("boss", "ollama", "llama3.1:8b", telegram_account_id="support-bot")
    for variant in commands.values():
        assert variant.endswith("--bind telegram:support-bot")
        assert "telegram:*" not in variant


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
    # Zod-validated on OpenClaw's end: a bare {"id": ...} is rejected with
    # "Config validation failed: ...models.0.name: Invalid input" (a real
    # field report) — name is required, and contextWindow is documented as
    # needing to be >= 16000 (recommended >= 65536) or the gateway's own
    # tooling auto-blocks the model.
    assert payload["models"] == [{
        "id": "gpt-oss:120b-cloud", "name": "gpt-oss:120b-cloud",
        "contextWindow": openclaw.OPENCLAW_MODEL_MIN_CONTEXT_WINDOW,
    }]


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

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(returncode=0, stdout="hello\n")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
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

    def fake_popen(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _FakePopen(returncode=0, stdout="ok")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw._run_openclaw(["agents", "list", "--json"], openclaw_binary="openclaw")
    assert captured["kwargs"]["stdin"] == openclaw.subprocess.DEVNULL


def test_run_openclaw_timeout_surfaces_partial_output(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(timeout_then_output=("partial stdout", "partial stderr")),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="partial stdout") as exc_info:
        openclaw._run_openclaw(["agents", "list", "--json"], openclaw_binary="openclaw", timeout=15)
    assert "partial stderr" in str(exc_info.value)


def test_run_openclaw_timeout_kills_process_tree(monkeypatch):
    # A real field report: openclaw CLI invocations were piling up as
    # orphaned processes in Task Manager. subprocess.run's default
    # timeout handling only kills the immediate process it started; the
    # whole tree must be killed explicitly instead.
    killed = []
    monkeypatch.setattr(openclaw, "_kill_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(timeout_then_output=("", "")),
    )
    with pytest.raises(openclaw.OpenClawTimeoutError):
        openclaw._run_openclaw(["agents", "list", "--json"], openclaw_binary="openclaw", timeout=15)
    assert killed == [12345]


def test_run_openclaw_json_recovers_complete_output_from_a_timeout(monkeypatch):
    # Real field report: `openclaw agents list --json` was reported "timed
    # out after 15.0s" but the timeout's own partial-output capture showed
    # a complete, well-formed JSON array — the CLI's work was done and
    # correct, it just never exited the process on its own. Read-only
    # calls (_run_openclaw_json) must treat that as success, not failure.
    real_agents = [{"id": "main", "isDefault": True}, {"id": "autotask", "model": "ollama-cloud/deepseek-v4-pro"}]

    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(timeout_then_output=(json.dumps(real_agents), "[state-migrations] ...")),
    )
    result = openclaw._run_openclaw_json(["agents", "list", "--json"], openclaw_binary="openclaw", timeout=15)
    assert result == real_agents


def test_run_openclaw_json_timeout_with_incomplete_output_still_raises(monkeypatch):
    # A genuine hang (no complete JSON yet) must still surface as an error
    # rather than being swallowed by the recovery path above.
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(timeout_then_output=('[{"id": "main"', "")),
    )
    with pytest.raises(openclaw.OpenClawTimeoutError, match="timed out"):
        openclaw._run_openclaw_json(["agents", "list", "--json"], openclaw_binary="openclaw", timeout=15)


def test_list_agents_recovers_from_timeout_with_full_output(monkeypatch):
    real_agents = [{"id": "main"}, {"id": "autotask"}]

    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(timeout_then_output=(json.dumps(real_agents), "")),
    )
    assert openclaw.list_agents(openclaw_binary="openclaw", timeout=15) == real_agents


def test_config_set_verified_succeeds_when_readback_matches_after_timeout(monkeypatch):
    # config set prints no output to recover from directly — the fallback
    # is a follow-up config get compared against the intended value.
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["config", "set"]:
            return _FakePopen(timeout_then_output=("", ""))
        return _FakePopen(returncode=0, stdout=json.dumps(["read", "write"]))

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw._config_set_verified("agents.list[0].tools.allow", ["read", "write"], openclaw_binary="openclaw")
    assert len(calls) == 2
    assert calls[0][1:3] == ["config", "set"]
    assert calls[1][1:3] == ["config", "get"]


def test_config_set_verified_raises_when_readback_does_not_match(monkeypatch):
    def fake_popen(cmd, **kwargs):
        if cmd[1:3] == ["config", "set"]:
            return _FakePopen(timeout_then_output=("", ""))
        return _FakePopen(returncode=0, stdout=json.dumps(["something-else"]))

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    with pytest.raises(openclaw.OpenClawTimeoutError):
        openclaw._config_set_verified("agents.list[0].tools.allow", ["read", "write"], openclaw_binary="openclaw")


def test_config_set_verified_no_readback_needed_on_normal_success(monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw._config_set_verified("agents.list[0].tools.allow", ["read"], openclaw_binary="openclaw")
    assert len(calls) == 1


def test_run_openclaw_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="boom"):
        openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw")


def test_run_openclaw_none_stdout_and_stderr_do_not_crash(monkeypatch):
    # Same defensive fix applied to goose.run_agent_task after a real field
    # report — guard here from the start rather than waiting for a repeat.
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=1, stdout=None, stderr=None),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="exited with status 1"):
        openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw")

    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=0, stdout=None, stderr=""),
    )
    assert openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw") == ""


def test_run_openclaw_missing_binary_raises(monkeypatch):
    def fake_popen(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    with pytest.raises(openclaw.OpenClawCliError, match="not found"):
        openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw")


def test_run_openclaw_timeout_raises(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(timeout_then_output=("", "")),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="timed out"):
        openclaw._run_openclaw(["agents", "list"], openclaw_binary="openclaw", timeout=5)


def test_run_openclaw_json_parses_output(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=0, stdout='{"a": 1}\n'),
    )
    assert openclaw._run_openclaw_json(["config", "get", "x"], openclaw_binary="openclaw") == {"a": 1}


def test_run_openclaw_json_empty_output_is_none(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=0, stdout=""),
    )
    assert openclaw._run_openclaw_json(["config", "get", "x"], openclaw_binary="openclaw") is None


def test_run_openclaw_json_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=0, stdout="not json"),
    )
    with pytest.raises(openclaw.OpenClawCliError, match="non-JSON"):
        openclaw._run_openclaw_json(["config", "get", "x"], openclaw_binary="openclaw")


def test_list_agents_unwraps_agents_key(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=0, stdout=json.dumps({"agents": [{"id": "boss"}]})),
    )
    assert openclaw.list_agents(openclaw_binary="openclaw") == [{"id": "boss"}]


def test_list_agents_accepts_bare_list(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=0, stdout=json.dumps([{"id": "boss"}])),
    )
    assert openclaw.list_agents(openclaw_binary="openclaw") == [{"id": "boss"}]


def test_list_agents_empty_output_returns_empty_list(monkeypatch):
    monkeypatch.setattr(openclaw.subprocess, "Popen", lambda *a, **k: _FakePopen(returncode=0, stdout=""))
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

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(returncode=0, stdout=json.dumps({"id": "boss", "tools": {"allow": ["read"]}}))

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    result = openclaw.get_agent_config("boss", openclaw_binary="openclaw")
    assert result == {"id": "boss", "tools": {"allow": ["read"]}}
    assert captured["cmd"] == ["openclaw", "config", "get", "agents.list[2]", "--json"]


def test_set_agent_tools_only_writes_provided_fields(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 0)
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.set_agent_tools("boss", allow=["read", "write"], openclaw_binary="openclaw")
    assert len(calls) == 1
    assert calls[0] == [
        "openclaw", "config", "set", "agents.list[0].tools.allow", json.dumps(["read", "write"]), "--strict-json",
    ]


def test_set_agent_tools_writes_allow_and_deny_separately(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 0)
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.set_agent_tools("boss", allow=["read"], deny=["browser"], openclaw_binary="openclaw")
    paths = [c[3] for c in calls]
    assert paths == ["agents.list[0].tools.allow", "agents.list[0].tools.deny"]


def test_set_agent_filesystem_binds(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 3)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.set_agent_filesystem_binds("boss", ["/data:/data:ro"], openclaw_binary="openclaw")
    assert captured["cmd"][3] == "agents.list[3].sandbox.docker.binds"
    assert json.loads(captured["cmd"][4]) == ["/data:/data:ro"]


def test_set_agent_sandbox_writes_only_given_fields(monkeypatch):
    monkeypatch.setattr(openclaw, "_agent_index", lambda agent_id, **k: 0)
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.set_agent_sandbox("boss", network="none", openclaw_binary="openclaw")
    assert len(calls) == 1
    assert calls[0][3] == "agents.list[0].sandbox.docker.network"
    assert json.loads(calls[0][4]) == "none"


def test_get_website_allowlist_defaults_to_empty_list(monkeypatch):
    monkeypatch.setattr(openclaw.subprocess, "Popen", lambda *a, **k: _FakePopen(returncode=0, stdout=""))
    assert openclaw.get_website_allowlist(openclaw_binary="openclaw") == []


def test_set_website_allowlist_writes_global_path(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.set_website_allowlist(["example.com"], openclaw_binary="openclaw")
    assert captured["cmd"] == [
        "openclaw", "config", "set", "browser.ssrfPolicy.hostnameAllowlist", json.dumps(["example.com"]), "--strict-json",
    ]


def test_tool_catalog_groups_cover_expected_tools():
    assert "browser" in openclaw.TOOL_CATALOG["Web access"]
    assert "read" in openclaw.TOOL_CATALOG["File access"]
    assert "write" in openclaw.TOOL_CATALOG["File access"]


# ---------------------------------------------------------------- Telegram


def test_set_telegram_dm_policy_pairing(monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.set_telegram_dm_policy("pairing", openclaw_binary="openclaw")
    dm_policy_call = next(c for c in calls if c[1:3] == ["config", "set"] and c[3] == "channels.telegram.dmPolicy")
    assert dm_policy_call[4] == json.dumps("pairing")
    assert not any(c[3] == "channels.telegram.allowFrom" for c in calls if c[1:3] == ["config", "set"])


def test_set_telegram_dm_policy_open_sets_allow_from_wildcard(monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.set_telegram_dm_policy("open", openclaw_binary="openclaw")
    dm_policy_call = next(c for c in calls if c[1:3] == ["config", "set"] and c[3] == "channels.telegram.dmPolicy")
    allow_from_call = next(c for c in calls if c[1:3] == ["config", "set"] and c[3] == "channels.telegram.allowFrom")
    assert dm_policy_call[4] == json.dumps("open")
    assert allow_from_call[4] == json.dumps(["*"])


def test_set_telegram_dm_policy_rejects_invalid_value():
    with pytest.raises(ValueError, match="dm_policy"):
        openclaw.set_telegram_dm_policy("whatever")


def test_get_telegram_status_configured_with_dm_policy(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(
            returncode=0, stdout=json.dumps({"enabled": True, "botToken": "sk-xxx", "dmPolicy": "open"})
        ),
    )
    assert openclaw.get_telegram_status(openclaw_binary="openclaw") == {"configured": True, "dm_policy": "open"}


def test_get_telegram_status_not_configured(monkeypatch):
    monkeypatch.setattr(openclaw.subprocess, "Popen", lambda *a, **k: _FakePopen(returncode=0, stdout=""))
    assert openclaw.get_telegram_status(openclaw_binary="openclaw") == {"configured": False, "dm_policy": "pairing"}


def test_get_telegram_status_defaults_dm_policy_to_pairing_when_configured_but_unset(monkeypatch):
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(returncode=0, stdout=json.dumps({"botToken": "sk-xxx"})),
    )
    assert openclaw.get_telegram_status(openclaw_binary="openclaw") == {"configured": True, "dm_policy": "pairing"}




# ---------------------------------------------------------------- multiple Telegram bots


@pytest.fixture
def telegram_bots_path(tmp_path, monkeypatch):
    path = tmp_path / "openclaw-telegram-bots.yaml"
    monkeypatch.setattr(openclaw.paths, "telegram_bots_path", lambda: path)
    return path


def test_load_telegram_bot_names_missing_file_returns_empty(telegram_bots_path):
    assert openclaw.load_telegram_bot_names() == {}


def test_save_and_load_telegram_bot_names_round_trip(telegram_bots_path):
    openclaw.save_telegram_bot_names({"bot-one": "Sales Bot"})
    assert openclaw.load_telegram_bot_names() == {"bot-one": "Sales Bot"}


def test_save_telegram_bot_names_backs_up_existing_file(telegram_bots_path):
    openclaw.save_telegram_bot_names({"bot-one": "Sales Bot"})
    openclaw.save_telegram_bot_names({"bot-one": "Sales Bot", "bot-two": "Support Bot"})
    backups = list(telegram_bots_path.parent.glob("*.bak-*"))
    assert len(backups) == 1


def test_list_telegram_bots_uses_real_channels_list_shape(monkeypatch, telegram_bots_path):
    # Real, verified output from `openclaw channels list --json`:
    # {"chat": {"telegram": {"accounts": ["default"], "installed": true,
    # "origin": "configured"}}} — bare account-id strings, no name/token.
    monkeypatch.setattr(
        openclaw.subprocess, "Popen",
        lambda *a, **k: _FakePopen(
            returncode=0,
            stdout=json.dumps({"chat": {"telegram": {"accounts": ["default", "bot-two"], "installed": True, "origin": "configured"}}}),
        ),
    )
    openclaw.save_telegram_bot_names({"bot-two": "Support Bot"})
    result = openclaw.list_telegram_bots(openclaw_binary="openclaw")
    assert result == [{"id": "default", "name": "default"}, {"id": "bot-two", "name": "Support Bot"}]


def test_list_telegram_bots_no_telegram_channel_configured(monkeypatch, telegram_bots_path):
    monkeypatch.setattr(openclaw.subprocess, "Popen", lambda *a, **k: _FakePopen(returncode=0, stdout=""))
    assert openclaw.list_telegram_bots(openclaw_binary="openclaw") == []


def test_add_or_update_telegram_bot_slugifies_name_and_saves_locally(monkeypatch, telegram_bots_path):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    account_id = openclaw.add_or_update_telegram_bot("Support Bot", "sk-fake-token", openclaw_binary="openclaw")
    assert account_id == "support-bot"
    assert captured["cmd"] == [
        "openclaw", "channels", "add", "--channel", "telegram",
        "--account", "support-bot", "--name", "Support Bot", "--token", "sk-fake-token",
    ]
    assert openclaw.load_telegram_bot_names() == {"support-bot": "Support Bot"}


def test_add_or_update_telegram_bot_reuses_explicit_account_id_to_update(monkeypatch, telegram_bots_path):
    # Confirmed via `openclaw channels add --help`: the command is
    # "Add or update a channel account" — re-running it with the same
    # --account id replaces the token in place, no separate rotate call.
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.add_or_update_telegram_bot("Support Bot", "sk-old-token", account_id="support-bot", openclaw_binary="openclaw")
    openclaw.add_or_update_telegram_bot("Support Bot", "sk-new-token", account_id="support-bot", openclaw_binary="openclaw")
    assert len(calls) == 2
    assert calls[0][-1] == "sk-old-token"
    assert calls[1][-1] == "sk-new-token"
    assert calls[0][calls[0].index("--account") + 1] == calls[1][calls[1].index("--account") + 1] == "support-bot"


def test_add_or_update_telegram_bot_recovers_from_timeout_when_bot_appears_in_list(monkeypatch, telegram_bots_path):
    # Real field report: this call reported a timeout on *every* add,
    # because unlike _config_set_verified it never checked whether the
    # underlying `channels add` had actually succeeded — the same
    # "does the work, doesn't exit cleanly" CLI quirk documented on
    # _run_openclaw.
    def fake_popen(cmd, **kwargs):
        if cmd[1:3] == ["channels", "add"]:
            return _FakePopen(timeout_then_output=("", ""))
        return _FakePopen(returncode=0, stdout=json.dumps({"chat": {"telegram": {"accounts": ["support-bot"]}}}))

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    account_id = openclaw.add_or_update_telegram_bot("Support Bot", "sk-fake-token", openclaw_binary="openclaw")
    assert account_id == "support-bot"
    assert openclaw.load_telegram_bot_names() == {"support-bot": "Support Bot"}


def test_add_or_update_telegram_bot_reraises_timeout_when_bot_missing_from_list(monkeypatch, telegram_bots_path):
    def fake_popen(cmd, **kwargs):
        if cmd[1:3] == ["channels", "add"]:
            return _FakePopen(timeout_then_output=("", ""))
        return _FakePopen(returncode=0, stdout=json.dumps({"chat": {"telegram": {"accounts": []}}}))

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    with pytest.raises(openclaw.OpenClawTimeoutError):
        openclaw.add_or_update_telegram_bot("Support Bot", "sk-fake-token", openclaw_binary="openclaw")
    assert openclaw.load_telegram_bot_names() == {}


def test_remove_telegram_bot_deletes_config_and_local_name(monkeypatch, telegram_bots_path):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.save_telegram_bot_names({"support-bot": "Support Bot", "sales-bot": "Sales Bot"})
    openclaw.remove_telegram_bot("support-bot", openclaw_binary="openclaw")
    assert captured["cmd"] == [
        "openclaw", "channels", "remove", "--channel", "telegram", "--account", "support-bot", "--delete",
    ]
    assert openclaw.load_telegram_bot_names() == {"sales-bot": "Sales Bot"}


def test_remove_telegram_bot_recovers_from_timeout_when_bot_gone_from_list(monkeypatch, telegram_bots_path):
    def fake_popen(cmd, **kwargs):
        if cmd[1:3] == ["channels", "remove"]:
            return _FakePopen(timeout_then_output=("", ""))
        return _FakePopen(returncode=0, stdout=json.dumps({"chat": {"telegram": {"accounts": []}}}))

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.save_telegram_bot_names({"support-bot": "Support Bot"})
    openclaw.remove_telegram_bot("support-bot", openclaw_binary="openclaw")
    assert openclaw.load_telegram_bot_names() == {}


def test_remove_telegram_bot_reraises_timeout_when_bot_still_in_list(monkeypatch, telegram_bots_path):
    def fake_popen(cmd, **kwargs):
        if cmd[1:3] == ["channels", "remove"]:
            return _FakePopen(timeout_then_output=("", ""))
        return _FakePopen(returncode=0, stdout=json.dumps({"chat": {"telegram": {"accounts": ["support-bot"]}}}))

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.save_telegram_bot_names({"support-bot": "Support Bot"})
    with pytest.raises(openclaw.OpenClawTimeoutError):
        openclaw.remove_telegram_bot("support-bot", openclaw_binary="openclaw")
    assert openclaw.load_telegram_bot_names() == {"support-bot": "Support Bot"}


def test_remove_telegram_bot_missing_local_name_does_not_error(monkeypatch, telegram_bots_path):
    monkeypatch.setattr(openclaw.subprocess, "Popen", lambda *a, **k: _FakePopen(returncode=0, stdout=""))
    openclaw.remove_telegram_bot("never-named", openclaw_binary="openclaw")  # must not raise


def test_bind_agent_to_telegram_account_targets_specific_account(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(returncode=0, stdout="")

    monkeypatch.setattr(openclaw.subprocess, "Popen", fake_popen)
    openclaw.bind_agent_to_telegram_account("boss", "support-bot", openclaw_binary="openclaw")
    assert captured["cmd"] == ["openclaw", "agents", "bind", "--agent", "boss", "--bind", "telegram:support-bot"]
