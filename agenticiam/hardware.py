"""Hardware-based Ollama model recommendations for the Setup wizard.

There's no reliable programmatic catalog of "how much VRAM does model X
need" — Ollama's own library pages don't expose it as an API. This is a
small curated table of popular models with their approximate Q4_K_M
(Ollama's typical default quantization) memory footprint, used to sort
models into three tiers against the hardware the user reports:

- "fast": the whole model fits comfortably in VRAM alongside some context
  headroom, so inference runs fully GPU-accelerated.
- "usable": too big for VRAM alone but fits once spilling into system RAM
  is allowed (partial GPU offload) — works, meaningfully slower.
- everything else is left off the list rather than recommended, rather
  than suggesting something that will thrash or fail to load.

Sizes are approximate and vary with actual quantization/context length —
labelled as such everywhere they're shown, not asserted as exact.
"""

# (id, family, params, tags, approx_q4_gb)
CATALOG = [
    ("llama3.2:1b", "Llama 3.2", "1B", ("general", "tiny"), 1.3),
    ("llama3.2:3b", "Llama 3.2", "3B", ("general", "tiny"), 2.0),
    ("qwen2.5:7b", "Qwen 2.5", "7B", ("general",), 4.7),
    ("qwen2.5-coder:7b", "Qwen 2.5 Coder", "7B", ("coding",), 4.7),
    ("llama3.1:8b", "Llama 3.1", "8B", ("general",), 4.9),
    ("mistral:7b", "Mistral", "7B", ("general",), 4.1),
    ("gemma2:9b", "Gemma 2", "9B", ("general",), 5.5),
    ("phi4:14b", "Phi-4", "14B", ("general", "reasoning"), 9.1),
    ("qwen2.5:14b", "Qwen 2.5", "14B", ("general",), 9.0),
    ("qwen2.5-coder:14b", "Qwen 2.5 Coder", "14B", ("coding",), 9.0),
    ("qwen2.5:32b", "Qwen 2.5", "32B", ("general",), 19.8),
    ("qwen2.5-coder:32b", "Qwen 2.5 Coder", "32B", ("coding",), 19.8),
    ("mixtral:8x7b", "Mixtral", "8x7B (MoE)", ("general",), 26.0),
    ("llama3.3:70b", "Llama 3.3", "70B", ("general", "reasoning"), 42.5),
]

# Fraction of VRAM/RAM left as headroom for KV cache/context and the OS —
# a model whose weights exactly equal the hardware size won't actually load.
VRAM_HEADROOM = 0.85
RAM_HEADROOM = 0.70


def recommend_models(ram_gb: float, vram_gb: float = None) -> dict:
    """Sort CATALOG into tiers for the given hardware.

    `ram_gb` is total system RAM; `vram_gb` is dedicated GPU VRAM (omit or
    0 for CPU-only / integrated-graphics-only systems, in which case
    everything is judged against RAM alone).
    """
    if not ram_gb or ram_gb <= 0:
        raise ValueError("ram_gb must be a positive number")
    vram_gb = vram_gb or 0

    fast, usable = [], []
    for model_id, family, params, tags, size_gb in CATALOG:
        entry = {
            "id": model_id, "family": family, "params": params,
            "tags": list(tags), "approx_size_gb": size_gb,
            "pull_command": f"ollama pull {model_id}",
        }
        if vram_gb and size_gb <= vram_gb * VRAM_HEADROOM:
            fast.append(entry)
        elif size_gb <= (vram_gb + ram_gb) * RAM_HEADROOM:
            usable.append(entry)
        # else: too big for this hardware, omitted rather than suggested

    return {
        "ram_gb": ram_gb,
        "vram_gb": vram_gb,
        "fast": fast,
        "usable": usable,
        "note": (
            "Sizes are approximate Q4_K_M (Ollama's common default quantization) "
            "estimates and vary with actual quant/context length — treat these as "
            "a starting point, not an exact fit."
        ),
    }
