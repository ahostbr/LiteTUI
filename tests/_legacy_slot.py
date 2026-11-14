"""Compose a test app's prompt the way every legacy prompt was composed: the host MEMORY section at slot 10.

T1306-D moved MEMORY (the conversation id and store path) behind the tools text. A prompt without recorded store
provenance predates T0338 and therefore has MEMORY at slot 10, which is where store_identity's legacy reader looks.
A fixture that manufactures such a prompt from today's composer must compose at that slot, or it manufactures a
file no LiteTUI ever wrote.
"""
from litetui.plugins import PROMPT_ORDER, PromptSection
from litetui.store_identity import LEGACY_MEMORY_ORDER


def compose_at_legacy_slot(app):
    app.plugins.prompt_sections = [
        PromptSection(s.owner, LEGACY_MEMORY_ORDER, s.render, s.enabled)
        if s.owner == "host" and s.order == PROMPT_ORDER["MEMORY"] else s
        for s in app.plugins.prompt_sections]
    return app
