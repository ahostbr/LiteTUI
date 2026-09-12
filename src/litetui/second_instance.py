"""Should this model load ask the human first? (T690)

Ryan's ruling (a-62edbbe0, 2026-09-12): two LiteTUI instances SHARE a model
server and run in parallel — *"both should be able to use the model at the same
time ... llama.cpp supports parallel and lmstudio does for exactly this"*. What
he wants guarded is the other thing: *"THE HUMAN MUST BE WARNED THAT LOADING
different models in different instances WILL CAUSE MULTIPLE MODELS IN VRAM! AND
CAN CAUSE OOM ... EVERY TIME a model would be swapped or loaded."*

🔴 SO THE TRIGGER IS VRAM, NOT CONTENTION. Two instances talking to the same
loaded model cost one set of weights and raise nothing. A load that ADDS a
second set is the whole hazard, and it is the only thing that prompts.

⬜ THE DECISION IS HERE AND THE DIALOG IS IN `app.py`, because this is the part
worth testing exhaustively and none of it needs a running terminal. The rule has
three inputs and they are all knowable without drawing anything.
"""

from __future__ import annotations


def needs_vram_confirmation(*, sibling: str | None, already_loaded: bool) -> bool:
    """True when a load must ask first.

    `sibling` is the name of another live LiteTUI (None when we are alone), and
    `already_loaded` says whether the server is ALREADY serving this model.

    ⚠️ ALONE MEANS NO PROMPT EVEN FOR A SWAP. A single instance swapping models
    replaces its own weights; there is no second set and nothing to warn about.
    Prompting there would be the noise that teaches someone to stop reading.
    """
    if sibling is None:
        return False
    return not already_loaded


def warning_text(sibling: str, model: str, *, needs_restart: bool = False) -> str:
    """What the human reads. Two shapes, because the costs differ.

    🔴 THE RESTART CASE IS A DIFFERENT SENTENCE, NOT A LOUDER ONE. A hot load
    adds weights; regenerating the preset means RESTARTING the shared server,
    which interrupts whatever the other instance is doing mid-turn. Telling
    someone "this may use more VRAM" while the real cost is "the other window's
    answer stops" would be the wrong warning delivered convincingly.
    """
    if needs_restart:
        return (
            f"Another LiteTUI instance is running ({sibling}).\n\n"
            f"{model} is not in this server's preset, so loading it means "
            f"RESTARTING the shared llama.cpp server — that interrupts "
            f"{sibling} mid-turn.\n\n"
            f"Restart the shared server and load {model}?"
        )
    return (
        f"Another LiteTUI instance is running ({sibling}).\n\n"
        f"Loading a different model puts a second model in VRAM and can OOM "
        f"depending on your setup. {sibling} is using this server too.\n\n"
        f"Load {model} anyway?"
    )


def refusal_text(sibling: str, model: str) -> str:
    """What a HEADLESS child answers instead of prompting.

    🔴 IT REFUSES; IT DOES NOT ASSUME YES. An rpc child has no keyboard, and the
    host (LiteSuite) renders refusals loudly already. Auto-approving here would
    put a second model on the card on behalf of a human who was never asked —
    the exact outcome the modal exists to prevent, reached by the one path that
    cannot show it.
    """
    return (
        f"refused: {sibling} is running and {model} is not loaded. Loading it "
        f"would put a second model in VRAM. Load it from a LiteTUI window, or "
        f"use the model that is already loaded."
    )
