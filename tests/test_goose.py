import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from agenticiam import goose


def test_slugify():
    assert goose.slugify("My Cool Agent!!") == "my-cool-agent"
    assert goose.slugify("  ") == "agent"
    assert goose.slugify("already-slug") == "already-slug"


def test_config_path_posix():
    assert str(goose._config_path_for(False, {})).endswith(".config/goose/config.yaml")


def test_config_path_windows(tmp_path):
    result = goose._config_path_for(True, {"APPDATA": str(tmp_path)})
    assert result == tmp_path / "Block" / "goose" / "config" / "config.yaml"


def test_config_path_windows_falls_back_without_appdata():
    result = goose._config_path_for(True, {})
    assert result.parts[-4:] == ("Block", "goose", "config", "config.yaml")


def test_register_extension_adds_without_clobbering_existing():
    cfg = {"GOOSE_PROVIDER": "anthropic", "extensions": {"other": {"name": "Other"}}}
    result = goose.register_extension(cfg, "my-agent", "My Agent", "agenticiam", ["mcp"], "tok123")
    assert result["GOOSE_PROVIDER"] == "anthropic"
    assert result["extensions"]["other"] == {"name": "Other"}
    entry = result["extensions"]["my-agent"]
    assert entry["cmd"] == "agenticiam"
    assert entry["args"] == ["mcp"]
    assert entry["envs"] == {"AGENTICIAM_TOKEN": "tok123"}
    assert entry["type"] == "stdio"
    assert entry["enabled"] is True
    # original dict is untouched
    assert "my-agent" not in cfg.get("extensions", {})


def test_set_default_provider_model():
    result = goose.set_default_provider_model({"extensions": {}}, "ollama", "llama3.1:8b")
    assert result["GOOSE_PROVIDER"] == "ollama"
    assert result["GOOSE_MODEL"] == "llama3.1:8b"


def test_load_config_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(goose, "config_path", lambda: tmp_path / "nope" / "config.yaml")
    assert goose.load_config() == {}


def test_save_and_load_config_roundtrip_with_backup(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(goose, "config_path", lambda: path)

    goose.save_config({"GOOSE_PROVIDER": "ollama", "GOOSE_MODEL": "llama3.1:8b"})
    assert goose.load_config()["GOOSE_MODEL"] == "llama3.1:8b"
    assert not list(tmp_path.glob("*.bak-*"))  # no prior file, nothing to back up

    goose.save_config({"GOOSE_PROVIDER": "ollama", "GOOSE_MODEL": "llama3.2:3b"})
    assert goose.load_config()["GOOSE_MODEL"] == "llama3.2:3b"
    assert list(tmp_path.glob("*.bak-*"))  # second write backs up the first


def test_extension_snippet_yaml_is_valid_yaml():
    import yaml

    snippet = goose.extension_snippet_yaml("my-agent", "My Agent", "agenticiam", ["mcp"], "tok123")
    parsed = yaml.safe_load(snippet)
    assert parsed["extensions"]["my-agent"]["envs"]["AGENTICIAM_TOKEN"] == "tok123"


def test_list_ollama_models_unreachable(monkeypatch):
    import urllib.error

    def fail(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(goose.urllib.request, "urlopen", fail)
    with pytest.raises(goose.OllamaUnavailable):
        goose.list_ollama_models()


class _FakeOllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": "llama3.1:8b"}, {"name": "phi4:latest"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture
def fake_ollama_server(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(goose, "OLLAMA_API_BASE", f"http://127.0.0.1:{port}")
    yield
    server.shutdown()
    thread.join(timeout=2)


def test_list_ollama_models_happy_path(fake_ollama_server):
    models = goose.list_ollama_models()
    assert models == ["llama3.1:8b", "phi4:latest"]


def test_list_anthropic_models_requires_key():
    with pytest.raises(goose.ProviderUnavailable):
        goose.list_anthropic_models(None)


def test_list_anthropic_models_parses_response(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "claude-sonnet-5"}, {"id": "claude-opus-4-8"}]}).encode()

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        captured["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr(goose.urllib.request, "urlopen", fake_urlopen)
    models = goose.list_anthropic_models("sk-ant-fake")
    assert models == ["claude-opus-4-8", "claude-sonnet-5"]
    assert captured["headers"]["X-api-key"] == "sk-ant-fake"


def test_list_ollama_cloud_models_requires_key():
    with pytest.raises(goose.ProviderUnavailable):
        goose.list_ollama_cloud_models(None)


def test_list_ollama_cloud_models_parses_response(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"models": [{"name": "gpt-oss:120b-cloud"}, {"name": "qwen3-coder:480b-cloud"}]}).encode()

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        captured["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr(goose.urllib.request, "urlopen", fake_urlopen)
    models = goose.list_ollama_cloud_models("fake-cloud-key")
    assert models == ["gpt-oss:120b-cloud", "qwen3-coder:480b-cloud"]
    assert captured["headers"]["Authorization"] == "Bearer fake-cloud-key"
    assert captured["url"] == "https://ollama.com/api/tags"


def test_list_google_models_requires_key():
    with pytest.raises(goose.ProviderUnavailable):
        goose.list_google_models(None)


def test_list_google_models_parses_response(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"models": [{"name": "models/gemini-3-pro"}, {"name": "models/gemini-3-flash"}]}).encode()

    monkeypatch.setattr(goose.urllib.request, "urlopen", lambda url, timeout=None: FakeResponse())
    models = goose.list_google_models("fake-key")
    assert models == ["gemini-3-flash", "gemini-3-pro"]


def test_list_provider_models_dispatches(monkeypatch):
    monkeypatch.setattr(goose, "list_ollama_models", lambda timeout=2.0: ["a"])
    monkeypatch.setattr(goose, "list_ollama_cloud_models", lambda api_key, timeout=5.0: ["a-cloud"])
    monkeypatch.setattr(goose, "list_anthropic_models", lambda api_key, timeout=5.0: ["b"])
    monkeypatch.setattr(goose, "list_google_models", lambda api_key, timeout=5.0: ["c"])
    assert goose.list_provider_models("ollama") == ["a"]
    assert goose.list_provider_models("ollama_cloud", api_key="k") == ["a-cloud"]
    assert goose.list_provider_models("anthropic", api_key="k") == ["b"]
    assert goose.list_provider_models("google", api_key="k") == ["c"]
    with pytest.raises(ValueError):
        goose.list_provider_models("nonsense")


def test_launch_commands_no_overrides():
    commands = goose.launch_commands("bot")
    assert commands["bash"] == "goose session -n bot"
    assert commands["powershell"] == "goose session -n bot"
    assert commands["cmd"] == "goose session -n bot"


def test_launch_commands_with_model():
    commands = goose.launch_commands("bot", "ollama", "llama3.1:8b")
    assert commands["bash"] == "GOOSE_PROVIDER=ollama GOOSE_MODEL=llama3.1:8b goose session -n bot"
    assert commands["powershell"] == '$env:GOOSE_PROVIDER="ollama"; $env:GOOSE_MODEL="llama3.1:8b"; goose session -n bot'
    assert commands["cmd"] == 'set "GOOSE_PROVIDER=ollama" && set "GOOSE_MODEL=llama3.1:8b" && goose session -n bot'


def test_launch_commands_context_limit_ollama_sets_both_vars():
    commands = goose.launch_commands("bot", "ollama", "llama3.1:8b", context_limit=32000)
    assert "GOOSE_CONTEXT_LIMIT=32000" in commands["bash"]
    assert "GOOSE_INPUT_LIMIT=32000" in commands["bash"]


def test_launch_commands_context_limit_non_ollama_skips_input_limit():
    commands = goose.launch_commands("bot", "anthropic", "claude-sonnet-5", context_limit=32000)
    assert "GOOSE_CONTEXT_LIMIT=32000" in commands["bash"]
    assert "GOOSE_INPUT_LIMIT" not in commands["bash"]


def test_launch_commands_api_key_is_quoted_and_never_bare():
    commands = goose.launch_commands("bot", "anthropic", "claude-sonnet-5", api_key="sk-ant-secret")
    assert "ANTHROPIC_API_KEY='sk-ant-secret'" in commands["bash"]
    assert "$env:ANTHROPIC_API_KEY='sk-ant-secret'" in commands["powershell"]
    assert 'set "ANTHROPIC_API_KEY=sk-ant-secret"' in commands["cmd"]


def test_launch_commands_ollama_ignores_api_key():
    # ollama has no PROVIDER_API_KEY_ENV entry, so a stray key must never appear
    commands = goose.launch_commands("bot", "ollama", "llama3.1:8b", api_key="should-not-appear")
    for variant in commands.values():
        assert "should-not-appear" not in variant


def test_ollama_install_commands_has_all_shells():
    commands = goose.ollama_install_commands()
    assert "ollama.com/install.sh" in commands["bash"]
    assert "Ollama.Ollama" in commands["powershell"]
    assert "Ollama.Ollama" in commands["cmd"]


def test_goose_install_commands_has_all_shells():
    commands = goose.goose_install_commands()
    assert "download_cli.sh" in commands["bash"]
    assert "CONFIGURE=false" in commands["bash"]
    assert "download_cli.ps1" in commands["powershell"]
    assert "cmd" in commands


def test_launch_commands_with_recipe_path_uses_goose_run_interactive():
    commands = goose.launch_commands("boss", recipe_path="/home/user/.config/goose/agenticiam-recipes/boss.yaml")
    for shell in ("bash", "powershell"):
        assert commands[shell] == "goose run --recipe '/home/user/.config/goose/agenticiam-recipes/boss.yaml' --interactive -n boss"
    assert commands["cmd"] == 'goose run --recipe "/home/user/.config/goose/agenticiam-recipes/boss.yaml" --interactive -n boss'


def test_launch_commands_with_recipe_path_and_model_still_sets_env_vars():
    commands = goose.launch_commands("boss", "ollama", "llama3.1:8b", recipe_path="/x/boss.yaml")
    assert commands["bash"] == "GOOSE_PROVIDER=ollama GOOSE_MODEL=llama3.1:8b goose run --recipe '/x/boss.yaml' --interactive -n boss"


def test_launch_commands_ollama_cloud_uses_provider_model_flags_not_env_vars():
    commands = goose.launch_commands("boss", "ollama_cloud", "gpt-oss:120b-cloud")
    assert commands["bash"] == "goose run --provider ollama_cloud --model 'gpt-oss:120b-cloud' -t 'Hello' --interactive -n boss"
    assert commands["powershell"] == "goose run --provider ollama_cloud --model 'gpt-oss:120b-cloud' -t 'Hello' --interactive -n boss"
    assert commands["cmd"] == 'goose run --provider ollama_cloud --model "gpt-oss:120b-cloud" -t "Hello" --interactive -n boss'
    for variant in commands.values():
        assert "GOOSE_PROVIDER" not in variant
        assert "GOOSE_MODEL" not in variant


def test_launch_commands_ollama_cloud_never_leaks_api_key_via_env():
    commands = goose.launch_commands("boss", "ollama_cloud", "gpt-oss:120b-cloud", api_key="should-never-appear")
    for variant in commands.values():
        assert "should-never-appear" not in variant


def test_launch_commands_ollama_cloud_recipe_path_still_takes_priority():
    # a manager's recipe launch command must win even for ollama_cloud
    commands = goose.launch_commands("boss", "ollama_cloud", "gpt-oss:120b-cloud", recipe_path="/x/boss.yaml")
    assert "--recipe '/x/boss.yaml'" in commands["bash"]
    assert "--provider ollama_cloud" not in commands["bash"]


def test_launch_commands_ollama_cloud_auto_configure_adds_disable_keyring_env():
    commands = goose.launch_commands("boss", "ollama_cloud", "gpt-oss:120b-cloud", auto_configure=True)
    assert commands["bash"] == "GOOSE_DISABLE_KEYRING=1 goose run --provider ollama_cloud --model 'gpt-oss:120b-cloud' -t 'Hello' --interactive -n boss"
    assert '$env:GOOSE_DISABLE_KEYRING="1"' in commands["powershell"]
    assert 'set "GOOSE_DISABLE_KEYRING=1"' in commands["cmd"]


def test_launch_commands_provider_flags_branch_always_includes_starting_text():
    # regression test: `goose run` rejects `--interactive` on its own with
    # "Must provide either --instructions (-i), --text (-t), or --recipe" —
    # confirmed against a real `goose run` invocation, not just docs. Any
    # future addition to GOOSE_ENV_UNCONFIGURABLE_PROVIDERS must keep -t
    # present in this branch or it'll generate a command Goose rejects.
    commands = goose.launch_commands("boss", "ollama_cloud", "some-model")
    for variant in commands.values():
        assert " -t " in variant or ' -t "' in variant


def test_launch_commands_auto_configure_ignored_for_normal_providers():
    # auto_configure only matters for GOOSE_ENV_UNCONFIGURABLE_PROVIDERS
    commands = goose.launch_commands("bot", "ollama", "llama3.1:8b", auto_configure=True)
    for variant in commands.values():
        assert "GOOSE_DISABLE_KEYRING" not in variant


def test_secrets_path_is_sibling_of_config_path(monkeypatch):
    monkeypatch.setattr(goose, "config_path", lambda: Path("/x/goose/config.yaml"))
    assert goose.secrets_path() == Path("/x/goose/secrets.yaml")


def test_load_secrets_missing_file_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(goose, "secrets_path", lambda: tmp_path / "secrets.yaml")
    assert goose.load_secrets() == {}


def test_write_ollama_cloud_secret_creates_and_merges(tmp_path, monkeypatch):
    path = tmp_path / "secrets.yaml"
    monkeypatch.setattr(goose, "secrets_path", lambda: path)

    goose.save_secrets({"SOME_OTHER_KEY": "unrelated-value"})
    written = goose.write_ollama_cloud_secret("sk-fake-cloud-key")
    assert written == path

    result = goose.load_secrets()
    assert result["SOME_OTHER_KEY"] == "unrelated-value"  # existing secrets preserved
    assert result[goose.OLLAMA_CLOUD_API_KEY_ENV] == "sk-fake-cloud-key"


def test_save_secrets_backs_up_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.yaml"
    monkeypatch.setattr(goose, "secrets_path", lambda: path)
    goose.save_secrets({"A": "1"})
    goose.save_secrets({"A": "2"})
    backups = list(tmp_path.glob("secrets.yaml.bak-*"))
    assert len(backups) == 1


def test_manager_recipe_yaml_has_required_fields_and_prompt_body():
    rendered = goose.manager_recipe_yaml("boss")
    parsed = yaml.safe_load(rendered)
    assert parsed["version"] == "1.0.0"
    assert parsed["title"] == "boss (manager)"
    assert "description" in parsed
    assert parsed["instructions"] == goose.MANAGER_SYSTEM_PROMPT
    # required by block/goose's recipe schema: at least one of instructions/prompt
    assert parsed["instructions"]
    assert "iamDispatchToAgent" in parsed["instructions"]
    assert "ollama" in parsed["instructions"]


def test_write_manager_recipe_writes_file_under_recipes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(goose, "config_path", lambda: tmp_path / "goose" / "config.yaml")
    path = goose.write_manager_recipe("Marketing Boss")
    assert path == tmp_path / "goose" / "agenticiam-recipes" / "marketing-boss.yaml"
    assert path.exists()
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["title"] == "Marketing Boss (manager)"


def test_write_manager_recipe_overwrites_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(goose, "config_path", lambda: tmp_path / "goose" / "config.yaml")
    goose.write_manager_recipe("boss")
    path = goose.write_manager_recipe("boss")
    assert path.read_text(encoding="utf-8").count("version:") == 1


def test_manager_recipe_yaml_without_provider_model_has_no_settings_block():
    # matches the original behavior when a manager was created with no model picked
    parsed = yaml.safe_load(goose.manager_recipe_yaml("boss"))
    assert "settings" not in parsed


def test_manager_recipe_yaml_embeds_settings_so_it_does_not_silently_use_the_wrong_model():
    # regression test: a recipe with no settings block falls back to
    # Goose's own global default provider/model instead of the one this
    # manager was actually created with — confirmed from a real report
    # where `goose run --recipe ...` launched with a completely different
    # model than the agent was configured with.
    parsed = yaml.safe_load(goose.manager_recipe_yaml("boss", provider="ollama_cloud", model="gemini-3-flash-preview"))
    assert parsed["settings"] == {"goose_provider": "ollama_cloud", "goose_model": "gemini-3-flash-preview"}


def test_write_manager_recipe_passes_through_provider_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr(goose, "config_path", lambda: tmp_path / "goose" / "config.yaml")
    path = goose.write_manager_recipe("boss", provider="anthropic", model="claude-sonnet-5")
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["settings"] == {"goose_provider": "anthropic", "goose_model": "claude-sonnet-5"}


def test_set_default_provider_model_with_context_limit_ollama():
    result = goose.set_default_provider_model({}, "ollama", "llama3.1:8b", context_limit=16000)
    assert result["GOOSE_CONTEXT_LIMIT"] == 16000
    assert result["GOOSE_INPUT_LIMIT"] == 16000


def test_set_default_provider_model_with_context_limit_non_ollama():
    result = goose.set_default_provider_model({}, "anthropic", "claude-sonnet-5", context_limit=16000)
    assert result["GOOSE_CONTEXT_LIMIT"] == 16000
    assert "GOOSE_INPUT_LIMIT" not in result


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_agent_task_builds_expected_command(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return _FakeCompletedProcess(returncode=0, stdout="  the answer  \n")

    monkeypatch.setattr(goose.subprocess, "run", fake_run)
    result = goose.run_agent_task("ollama", "llama3.1:8b", "summarize this", timeout=30, goose_binary="goose")
    assert result == "the answer"
    assert captured["cmd"] == ["goose", "run", "--no-session", "--provider", "ollama", "--model", "llama3.1:8b", "-t", "summarize this"]
    assert captured["timeout"] == 30


def test_run_agent_task_no_window_suppression_kwarg_on_posix(monkeypatch):
    """On Windows this should add creationflags=CREATE_NO_WINDOW so
    dispatch doesn't pop a visible console (a real reported bug — Windows
    allocates one for a console-subsystem child when the parent, e.g.
    agenticiam-gui.exe, has none of its own). That branch can't be safely
    exercised in this Linux sandbox: subprocess.CREATE_NO_WINDOW doesn't
    exist here, and mutating os.name globally caused a real pytest crash
    earlier in this project (see test_config_path_windows's history) by
    interfering with pathlib's platform detection mid-suite. This test
    instead locks in the current, verifiable platform's behavior: no
    creationflags kwarg reaches subprocess.run at all."""
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(returncode=0, stdout="ok")

    monkeypatch.setattr(goose.subprocess, "run", fake_run)
    goose.run_agent_task("ollama", "llama3.1:8b", "task", goose_binary="goose")
    assert captured["kwargs"] == {}


def test_run_agent_task_disable_keyring_sets_scoped_env_var(monkeypatch):
    # doesn't mutate the real os.environ (a prior lesson in this project:
    # mutating global process state like os.name mid-suite caused a real
    # pytest crash) — just checks the env kwarg passed to subprocess.run
    # is a merge of the real environment plus the one new key.
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(returncode=0, stdout="ok")

    monkeypatch.setattr(goose.subprocess, "run", fake_run)
    goose.run_agent_task("ollama_cloud", "gpt-oss:120b-cloud", "task", goose_binary="goose", disable_keyring=True)
    passed_env = captured["kwargs"]["env"]
    assert passed_env["GOOSE_DISABLE_KEYRING"] == "1"
    assert passed_env["PATH"] == goose.os.environ["PATH"]  # the real environment is still there, not replaced
    # the parent AgenticIAM process's own environment is never mutated
    assert "GOOSE_DISABLE_KEYRING" not in goose.os.environ


def test_run_agent_task_disable_keyring_false_omits_env_kwarg(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(returncode=0, stdout="ok")

    monkeypatch.setattr(goose.subprocess, "run", fake_run)
    goose.run_agent_task("ollama", "llama3.1:8b", "task", goose_binary="goose", disable_keyring=False)
    assert "env" not in captured["kwargs"]


def test_dispatchable_providers_includes_ollama_and_ollama_cloud_only():
    assert goose.DISPATCHABLE_PROVIDERS == {"ollama", "ollama_cloud"}


def test_run_agent_task_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(
        goose.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout="", stderr="model not found"),
    )
    with pytest.raises(goose.DispatchError, match="model not found"):
        goose.run_agent_task("ollama", "nope", "task")


def test_run_agent_task_none_stdout_raises_dispatch_error_instead_of_crashing(monkeypatch):
    # subprocess.run(capture_output=True, text=True) is documented to always
    # return a str for stdout, but this has been reported in the field as an
    # unhandled AttributeError ('NoneType' object has no attribute 'strip').
    # Whatever the platform-specific cause, a None stdout must surface as a
    # clear DispatchError, never an unhandled crash.
    monkeypatch.setattr(
        goose.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=None, stderr=""),
    )
    with pytest.raises(goose.DispatchError, match="no captured stdout"):
        goose.run_agent_task("ollama", "llama3.1:8b", "task")


def test_run_agent_task_none_stderr_on_nonzero_exit_raises_dispatch_error(monkeypatch):
    monkeypatch.setattr(
        goose.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout="", stderr=None),
    )
    with pytest.raises(goose.DispatchError, match="exited with status 1"):
        goose.run_agent_task("ollama", "llama3.1:8b", "task")


def test_run_agent_task_missing_binary_raises(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(goose.subprocess, "run", fake_run)
    with pytest.raises(goose.DispatchError, match="not found"):
        goose.run_agent_task("ollama", "llama3.1:8b", "task", goose_binary="goose")


def test_run_agent_task_timeout_raises(monkeypatch):
    def fake_run(*a, **k):
        raise goose.subprocess.TimeoutExpired(cmd="goose", timeout=5)

    monkeypatch.setattr(goose.subprocess, "run", fake_run)
    with pytest.raises(goose.DispatchError, match="timed out"):
        goose.run_agent_task("ollama", "llama3.1:8b", "task", timeout=5)


def test_run_agent_task_timeout_surfaces_partial_output(monkeypatch):
    def fake_run(*a, **k):
        exc = goose.subprocess.TimeoutExpired(cmd="goose", timeout=5)
        exc.stdout = "partial response so far..."
        exc.stderr = "some warning on stderr"
        raise exc

    monkeypatch.setattr(goose.subprocess, "run", fake_run)
    with pytest.raises(goose.DispatchError) as excinfo:
        goose.run_agent_task("ollama", "llama3.1:8b", "task", timeout=5)
    assert "partial response so far" in str(excinfo.value)
    assert "some warning on stderr" in str(excinfo.value)


def test_run_agent_task_default_timeout_matches_extension_timeout():
    assert goose.DEFAULT_DISPATCH_TIMEOUT == 300.0
