"""Canonical persistence ownership. New Settings fields require a decision here."""
from dataclasses import dataclass, fields
from enum import Enum


class SettingScope(str, Enum):
    CONVERSATION = 'conversation'
    DEFAULTS = 'defaults'
    DEVICE = 'device'


@dataclass(frozen=True)
class SettingSpec:
    key: str
    scope: SettingScope
    apply_timing: str = 'next_turn'
    sensitive: bool = False


_CONVERSATION = '''custom_base_url custom_api_key_env custom_context_length lm_host ninfer_host ninfer_executable ninfer_artifact
ninfer_max_context ninfer_max_concurrency ninfer_kv_dtype ninfer_kv_capacity ninfer_host_kv_mib default_model default_context_length
pin_default_model backend codex_native_engine llama_host llama_executable
llama_attach_hosts llama_models_max llama_load_settings model_infer_overrides
lms_load_timeout_s max_tokens_tools max_tokens_chat thinking_level
lmstudio_graded_thinking_models temperature top_p top_k min_p repeat_penalty
presence_penalty frequency_penalty seed stop tool_iterations tool_auto_background_s
subagent_model tool_summary_model tools_enabled tool_policy_profile tool_always_allow
tool_deny tools_disabled tool_context_mode tool_context_threshold_chars
compact_max_tool_iters compact_max_tokens compact_keep_recent compact_thinking_level
autocompact_enabled autocompact_at_percent wake_after_compact seat_name skills_enabled
skill_roots mcp_enabled mcp_disabled_servers plugins_disabled'''.split()
_DEVICE = '''claude_executable llama_scan_litesuite llama_scan_lmstudio llama_scan_hf_cache
llama_models_dirs enter_interrupts clear_screen_after_compact theme_name custom_themes
show_thinking show_stop_line show_stop_time autoscroll error_message_style dialog_style dialog_side
image_viewer_enabled footer_show_seat footer_show_thinking footer_show_bg
footer_show_subagents footer_show_convo footer_show_context footer_show_context_pct
footer_show_tps footer_order tts_enabled tts_timeout tts_engine tts_voice tts_edge_voice
stt_model stt_mic stt_hotkey'''.split()
_DEFAULTS = ['backend_chosen', 'llama_presets']
_RECONNECT = set('claude_executable custom_base_url custom_api_key_env custom_context_length lm_host ninfer_host llama_host backend default_model codex_native_engine pin_default_model skills_enabled skill_roots mcp_enabled mcp_disabled_servers'.split())
_RESTART = set('ninfer_executable ninfer_artifact ninfer_max_context ninfer_max_concurrency ninfer_kv_dtype ninfer_kv_capacity ninfer_host_kv_mib llama_executable llama_attach_hosts llama_models_max plugins_disabled'.split())

SETTING_SPECS = {}
for scope, names in ((SettingScope.CONVERSATION, _CONVERSATION),
                     (SettingScope.DEVICE, _DEVICE),
                     (SettingScope.DEFAULTS, _DEFAULTS)):
    for name in names:
        if name in SETTING_SPECS:
            raise ValueError(f'Duplicate scope mapping: {name}')
        timing = ('restart' if name in _RESTART else 'reconnect' if name in _RECONNECT
                  else 'reload' if name in ('default_context_length', 'llama_load_settings')
                  else 'immediate' if scope == SettingScope.DEVICE else 'next_turn')
        SETTING_SPECS[name] = SettingSpec(name, scope, timing)


def validate_registry(registry=None):
    from litetui.settings import Settings
    registry = SETTING_SPECS if registry is None else registry
    expected = {f.name for f in fields(Settings)}
    missing, extra = expected - registry.keys(), registry.keys() - expected
    invalid = [k for k, spec in registry.items()
               if spec.key != k or not isinstance(spec.scope, SettingScope)]
    if missing or extra or invalid:
        raise ValueError(f'Invalid settings scopes: missing={sorted(missing)}, extra={sorted(extra)}, invalid={invalid}')
