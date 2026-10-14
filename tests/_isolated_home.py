"""Redirect import-cached home defaults without changing product availability checks.

The caller owns patch lifetime: pytest supplies monkeypatch.setattr and script
entry points supply an ExitStack-backed setter. Explicit executable/model paths
and later test-specific patches remain authoritative.
"""
from pathlib import Path


def isolated_environment(home):
    """Replace inherited profile/data overrides; deliberate test overrides win."""
    home = Path(home)
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CLAUDE_SKILL_DIR": "",
        "HF_HOME": str(home / ".cache" / "huggingface"),
        "LMSTUDIO_MODELS": str(home / ".lmstudio" / "models"),
        "LITESUITE_LLM_DIR": str(home / ".litesuite" / "llm"),
        "CLINE_DATA_DIR": str(home / ".cline" / "data"),
        # Empty exclusive overrides preserve normal fallback/selection priority.
        "CLINE_PROVIDER_SETTINGS_PATH": "",
        "LITETUI_SIDECAR_EXE": "",
        "LITESUITE_FLEET_POLICY": str(home / ".litesuite" / "fleet-policy.json"),
    }


def redirect_cached_home(home, set_attribute):
    from litetui import harness, listen_tool, llm_backend

    home = Path(home)
    engine = home / ".litesuite" / "llm"
    audio = home / ".lmstudio" / "models" / "mradermacher" / "Qwen2-Audio-7B-Instruct-GGUF"
    defaults = (
        (llm_backend, "LITESUITE_LLM", engine),
        (llm_backend, "LLAMA_EXE", engine / "bin" / "llama-server.exe"),
        (llm_backend, "BUILD_INFO", engine / "bin" / ".build-info"),
        (harness, "INBOX_ROOT", home / ".liteharness" / "inbox"),
        (harness, "NEW", home / ".liteharness" / "inbox" / "new"),
        (harness, "CUR", home / ".liteharness" / "inbox" / "cur"),
        (harness, "DONE", home / ".liteharness" / "inbox" / "done"),
        (listen_tool, "MODEL_DIR", audio),
        (listen_tool, "GGUF", audio / "Qwen2-Audio-7B-Instruct.Q4_K_M.gguf"),
        (listen_tool, "MMPROJ", audio / "Qwen2-Audio-7B-Instruct.mmproj-f16.gguf"),
    )
    for module, name, value in defaults:
        set_attribute(module, name, value)
