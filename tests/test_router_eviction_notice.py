"""T688 G — a load that evicts somebody else's model says so.

The llama.cpp router holds at most `--models-max` models, and that ceiling is
the OWNER's `llama_models_max` (`llm_backend.py:850`). Every instance talking to
that router shares the ceiling, so a load from one can push out a model the
other is mid-turn on. The router does it silently: `/models/load` answers
`{"success": true}` either way, and the evicted instance finds out when its next
completion is slow or fails.

⬜ THERE IS NO CROSS-PROCESS PROTOCOL HERE AND DELIBERATELY SO. Both instances
read the same `/models`, so the one doing the loading can name what it is about
to displace, and the other can notice on its next refresh. A lock or a handshake
would be a new contract between two apps that currently share nothing but a port
— far more than the problem is worth, and one more thing to go wrong when only
one of them is running.

⚠️ WHAT THIS CANNOT DO IS NAME *WHICH* MODEL THE ROUTER WILL DROP. The eviction
policy belongs to llama.cpp, not to us; claiming "evicting X" when the router
actually drops Y would be a confident wrong answer, which is worse than a vague
right one. So the notice names the models that ARE loaded and the ceiling that
forces the choice.
"""

from __future__ import annotations

import pytest

from litetui import llm_backend
from litetui import settings as st


def _backend(max_models: int, loaded: dict[str, str]) -> llm_backend.LlamaCppBackend:
    """`loaded` maps model id -> status value, as /models reports it."""
    s = st.Settings()
    s.llama_models_max = max_models
    b = llm_backend.LlamaCppBackend(s)
    b._server_models = lambda: {  # type: ignore[method-assign]
        key: {"id": key, "status": {"value": state}} for key, state in loaded.items()
    }
    return b


def test_a_load_under_the_ceiling_says_nothing(_no_network: None) -> None:
    """Room to spare — no eviction, so no notice. A warning that fires when
    nothing is displaced trains the user to ignore it."""
    b = _backend(2, {"qwen/a": "loaded"})
    assert b.eviction_notice("qwen/b") is None


def test_a_load_at_the_ceiling_names_what_is_loaded_and_the_limit(_no_network) -> None:
    b = _backend(1, {"qwen/a": "loaded"})
    note = b.eviction_notice("qwen/b")
    assert note is not None
    assert "qwen/a" in note, "it must name what is about to be displaced"
    assert "1" in note, "...and the ceiling that forces it"


def test_reloading_a_model_that_is_ALREADY_loaded_evicts_nothing(_no_network) -> None:
    """The discriminator. At the ceiling, asking for a resident model is a
    no-op, and warning about it would be a false alarm every time the user
    re-picks the model they are already on."""
    b = _backend(1, {"qwen/a": "loaded"})
    assert b.eviction_notice("qwen/a") is None


def test_models_that_are_NOT_loaded_do_not_count_toward_the_ceiling(_no_network) -> None:
    """`/models` lists everything the ini knows, loaded or not. Counting rows
    instead of LOADED rows would warn about an eviction on an empty server —
    which is what a first draft of this did until the fixture carried a status.
    """
    b = _backend(1, {"qwen/a": "not-loaded", "qwen/b": "not-loaded"})
    assert b.eviction_notice("qwen/c") is None


def test_an_unreadable_model_list_is_silence_not_a_crash(_no_network) -> None:
    """This runs on the way INTO a load. A probe that raised here would turn a
    load that was about to work into a failure, which is a worse outcome than
    the missing warning it was trying to give."""
    b = _backend(1, {})

    def _boom() -> dict:
        raise llm_backend.BackendError("no /models today")

    b._server_models = _boom  # type: ignore[method-assign]
    assert b.eviction_notice("qwen/b") is None


@pytest.fixture()
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may touch a real server; `_server_models` is stubbed per
    backend, and this makes any OTHER outbound call fail loudly rather than
    quietly reaching Ryan's live router on 7470."""

    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("an arm tried to reach the network")

    monkeypatch.setattr(llm_backend, "_http_json", _forbidden)
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: False)
