"""Read-only reasoning choices from the active backend; never initiates inference."""
from litetui.settings import THINKING_LEVELS
from litetui.turn_engine import _resolve_reasoning_effort


def _as_choices(levels) -> list[str]:
    """Wire spellings -> the spellings a person picks from.

    `none` is what the ENGINE takes and `off` is what the UI says, and that
    translation must exist in exactly one place: it is the difference
    between the two vocabularies agreeing and only looking like they do.
    """
    return list(dict.fromkeys("off" if level == "none" else level
                              for level in levels if isinstance(level, str)))


def backend_levels(app) -> list[str] | None:
    """What the ACTIVE BACKEND says it takes, or None when it says nothing.

    🔴 ONE QUESTION, ONE ANSWER, TWO CALLERS. `/think` and `set_thinking`
    used to decide this separately and they DISAGREED. Measured 2026-09-17
    on ninfer with a stub app:

        /think offered  ['off','minimal','low','medium','high','xhigh']
        set_thinking    ['off','low','medium','xhigh']

    so `/think high` was accepted while `set_thinking("high")` answered
    "not supported by the active backend/model" -- for the same level, the
    same model and the same backend, in the same second. `/think` never
    asked the backend at all (it fell back to `settings.THINKING_LEVELS`,
    a global list), while `thinking_capabilities` did.

    ⚠️ AND FIXING ONLY THE TABLE WOULD HAVE FLIPPED THE GAP RATHER THAN
    CLOSING IT: with NInfer's seven levels restored, `set_thinking("max")`
    works while `/think max` is refused, because THINKING_LEVELS has six
    and lacks `max`. Two lists cannot be kept equal by editing one of them.

    None, not `[]`, when the backend does not answer: the callers have
    fallbacks for a backend that reports nothing, and an empty list would
    read as "this backend supports no levels", which is a different claim.
    """
    metadata = getattr(getattr(app, "backend", None), "reasoning_levels", None)
    if not callable(metadata):
        return None
    return _as_choices(metadata(getattr(app, "model_id", None))) or None


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
    choices = _as_choices(levels)
    return {"levels": ["default", *choices], "source": source,
            "note": ("Model support has not been reported; use the backend default."
                     if source == "unavailable" else
                     "The adapter forwards these levels; their effect depends on the server, model and chat template."
                     if name in ("llamacpp", "custom") else
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
