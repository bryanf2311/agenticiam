import pytest

from agenticiam import hardware


def test_requires_positive_ram():
    with pytest.raises(ValueError):
        hardware.recommend_models(0)
    with pytest.raises(ValueError):
        hardware.recommend_models(-5)


def test_small_gpu_puts_7b_models_in_fast_tier():
    result = hardware.recommend_models(ram_gb=64, vram_gb=8)
    fast_ids = {m["id"] for m in result["fast"]}
    assert "llama3.1:8b" in fast_ids
    assert "qwen2.5-coder:7b" in fast_ids
    # 32B is far too big for 8GB VRAM to be "fast"
    assert "qwen2.5:32b" not in fast_ids


def test_large_ram_allows_bigger_models_in_usable_tier():
    result = hardware.recommend_models(ram_gb=64, vram_gb=8)
    usable_ids = {m["id"] for m in result["usable"]}
    assert "qwen2.5:32b" in usable_ids


def test_tiny_hardware_excludes_large_models_entirely():
    result = hardware.recommend_models(ram_gb=8, vram_gb=0)
    all_ids = {m["id"] for m in result["fast"] + result["usable"]}
    assert "llama3.3:70b" not in all_ids
    assert "llama3.2:1b" in all_ids or "llama3.2:3b" in all_ids


def test_cpu_only_still_returns_recommendations():
    result = hardware.recommend_models(ram_gb=32, vram_gb=0)
    assert result["fast"] == []  # no GPU, nothing is "fast"
    assert len(result["usable"]) > 0


def test_pull_commands_are_well_formed():
    result = hardware.recommend_models(ram_gb=64, vram_gb=8)
    for entry in result["fast"] + result["usable"]:
        assert entry["pull_command"] == f"ollama pull {entry['id']}"


def test_result_includes_hardware_echo_and_note():
    result = hardware.recommend_models(ram_gb=64, vram_gb=8)
    assert result["ram_gb"] == 64
    assert result["vram_gb"] == 8
    assert "approximate" in result["note"]
