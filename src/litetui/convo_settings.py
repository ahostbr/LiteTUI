"""Per-conversation model configuration (T691).

Ryan, 2026-09-12 (a-62edbbe0): *"we fix this with per convo settings files json
that save backend model liteharness-info think level when on codex and
everything llama and lmstudio support / need for their specifics"*.

🔴 A SIBLING FILE, `.convos/<id>/settings.json`, NOT A BLOCK IN THE TRANSCRIPT.
Three reasons, in the order that decided it:
  1. THE DIRECTORY IS ALREADY THE UNIT. `handoff.md`, `memory.md`, `soul.md` and
     `memories/` are all siblings of `convo.jsonl` today, so /resume and the
     handoff flow already carry the directory rather than the transcript.
  2. `convo.jsonl` IS APPEND-ONLY. A setting that changes needs last-write-wins;
     putting mutable state in an append log means the current value is "whatever
     the final record happened to say", and every reader replays the log to
     learn one string.
  3. THE LIST PATH MUST NOT PARSE TRANSCRIPTS. `ConversationRepository` walks
     every convo directory to build the picker; answering "what model is this
     one on" from inside the transcript would make listing N conversations cost
     the size of N transcripts, for a field the row wants to show.

⚠️ BORN FROM THE GLOBAL DEFAULTS, THEN INDEPENDENT. A new conversation copies
`settings.json`'s values once; after that a change inside a conversation writes
HERE and never back to the global file. The global file keeps being the defaults
plus the knobs that are genuinely app-wide (theme, seat name, dialog style).
"""

from __future__ import annotations

import json
from copy import deepcopy
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

SETTINGS_NAME = "settings.json"


@dataclass
class ConvoSettings:
    """What a conversation remembers about how to run itself.

    ⬜ EVERY FIELD IS OPTIONAL AND DEFAULTS TO "ASK THE GLOBAL SETTINGS". `None`
    means "never chosen here", which is how a conversation created before this
    existed keeps behaving exactly as it did — its file is absent, every field
    is None, and every read falls through to the global default.
    """

    backend: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    #: Codex reasoning effort, which is a different vocabulary from LM Studio's
    #: thinking levels and must not be coerced into the same field.
    reasoning_effort: str | None = None
    tool_policy_profile: str | None = None
    #: The llama.cpp load settings for THIS conversation's model — ctx, gpu
    #: layers, whatever `llama_load_settings[model]` holds.
    llama_load: dict = field(default_factory=dict)
    #: LM Studio's side: ctx, parallel, ttl, whatever /models/load takes.
    lmstudio_load: dict = field(default_factory=dict)
    #: Who owned this conversation, for the handoff and the roster.
    seat_name: str | None = None
    seat_id: str | None = None
    seat_tier: str | None = None
    #: Additive execution snapshot; legacy top-level choices remain authoritative.
    schema_version: int = 2
    execution: dict = field(default_factory=dict)


#: The global `Settings` attribute each field is born from. A field absent here
#: has no global default and simply starts as None.
BORN_FROM: dict[str, str] = {
    "backend": "backend",
    "model": "default_model",
    "thinking_level": "thinking_level",
    "tool_policy_profile": "tool_policy_profile",
}


def path_for(convo_dir: Path) -> Path:
    return Path(convo_dir) / SETTINGS_NAME


def born_from(settings) -> ConvoSettings:
    """A new conversation's starting point: the global defaults, copied once."""
    settings = deepcopy(settings)
    from litetui.settings import ENV_OVERRIDES
    for key, value in getattr(settings, '_saved_values', {}).items():
        if key in ENV_OVERRIDES and os.environ.get(ENV_OVERRIDES[key]):
            setattr(settings, key, deepcopy(value))
    cs = ConvoSettings()
    for own, global_name in BORN_FROM.items():
        setattr(cs, own, deepcopy(getattr(settings, global_name, None)))
    from litetui.settings_scope import SETTING_SPECS, SettingScope, validate_registry
    validate_registry()
    cs.execution = {name: deepcopy(getattr(settings, name))
                    for name, spec in SETTING_SPECS.items()
                    if spec.scope == SettingScope.CONVERSATION}
    return cs


def load(convo_dir: Path) -> ConvoSettings:
    """This conversation's settings, or an all-None record when it has none.

    NEVER RAISES. A conversation whose settings file is missing, truncated or
    hand-edited to nonsense must still OPEN — the transcript is the valuable
    thing and a config file is not worth losing it over. Unreadable is treated
    exactly like absent, which falls through to the global defaults.
    """
    cs = ConvoSettings()
    cs._diagnostics = []
    p = path_for(convo_dir)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cs
    except (OSError, ValueError) as exc:
        cs._diagnostics.append(str(exc))
        return cs
    if not isinstance(raw, dict):
        cs._diagnostics.append('Settings must be an object')
        return cs
    known = {f.name for f in fields(ConvoSettings)}
    for k, v in raw.items():
        if k in known:
            valid = (isinstance(v, dict) if k in ('execution', 'llama_load', 'lmstudio_load')
                     else type(v) is int and v in (1, 2) if k == 'schema_version'
                     else v is None or isinstance(v, str))
            if valid:
                setattr(cs, k, v)
            else:
                cs._diagnostics.append(f'Invalid value for {k}')
    return cs


def save(convo_dir: Path, cs: ConvoSettings) -> Path:
    """Write atomically — temp file plus one `os.replace`.

    The same rule T688 established for the global settings, and for the same
    reason: `write_text` truncates before it writes, `load` above answers an
    unparseable file with silent defaults, so a reader landing between the two
    syscalls would see a conversation quietly revert to the global model rather
    than an error anybody could act on.
    """
    from litetui.shared_state import coordinated_write
    with coordinated_write(path_for(convo_dir)):
        return _save_locked(convo_dir, cs)


def _save_locked(convo_dir: Path, cs: ConvoSettings) -> Path:
    d = Path(convo_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = path_for(d)
    payload = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(existing, dict):
                payload.update(existing)
        except (OSError, ValueError):
            pass
    payload.update(asdict(cs))
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def resolved(cs: ConvoSettings, settings, name: str):
    """This conversation's value for `name`, falling back to the global one.

    ⚠️ `None` IS "NOT CHOSEN HERE", NOT "CHOSEN AS NOTHING". `thinking_level`
    can legitimately be the string "off"; treating a falsy value as unset would
    make "off" unstorable and silently promote the global default over an
    explicit choice.
    """
    own = getattr(cs, name, None)
    if own is not None:
        return own
    global_name = BORN_FROM.get(name, name)
    if global_name in cs.execution:
        return deepcopy(cs.execution[global_name])
    return getattr(settings, global_name, None) if global_name else None
