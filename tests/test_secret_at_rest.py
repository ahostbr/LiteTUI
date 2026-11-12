"""Provider API keys are sealed (Windows DPAPI, AES-256) in settings.json."""
import base64
import json
import struct
import sys

import pytest

from litetui import secret_at_rest as sar
from litetui import settings as st
from litetui.settings_service import SettingChange, SettingsService

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")

KEY = "sk-ant-fixture-credential-do-not-log"


def disk(root):
    return json.loads(st.settings_path(root).read_text(encoding="utf-8"))


def test_save_writes_sealed_value_and_load_returns_plaintext(tmp_path):
    s = st.load(tmp_path)
    s.anthropic_api_key = KEY
    s.theme_name = "monokai"
    st.save(s, tmp_path)

    raw = st.settings_path(tmp_path).read_text(encoding="utf-8")
    assert KEY not in raw
    assert disk(tmp_path)["anthropic_api_key"].startswith(sar.PREFIX)
    assert disk(tmp_path)["theme_name"] == "monokai"  # other fields untouched
    assert st.load(tmp_path).anthropic_api_key == KEY


def test_unrelated_save_does_not_reseal_or_rewrite_the_key(tmp_path):
    s = st.load(tmp_path)
    s.anthropic_api_key = KEY
    st.save(s, tmp_path)
    sealed = disk(tmp_path)["anthropic_api_key"]
    again = st.load(tmp_path)
    again.theme_name = "nord"
    st.save(again, tmp_path)
    assert disk(tmp_path)["anthropic_api_key"] == sealed


def test_settings_service_patch_seals_and_snapshot_unseals(tmp_path):
    service = SettingsService(tmp_path)
    (tmp_path / ".convos" / "abc").mkdir(parents=True)
    revisions = service.snapshot("abc").revisions
    result = service.save_patch("abc", [SettingChange("anthropic_api_key", KEY, "device")], revisions)
    assert all(p.saved for p in result.persistence), result
    assert KEY not in st.settings_path(tmp_path).read_text(encoding="utf-8")
    assert service.snapshot("abc").saved.anthropic_api_key == KEY
    assert service.global_value("anthropic_api_key") == (True, KEY)


def test_plaintext_from_older_build_is_sealed_on_load(tmp_path):
    st.settings_path(tmp_path).write_text(json.dumps(
        {"anthropic_api_key": KEY, "groq_api_key": "gsk-plain", "theme_name": "nord"}), encoding="utf-8")
    loaded = st.load(tmp_path)
    assert (loaded.anthropic_api_key, loaded.groq_api_key) == (KEY, "gsk-plain")
    on_disk = disk(tmp_path)
    assert on_disk["anthropic_api_key"].startswith(sar.PREFIX)
    assert on_disk["groq_api_key"].startswith(sar.PREFIX)
    assert on_disk["theme_name"] == "nord"  # non-secrets are not touched


def test_every_provider_key_is_sealed(tmp_path):
    from litetui.sidecar_settings import SECRET_FIELDS

    s = st.load(tmp_path)
    for name in SECRET_FIELDS:
        setattr(s, name, f"{name}-fixture-value")
    st.save(s, tmp_path)
    raw = st.settings_path(tmp_path).read_text(encoding="utf-8")
    assert "fixture-value" not in raw
    loaded = st.load(tmp_path)
    assert all(getattr(loaded, name) == f"{name}-fixture-value" for name in SECRET_FIELDS)


def test_free_tier_uses_sealed_key_and_never_sends_an_unreadable_one(tmp_path, monkeypatch):
    from litetui import free_tier

    groq = next(s for s in free_tier.SOURCES if s.key_env == "GROQ_API_KEY")
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = st.load(tmp_path)
    s.groq_api_key = "gsk-fixture"
    st.save(s, tmp_path)
    assert free_tier.source_key(groq) == "gsk-fixture"

    foreign = sar.seal("other_field", "gsk-fixture")
    st.settings_path(tmp_path).write_text(json.dumps({"groq_api_key": foreign}), encoding="utf-8")
    assert free_tier.source_key(groq) is None


def test_undecryptable_key_is_reported_not_treated_as_missing(tmp_path, monkeypatch):
    from litetui.anthropic_backend import api_key
    from litetui.model_transport import ProviderError

    # A blob sealed for another field cannot be opened as this one (entropy binding),
    # standing in for a settings.json copied from another Windows account.
    foreign = sar.seal("other_field", KEY)
    st.settings_path(tmp_path).write_text(json.dumps({"anthropic_api_key": foreign}), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-must-not-be-used")
    loaded = st.load(tmp_path)
    assert loaded.anthropic_api_key == foreign
    with pytest.raises(ProviderError, match="cannot be decrypted"):
        api_key(loaded)


def test_blank_key_stays_blank(tmp_path):
    s = st.load(tmp_path)
    s.anthropic_api_key = KEY
    st.save(s, tmp_path)
    s.anthropic_api_key = ""
    st.save(s, tmp_path)
    assert disk(tmp_path)["anthropic_api_key"] == ""


def test_dpapi_blob_uses_aes_256():
    blob = base64.b64decode(sar.seal("anthropic_api_key", KEY)[len(sar.PREFIX):])
    # DPAPI blob: version, provider GUID, mk version, mk GUID, flags, desc len, desc, algCrypt.
    desc_len = struct.unpack_from("<I", blob, 44)[0]
    alg_crypt, alg_bits = struct.unpack_from("<II", blob, 48 + desc_len)
    assert (alg_crypt, alg_bits) == (0x6610, 256)  # CALG_AES_256
