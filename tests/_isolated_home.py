"""Redirect import-cached home defaults without changing product availability checks.

The caller owns patch lifetime: pytest supplies monkeypatch.setattr and script
entry points supply an ExitStack-backed setter. Explicit executable/model paths
and later test-specific patches remain authoritative.
"""
from pathlib import Path


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
        (listen_tool, "MODEL_DIR", audio),
        (listen_tool, "GGUF", audio / "Qwen2-Audio-7B-Instruct.Q4_K_M.gguf"),
        (listen_tool, "MMPROJ", audio / "Qwen2-Audio-7B-Instruct.mmproj-f16.gguf"),
    )
    for module, name, value in defaults:
        set_attribute(module, name, value)
