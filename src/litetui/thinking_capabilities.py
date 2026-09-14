"""Read-only reasoning choices from the active backend; never initiates inference."""
from litetui.settings import THINKING_LEVELS
from litetui.turn_engine import _resolve_reasoning_effort


def thinking_capabilities(app):
    backend = getattr(app, "backend", None)
    name = getattr(backend, "name", "")
    model = getattr(app, "model_id", None)
    metadata = getattr(backend, "reasoning_levels", None)
    source = "unavailable"
    levels = []
    if callable(metadata):
        levels = metadata(model)
        source = "backend model metadata" if levels else "unavailable"
    elif name == "llamacpp":
        levels = list(THINKING_LEVELS)
        source = "llama.cpp adapter vocabulary"
    elif name == "lmstudio":
        discovered = getattr(app, "_model_thinking_levels", None)
        seed = getattr(getattr(app, "settings", None), "lmstudio_graded_thinking_models", ())
        if discovered:
            levels = [level for level in discovered
                      if _resolve_reasoning_effort(level, name, model, seed) is not None]
            source = "cached model discovery/configuration and adapter"
    choices = list(dict.fromkeys("off" if level == "none" else level
                                for level in levels if isinstance(level, str)))
    return {"levels": ["default", *choices], "source": source,
            "note": ("Model support has not been reported; use the backend default."
                     if source == "unavailable" else
                     "The llama.cpp adapter forwards these levels; their effect depends on the model and chat template."
                     if name == "llamacpp" else
                     "Codex CLI manages reasoning and caching; default uses the model default. Max and Ultra consume usage limits faster."
                     if name == "codex" else
                     "Levels reported by the active backend; default omits the effort field.")}


def set_thinking(app, level):
    if level is None or level == "default":
        value = None
    else:
        if level not in thinking_capabilities(app)["levels"]:
            raise ValueError("Thinking level is not supported by the active backend/model")
        value = level
    # A per-model override takes precedence over the global level in TurnEngine.
    overrides = getattr(getattr(app, "settings", None), "model_infer_overrides", {})
    entry = overrides.get(getattr(app, "model_id", None), {})
    if value is None:
        entry.pop("reasoning_effort", None)
    elif "reasoning_effort" in entry:
        entry["reasoning_effort"] = "none" if value == "off" else value
    app.thinking_level = value
    if value is None:
        remember = getattr(app, "_remember_for_this_convo", None)
        if callable(remember):
            remember("thinking_level", "default")
            if getattr(getattr(app, "backend", None), "name", "") == "codex":
                remember("reasoning_effort", "default")
