import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

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
