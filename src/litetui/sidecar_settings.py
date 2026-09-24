"""Read-only projection of authoritative settings for the trusted native preview."""
from __future__ import annotations

import re
from copy import deepcopy

from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingsSnapshot

_SENSITIVE_NAME = re.compile(r"key|token|secret|password|auth", re.IGNORECASE)


def is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE_NAME.search(name) or SETTING_SPECS.get(name) and SETTING_SPECS[name].sensitive)


def public_snapshot(snapshot: SettingsSnapshot) -> dict:
    """Return a detached wire payload; no write capability or secret-shaped fields."""
    fields = {}
    for key, spec in SETTING_SPECS.items():
        if is_sensitive(key):
            continue
        saved = deepcopy(getattr(snapshot.saved, key))
        effective = deepcopy(getattr(snapshot.effective, key))
        fields[key] = {"scope": spec.scope.value, "apply_timing": spec.apply_timing,
                       "saved": saved, "effective": effective,
                       "source": "override" if saved != effective else "saved"}
    return {"revisions": dict(snapshot.revisions), "fields": fields}
