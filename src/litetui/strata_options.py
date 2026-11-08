"""Every Strata run option that can be edited, as one catalog.

Strata is started from a `strata-<model>.json` (engine args plus server keys). Until now only the
context length could be changed from LiteTUI; everything else (the VRAM reserve, the KV format,
speculative decoding, the expert cache, batching, the server's reply handling ...) lived only in that
file or on a command line. Ryan 2026-10-24: "expose that and anything and everything strata exposes
we can edit ... no flags left then organize them like the rest of litetui settings our in the
collapseable sections".

ONE TABLE DRIVES FIVE PLACES: the `Settings` fields (`strata_<key>`, declared in settings.py and
pinned to this table by a test), their scope and apply timing (settings_scope), the labels, help and
collapsible sections (settings_ui_model), the controls (settings_screen) and the launch (`apply`,
called by `strata_engine.launch_plan`). A blank value means "leave the install's config as it is";
a value replaces that flag or key in the COPY LiteTUI launches (`<model>.litetui.json` beside the
log) and never touches the install's own file.

WHERE EACH OPTION COMES FROM: the flag list is `strata.exe --help` (engine 0.1.x, the Strata fork),
the server keys are `serve/runconfig.py` EDITABLE plus the keys docs/DETAILS.md documents. NOT in
the catalog on purpose, and reachable through "Extra engine arguments" where they are engine flags:
the diagnostics and A/B arms (--dump-*, --native-* arms, --stage-timing, --bench ...), the network and
security keys (host, api_key, cors_origins, trusted_origins, allowed_hosts) and the keys that run
programs (before_load, MCP servers), and the install's own plumbing (exe, cwd, lib_dirs, log,
tokenizer, model_name, port: the port is `strata_host`).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

#: Prefix of every Settings field this catalog owns.
PREFIX = "strata_"
#: The free-text escape hatch: any engine flag not in the catalog.
EXTRA_FIELD = "strata_extra_args"

#: Measured on Ryan's PC (RTX 5090 32 GB, Uncensored IQ3_XXS, 2026-10-24,
#: strata-uncensored_iq3_xxs.log): `expert cache auto: 24.64 GiB free, 1082 MiB reserved (+184 MiB
#: for the draft head) -> 11547 slots`, then `688 MiB of VRAM free with everything loaded`, and 480-783
#: MiB free while it ran. The cache took about 23 GiB.
_MEASURED = ("Measured here (RTX 5090, Uncensored IQ3_XXS): reserve 1082 MiB gave 11,547 cache slots "
             "(about 23 GiB) and left 480-783 MiB free once loaded.")


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str
    summary: str
    keywords: tuple[str, ...] = ()


SECTIONS: tuple[Section, ...] = (
    Section("strata-memory", "Memory and VRAM",
            "How much VRAM and RAM the engine takes; mistakes here stall the PC or kill the engine.",
            ("vram", "reserve", "expert cache", "ram", "memory")),
    Section("strata-context", "Context, KV cache and RoPE",
            "How context is stored and stretched; the context length itself is in Engine above.",
            ("kv", "int8", "rope", "yarn", "context")),
    Section("strata-speculative", "Speculative decoding and MTP",
            "The draft layer that proposes tokens for the model to check.",
            ("spec", "mtp", "draft", "speculative", "lookup")),
    Section("strata-prefill", "Prompt processing and caches",
            "Reading the prompt in chunks, and what is kept between requests.",
            ("prefill", "prompt cache", "conversation cache", "session")),
    Section("strata-experts", "Expert cache and CPU pool",
            "Which experts live in VRAM and how the CPU shares the rest.",
            ("expert", "profile", "pool", "affinity", "threads", "cpu")),
    Section("strata-gpus", "Batching and multiple GPUs",
            "Several requests at once, and models split across cards.",
            ("batch", "parallel", "gpu", "layer split", "peer", "pipeline")),
    Section("strata-files", "Model files",
            "The files the engine loads. Blank keeps the model's own paths.",
            ("pack", "gguf", "native", "ple", "mtp", "embd")),
    Section("strata-server", "Server behaviour",
            "What the Strata server does around the engine: unloading, replies, tool-call recovery.",
            ("idle", "unload", "lazy", "reasoning", "anthropic", "codex", "fit max tokens")),
    Section("strata-sampling", "Sampling defaults",
            "Defaults for requests that send none (LiteTUI sends its own, which win).",
            ("temperature", "top p", "top k", "min p", "sampling")),
    Section("strata-extra", "Extra engine arguments",
            "Any engine flag not listed above, so no flag is left only in a file.",
            ("flags", "arguments", "args", "diagnostic")),
)


@dataclass(frozen=True)
class Opt:
    """One option. `kind`: int, float, text, choice, auto (the word `auto` or a whole number),
    switch (As configured / On / Off). `target`: ("arg", flag) | ("pair", on_flag, off_flag) |
    ("key", config_key) | ("sampling", name)."""
    key: str
    section: str
    target: tuple
    kind: str
    label: str
    help: str
    choices: tuple[str, ...] = ()
    lo: float | None = None
    hi: float | None = None
    warn: str = ""
    #: choice -> the JSON value written for it (reasoning_loop_recovery's "off" is `false`)
    convert: tuple[tuple[str, object], ...] = ()
    placeholder: str = ""

    @property
    def field(self) -> str:
        return PREFIX + self.key

    @property
    def type_name(self) -> str:
        return {"int": "int | None", "float": "float | None"}.get(self.kind, "str | None")

    def full_help(self) -> str:
        return f"{self.help} {self.warn}".strip()


def _o(key, section, target, kind, label, help_, **kw) -> Opt:
    return Opt(key, section, target, kind, label, help_, **kw)


_M, _C, _S, _P, _E, _G, _F, _V, _SA = (s.section_id for s in SECTIONS[:9])

OPTIONS: tuple[Opt, ...] = (
    # ── memory ──
    _o("vram_reserve_mib", _M, ("arg", "--vram-reserve-mib"), "int", "VRAM kept free (MiB)",
       "VRAM the engine leaves free for other programs; the expert cache gets the rest. Engine default 700.",
       lo=0, warn="Too low and the card runs out while serving; too high shrinks the expert cache. " + _MEASURED),
    _o("expert_cache", _M, ("arg", "--expert-cache"), "auto", "Expert cache size",
       "Expert blobs kept resident in VRAM: auto fills the card down to the VRAM reserve, a number fixes the count.",
       lo=0, placeholder="auto, or a slot count",
       warn="A fixed count that does not fit leaves no VRAM free; leave auto unless you have measured. "
            "Here auto chose 11,547 slots (about 23 GiB) at a 1082 MiB reserve."),
    _o("expert_cache_per_layer", _M, ("arg", "--expert-cache-per-layer"), "switch", "Give each layer its own cache slots",
       "Slots are shared out per layer instead of in arrival order; the engine's measured hit rate is far higher."),
    _o("vram_elastic", _M, ("arg", "--vram-elastic"), "switch", "Let other programs borrow VRAM",
       "The expert cache is allocated in segments so the server can hand VRAM back between requests (one NVIDIA GPU)."),
    _o("vram_segment_mib", _M, ("arg", "--vram-segment-mib"), "int", "Elastic segment size (MiB)",
       "Size of one expert-cache segment when VRAM can be handed back. Engine default 512.", lo=1),
    _o("kv_resident", _M, ("arg", "--kv-resident"), "int", "KV cells kept in VRAM",
       "Streams the KV cache: keep this many cells per layer in VRAM and the rest in pinned RAM; the freed VRAM goes to experts. "
       "0 keeps all of it in VRAM; otherwise at least 20480.", lo=0),
    _o("kv_grow", _M, ("arg", "--kv-grow"), "switch", "Grow the KV cache as needed",
       "Take VRAM for the KV cache only as requests reach it, giving the expert cache the rest (one GPU, needs an expert profile)."),
    _o("mmap_experts", _M, ("arg", "--mmap-experts"), "switch", "Read experts through mapped files",
       "Opts out of the resident expert arena; the OS page cache must hold the experts, so a cold start is much slower."),
    _o("resident_cpu_experts", _M, ("arg", "--resident-cpu-experts"), "switch", "Keep CPU experts resident in RAM",
       "With mapped files and a static profile, hold the experts the GPU does not cache in ordinary RAM."),
    _o("resident_experts", _M, ("arg", "--resident-experts"), "switch", "Low-RAM resident mode",
       "The low-RAM PC's mode: mapped files plus resident CPU experts, page-locked when possible, 4 GiB headroom."),
    _o("resident_budget_gib", _M, ("arg", "--resident-budget-gib"), "int", "Resident RAM budget (GiB)",
       "RAM budget for the resident variant when the experts do not all fit in memory.", lo=1),
    _o("min_free_vram_mib", _M, ("key", "min_free_vram_mib"), "int", "Free VRAM needed to load (MiB)",
       "Load an unloaded model only when this much VRAM is free (waits up to 15 s), else answer 503 'GPU in use'.", lo=0),
    # ── context ──
    _o("kv", _C, ("arg", "--kv"), "choice", "KV cache format",
       "fp16 is exact; int8 halves the VRAM; q4_0 quarters it with slightly lower precision; k8v4 keeps keys at 8 bits and values at 4.",
       choices=("fp16", "int8", "q4_0", "k8v4")),
    _o("rope_scaling", _C, ("arg", "--rope-scaling"), "choice", "RoPE scaling",
       "Stretches the trained context: none, linear (position interpolation) or yarn. Fixed for a run.",
       choices=("none", "linear", "yarn")),
    _o("rope_scale", _C, ("arg", "--rope-scale"), "float", "RoPE extension factor",
       "The factor for linear or yarn scaling (1 = off).", lo=0.01),
    _o("rope_freq_base", _C, ("arg", "--rope-freq-base"), "float", "RoPE frequency base",
       "Raw ggml knob: the frequency base (0 = the model's 1e7).", lo=0),
    _o("rope_freq_scale", _C, ("arg", "--rope-freq-scale"), "float", "RoPE angle shrink",
       "Raw ggml knob: the angle shrink (0 = 1 / the extension factor).", lo=0),
    _o("yarn_orig_ctx", _C, ("arg", "--yarn-orig-ctx"), "int", "YaRN trained context",
       "The trained context YaRN's correction targets (0 = 262144).", lo=0),
    _o("yarn_ext_factor", _C, ("arg", "--yarn-ext-factor"), "float", "YaRN extrapolation mix",
       "YaRN's extrapolation factor; -1 chooses automatically (1 for yarn)."),
    _o("yarn_attn_factor", _C, ("arg", "--yarn-attn-factor"), "float", "YaRN attention factor",
       "YaRN's attention magnitude scale. Default 1.", lo=0),
    _o("yarn_beta_fast", _C, ("arg", "--yarn-beta-fast"), "float", "YaRN fast beta",
       "YaRN's low correction dimension. Default 32.", lo=0),
    _o("yarn_beta_slow", _C, ("arg", "--yarn-beta-slow"), "float", "YaRN slow beta",
       "YaRN's high correction dimension. Default 1.", lo=0),
    # ── speculative ──
    _o("spec", _S, ("arg", "--spec"), "int", "Draft window (tokens)",
       "How many tokens the MTP drafter proposes per check; the install's setup writes 4.", lo=0),
    _o("spec_min_p", _S, ("arg", "--spec-min-p"), "float", "Draft confidence floor",
       "How sure the draft layer must be to extend a window by another guess (setup writes 0.5).", lo=0, hi=1),
    _o("suffix_draft", _S, ("arg", "--suffix-draft"), "int", "Prompt-lookup drafting (match length)",
       "Draft from an earlier repeat of the last N+ tokens of context when it pays (default 3; 0 = MTP only).", lo=0),
    _o("mtp_max_t", _S, ("arg", "--mtp-max-t"), "int", "Cap on MTP windows",
       "Cap the MTP's windows at this many tokens (0 = the draft window; longer ones come from suffixes).", lo=0),
    _o("lookup_chain", _S, ("arg", "--lookup-chain"), "int", "Chained lookup drafts",
       "After the MTP's drafts, add up to this many prompt-lookup drafts that continue them (default 0 = off).", lo=0),
    _o("lookup_chain_min", _S, ("arg", "--lookup-chain-min"), "int", "Chained lookup match length",
       "The shortest context match a chained lookup extends on (default 3).", lo=1),
    _o("mtp_hnorm", _S, ("arg", "--mtp-hnorm"), "choice", "Draft layer input norm",
       "One RMS over all four streams (pooled, the default) or one per stream (llama.cpp's MTP graph).",
       choices=("pooled", "stream")),
    _o("mtp_q4", _S, ("arg", "--mtp-q4"), "choice", "4-bit draft copies",
       "Opt-in: the draft layer's projections and/or draft head as 4-bit copies.", choices=("all", "proj", "head")),
    _o("coupled_draft", _S, ("pair", "--coupled-draft", "--no-coupled-draft"), "switch", "Coupled draft sampling",
       "Coupled draft sampling for the MTP drafter under sampling; Off proposes argmax drafts."),
    # ── prefill and caches ──
    _o("prefill", _P, ("arg", "--prefill"), "text", "Prompt chunk size",
       "Batched prompt processing: auto (largest chunk up to 8192 the cache can lend), auto:16384 / auto:32768, or a token count.",
       placeholder="auto, auto:16384, or tokens"),
    _o("short_read", _P, ("arg", "--short-read"), "int", "Short-prompt threshold (tokens)",
       "Read at most this many fresh tokens through the decode windows instead of the batched path (default 64, 0 = off).", lo=0),
    _o("prompt_cache", _P, ("arg", "--prompt-cache"), "int", "Prompt checkpoints kept",
       "Conversation checkpoints kept between requests (default 6, about 118 MB of RAM each; 0 = read every prompt from the start).", lo=0),
    _o("prompt_cache_every", _P, ("arg", "--prompt-cache-every"), "int", "Checkpoint every N prompt tokens",
       "Also checkpoint every N fresh prompt tokens (default 16384, 0 = off).", lo=0),
    _o("prompt_cache_tail", _P, ("arg", "--prompt-cache-tail"), "switch", "Checkpoint near the prompt's end",
       "One extra checkpoint near the end of the prompt (single GPU; needs prompt checkpoints on)."),
    _o("conversation_cache_mib", _P, ("arg", "--conversation-cache-mib"), "int", "Parked conversations RAM (MiB)",
       "RAM budget for parked conversations (default 0 = off).", lo=0),
    _o("conversation_cache_slots", _P, ("arg", "--conversation-cache-slots"), "int", "Parked conversations kept",
       "At most this many parked conversations (default 4).", lo=0),
    _o("conversation_cache_min_free_mib", _P, ("arg", "--conversation-cache-min-free-mib"), "int", "RAM floor when parking (MiB)",
       "Physical RAM that must stay free when parking or restoring a session (default 2560).", lo=0),
    _o("session_min_free_mib", _P, ("arg", "--session-min-free-mib"), "int", "Disk left free by a SAVE (MiB)",
       "Disk space a session SAVE must leave free (default 4096; 0 = no check).", lo=0),
    _o("slot_save_path", _P, ("key", "slot_save_path"), "text", "Session save folder",
       "Folder where conversation sessions can be saved and restored; keep it private."),
    # ── experts and pool ──
    _o("expert_profile", _E, ("arg", "--expert-profile"), "text", "Expert profile to preload",
       "A profile.bin that pre-loads the VRAM expert tier instead of admitting on first use."),
    _o("expert_profile_save", _E, ("arg", "--expert-profile-save"), "text", "Save what the cache learned",
       "Where to save the experts the adaptive tier learned, on quit and between requests."),
    _o("expert_profile_save_every", _E, ("key", "expert_profile_save_every"), "int", "Profile save interval (min)",
       "Minutes between profile saves while serving (default 10; 0 = on quit only).", lo=0),
    _o("pool_workers", _E, ("arg", "--pool-workers"), "int", "CPU expert workers",
       "CPU expert pool worker count. 0 = every physical core except the one the host loop spins on.", lo=0),
    _o("pool_tasks", _E, ("arg", "--pool-tasks"), "int", "CPU expert tasks per phase",
       "Batched CPU expert tasks per GU/Down phase (0..4096; 0 = 3 per thread).", lo=0, hi=4096),
    _o("host_core", _E, ("arg", "--host-core"), "choice", "Host thread core",
       "Which core the host thread uses: first (default) or last physical core. Not on hybrid CPUs.", choices=("first", "last")),
    _o("pool_affinity", _E, ("arg", "--pool-affinity"), "choice", "Worker CPU affinity",
       "all (one worker per physical core), auto (hybrid CPUs: P-cores first) or p-cores only.",
       choices=("all", "auto", "p-cores")),
    _o("no_pool", _E, ("arg", "--no-pool"), "switch", "Skip the CPU expert pool",
       "The GPU-only floor: no CPU pool. Experts outside the cache are not computed on the CPU."),
    _o("no_host_worker", _E, ("arg", "--no-host-worker"), "switch", "Keep the host thread out of the pool",
       "By default the host thread joins the CPU drain; this restores the one-fewer-worker form."),
    _o("adapt_async", _E, ("arg", "--adapt-async"), "int", "Asynchronous cache adaptation",
       "With the resident RAM mode (opt-in): 1 advances the adaptive tier's swaps between verify windows; 0 is the blocking tier.",
       lo=0, hi=1),
    # ── batching and GPUs ──
    _o("gpu", _G, ("key", "gpu"), "text", "GPU(s) to run on",
       "The GPU as nvidia-smi numbers it, or several for a layer split (\"0,2\").", placeholder="0, or 0,2"),
    _o("parallel", _G, ("key", "parallel"), "int", "Requests decoded together",
       "Batch slots: up to this many conversations decode together (2..8). Each slot has its own session.", lo=0, hi=8,
       warn="Each slot costs VRAM like the main session."),
    _o("batch_mtp", _G, ("arg", "--batch-mtp"), "switch", "MTP in every batch slot",
       "Each batch slot also verifies one MTP proposal per window (one GPU; needs the draft window)."),
    _o("batch_groups", _G, ("arg", "--batch-groups"), "int", "Batch pipeline groups",
       "With a layer split: the slots in this many groups pipelined through the GPUs.", lo=1),
    _o("pipeline_windows", _G, ("arg", "--pipeline-windows"), "int", "Pipelined verify windows",
       "Layer split on two GPUs: 1 overlaps the prompt's short reads, 2 overlaps decode as well.", lo=0, hi=2),
    _o("layer_split", _G, ("key", "layer_split"), "text", "Layer split",
       "auto (placed by each card's free VRAM) or the first layer of each later card, such as 24,36,42.",
       placeholder="auto, or 24,36,42"),
    _o("trim_stage_weights", _G, ("arg", "--trim-stage-weights"), "switch", "Trim weights per GPU",
       "With an explicit layer split, each GPU loads only its own layers' dense weights."),
    _o("split_skip_if_fits", _G, ("key", "split_skip_if_fits"), "switch", "Skip the split when one card fits",
       "Run on the first card alone when it holds everything."),
    _o("peer_device", _G, ("arg", "--peer-device"), "int", "Second GPU as expert cache",
       "A second GPU used as an adaptive expert-cache tier (rows over P2P). Not with a layer split.", lo=0),
    _o("peer_reserve_mib", _G, ("arg", "--peer-reserve-mib"), "int", "Peer GPU VRAM kept free (MiB)",
       "VRAM the peer tier leaves free on its card (default 600).", lo=0),
    _o("peer_slots", _G, ("arg", "--peer-slots"), "int", "Peer GPU expert slots",
       "Expert slots on the peer (default: what fits).", lo=0),
    _o("peer_adapt_swaps", _G, ("arg", "--peer-adapt-swaps"), "int", "Peer cache swaps per step",
       "Peer cache swaps per adaptation step.", lo=0),
    _o("peer_prefill_rows", _G, ("arg", "--peer-prefill-rows"), "int", "Peer prompt rows per layer",
       "Prompt rows per layer the peer computes (default half of chunk x top-k; 0 = the prompt stays on the primary).", lo=0),
    _o("expert_cache_device1", _G, ("arg", "--expert-cache-device1"), "auto", "Expert cache on GPU 1",
       "Fill CUDA1 with this many experts, or as many as fit (auto).", lo=0, placeholder="auto, or a count"),
    _o("expert_cache_device2", _G, ("arg", "--expert-cache-device2"), "auto", "Expert cache on GPU 2",
       "Fill CUDA2 with more experts.", lo=0, placeholder="auto, or a count"),
    _o("expert_cache_device3", _G, ("arg", "--expert-cache-device3"), "auto", "Expert cache on GPU 3",
       "Fill CUDA3 with more experts.", lo=0, placeholder="auto, or a count"),
    _o("vision", _G, ("arg", "--vision"), "switch", "Accept images",
       "The server takes images too (embeddings from strata-vision)."),
    # ── files ──
    _o("pack", _F, ("arg", "--pack"), "text", "Pack folder", "The pack directory the engine loads."),
    _o("native", _F, ("arg", "--native"), "text", "Native model shard (GGUF)",
       "Shard 1 of the model GGUF: every full-context native path at once."),
    _o("ple_gguf", _F, ("arg", "--ple-gguf"), "text", "PLE table (GGUF)",
       "The n-gram/PLE shard. Without it layer 1's PLE is silently skipped, which changes every number downstream.",
       warn="Pass it for any real run."),
    _o("ple_io", _F, ("arg", "--ple-io"), "choice", "PLE table reads",
       "direct (default): unbuffered SSD reads, never in RAM; mmap: A/B arm; ram: the whole table locked in RAM.",
       choices=("direct", "mmap", "ram")),
    _o("ple_row_cache", _F, ("arg", "--ple-row-cache"), "int", "PLE row cache (rows)",
       "Bounded cache of fetched PLE rows, 90 B each (default 1048576; 0 = off).", lo=0),
    _o("ple_inflight", _F, ("arg", "--ple-inflight"), "int", "PLE outstanding reads",
       "Outstanding SSD reads for the PLE table (default 256).", lo=1),
    _o("embd_gguf", _F, ("arg", "--embd-gguf"), "text", "Token embedding (GGUF)",
       "Take the token embedding from this GGUF instead of the native shard's (BF16 pack, mapped host memory)."),
    _o("native_head_gguf", _F, ("arg", "--native-head-gguf"), "text", "Native head (GGUF)",
       "Native Q5_K head from model shard 1."),
    _o("mtp", _F, ("arg", "--mtp"), "text", "MTP draft layer folder", "The folder holding the MTP draft layer."),
    # ── server ──
    _o("idle_unload_s", _V, ("key", "idle_unload_s"), "float", "Unload after idle (s)",
       "Unload the model after this many seconds without requests; the next request loads it again (blank/0: never).", lo=0),
    _o("lazy_load", _V, ("key", "lazy_load"), "switch", "Load on first request",
       "Start without loading the model; the first request loads it (text only)."),
    _o("engine_silence_s", _V, ("key", "engine_silence_s"), "float", "End silent requests after (s)",
       "End a request when the engine says nothing for this long (default 300 s, 0 = wait).", lo=0),
    _o("api_monitor", _V, ("key", "api_monitor"), "switch", "Keep request history",
       "Keep the last 100 requests' prompts and answers in memory for the server's /api-monitor page."),
    _o("open_browser", _V, ("key", "open_browser"), "switch", "Open Strata's page when ready",
       "Open the server's chat page in the browser when the model is ready."),
    _o("fit_max_tokens", _V, ("key", "fit_max_tokens"), "switch", "Shorten max_tokens to fit",
       "Shorten a max_tokens that does not fit the context instead of answering 400."),
    _o("aliases", _V, ("key", "aliases"), "text", "Other model names",
       "Other model names the server lists and answers to (comma-separated).", placeholder="name, name"),
    _o("anthropic_thinking", _V, ("key", "anthropic_thinking"), "choice", "Thinking for Anthropic requests",
       "Requests that do not ask for thinking: think as the model does (model) or not (on_request).",
       choices=("model", "on_request")),
    _o("effort_position", _V, ("key", "effort_position"), "choice", "Where reasoning effort goes",
       "Where a non-default reasoning effort goes: start (default) or end (keeps the cache when it changes).",
       choices=("start", "end")),
    _o("reasoning_budget_tokens", _V, ("key", "reasoning_budget_tokens"), "int", "Cap thinking (tokens)",
       "Cap the thinking of every request at this many tokens (0 or blank: no cap).", lo=0),
    _o("reasoning_loop_recovery", _V, ("key", "reasoning_loop_recovery"), "choice", "Repeated-reasoning guard",
       "Detects a model repeating whole passages: off, stop (ends the reply) or recover.",
       choices=("off", "stop", "recover"), convert=(("off", False),)),
    _o("reasoning_close_retry", _V, ("key", "reasoning_close_retry"), "switch", "Retry a missing </think>",
       "Retry when the model ends its turn with no </think> and empty content."),
    _o("repeat_stop_tokens", _V, ("key", "repeat_stop_tokens"), "int", "Stop a single-token loop after",
       "End a reply that answers one token forever after this many repeats.", lo=0),
    _o("tool_call_recovery", _V, ("key", "tool_call_recovery"), "switch", "Recover odd tool calls",
       "Read a tool call written in another form out of the text (opt-in)."),
    _o("codex_thread_titles", _V, ("key", "codex_thread_titles"), "switch", "Answer Codex thread titles",
       "Answer Codex's thread-title requests without the engine (opt-in)."),
    _o("codex_compaction_cache", _V, ("key", "codex_compaction_cache"), "switch", "Cache Codex compaction",
       "Keep Codex's compaction work in the cache (opt-in)."),
    # ── sampling ──
    _o("temperature", _SA, ("sampling", "temperature"), "float", "Default temperature",
       "For requests that send none (0 = greedy).", lo=0),
    _o("top_p", _SA, ("sampling", "top_p"), "float", "Default top_p",
       "For requests that send none (0 < x <= 1).", lo=0.0001, hi=1),
    _o("top_k", _SA, ("sampling", "top_k"), "int", "Default top_k", "For requests that send none (1-64).", lo=1, hi=64),
    _o("min_p", _SA, ("sampling", "min_p"), "float", "Default min_p", "For requests that send none (0-1).", lo=0, hi=1),
)

#: Field name -> Opt, and the extra-args text field's description.
BY_FIELD: dict[str, Opt] = {o.field: o for o in OPTIONS}
FIELD_NAMES: tuple[str, ...] = tuple(BY_FIELD) + (EXTRA_FIELD,)
EXTRA_LABEL = "Extra engine arguments"
EXTRA_HELP = ("Engine flags not listed above, written as on a command line (for example --stage-timing). "
              "They replace the same flag from the model's config. Blank adds nothing.")


def options_for(section_id: str) -> tuple[Opt, ...]:
    return tuple(o for o in OPTIONS if o.section == section_id)


def choice_labels(opt: Opt) -> list[tuple[str, str]]:
    """(label, value) pairs for the Select: the blank 'as configured' first."""
    if opt.kind == "switch":
        return [("As configured", ""), ("On", "on"), ("Off", "off")]
    return [("As configured", "")] + [(c, c) for c in opt.choices]


# ── checking ────────────────────────────────────────────────────────────────

_AUTO_RE = re.compile(r"auto|\d+")
_PREFILL_RE = re.compile(r"auto(:\d+)?|\d+")


def _check_number(opt: Opt, value) -> None:
    if value is None:
        return
    if opt.lo is not None and value < opt.lo:
        raise ValueError(f"{opt.field}: {opt.label} must be at least {opt.lo:g}")
    if opt.hi is not None and value > opt.hi:
        raise ValueError(f"{opt.field}: {opt.label} must be at most {opt.hi:g}")


def split_extra(text: str) -> list[tuple[str, list[str]]]:
    """`--flag value ... --flag2` -> [(flag, [values]), ...]. ValueError when it does not start with a flag."""
    try:
        tokens = [t.strip('"\'') for t in shlex.split(text or "", posix=False)]
    except ValueError as exc:
        raise ValueError(f"{EXTRA_FIELD}: {exc}") from exc
    groups: list[tuple[str, list[str]]] = []
    for token in tokens:
        if token.startswith("--") and len(token) > 2:
            groups.append((token, []))
        elif not groups:
            raise ValueError(f"{EXTRA_FIELD}: {token!r} is not a flag; every entry starts with --")
        else:
            groups[-1][1].append(token)
    return groups


def validate(settings) -> None:
    """ValueError naming the field when a value cannot be launched. Blank is always fine."""
    for opt in OPTIONS:
        value = getattr(settings, opt.field, None)
        if value in (None, ""):
            continue
        if opt.kind in ("int", "float"):
            _check_number(opt, value)
            if opt.field == "strata_kv_resident" and value != 0 and value < 20480:
                raise ValueError("strata_kv_resident: use 0 (all in VRAM) or at least 20480 cells")
        elif opt.kind == "auto":
            text = str(value).strip().lower()
            if not _AUTO_RE.fullmatch(text):
                raise ValueError(f"{opt.field}: {opt.label} is 'auto' or a whole number, not {value!r}")
        elif opt.kind == "choice":
            if value not in opt.choices:
                raise ValueError(f"{opt.field}: {opt.label} must be one of {', '.join(opt.choices)}")
        elif opt.kind == "switch":
            if value not in ("on", "off"):
                raise ValueError(f"{opt.field}: {opt.label} must be On or Off")
        elif opt.field == "strata_prefill":
            if not _PREFILL_RE.fullmatch(str(value).strip().lower()):
                raise ValueError("strata_prefill: use auto, auto:<tokens> or a token count")
        elif opt.field == "strata_gpu":
            if not re.fullmatch(r"\d+(,\d+)*", str(value).replace(" ", "")):
                raise ValueError("strata_gpu: GPU numbers such as 0 or 0,2")
        elif opt.field == "strata_layer_split":
            if not re.fullmatch(r"auto|\d+(,\d+)*", str(value).replace(" ", "").lower()):
                raise ValueError("strata_layer_split: auto, or rising layer numbers such as 24,36,42")
    split_extra(str(getattr(settings, EXTRA_FIELD, "") or ""))


# ── applying to a launch config ─────────────────────────────────────────────

def _set_arg(args: list[str], flag: str, value: str | None) -> None:
    """Replace the flag's value (or add it). `value` None = a presence flag with no value."""
    values = [] if value is None else [value]
    if flag in args:
        i = args.index(flag)
        j = i + 1
        while j < len(args) and not str(args[j]).startswith("--"):
            j += 1
        args[i + 1:j] = values
    else:
        args += [flag, *values]


def _drop_flag(args: list[str], flag: str) -> None:
    if flag in args:
        i = args.index(flag)
        j = i + 1
        while j < len(args) and not str(args[j]).startswith("--"):
            j += 1
        del args[i:j]


def _json_value(opt: Opt, value):
    if opt.convert:
        mapped = dict(opt.convert)
        if value in mapped:
            return mapped[value]
    if opt.kind == "switch":
        return value == "on"
    if opt.field == "strata_aliases":
        return [x.strip() for x in str(value).split(",") if x.strip()] or None
    if opt.field == "strata_gpu":
        text = str(value).replace(" ", "")
        return int(text) if text.isdigit() else text
    if opt.field == "strata_layer_split":
        return str(value).strip()
    return value


def _arg_text(value) -> str:
    return str(int(value)) if isinstance(value, float) and value == int(value) and abs(value) >= 1 else str(value)


def apply(cfg: dict, settings) -> dict:
    """`cfg` with every set option applied, or `cfg` itself when none is. Never mutates `cfg`."""
    changes = [(opt, getattr(settings, opt.field, None)) for opt in OPTIONS]
    changes = [(o, v) for o, v in changes if v not in (None, "")]
    extra = split_extra(str(getattr(settings, EXTRA_FIELD, "") or ""))
    if not changes and not extra:
        return cfg
    new = {**cfg, "args": [str(a) for a in cfg.get("args") or ()]}
    args = new["args"]
    for opt, value in changes:
        kind = opt.target[0]
        if kind == "arg":
            if opt.kind == "switch":
                _set_arg(args, opt.target[1], None) if value == "on" else _drop_flag(args, opt.target[1])
            else:
                _set_arg(args, opt.target[1], _arg_text(value))
        elif kind == "pair":
            on, off = opt.target[1], opt.target[2]
            _drop_flag(args, off if value == "on" else on)
            _set_arg(args, on if value == "on" else off, None)
        elif kind == "key":
            new[opt.target[1]] = _json_value(opt, value)
        elif kind == "sampling":
            new["sampling"] = {**(new.get("sampling") or {}), opt.target[1]: value}
    for flag, values in extra:
        _drop_flag(args, flag)
        args += [flag, *values]
    # An empty alias list means "remove the key", as the server's own Settings view does.
    if new.get("aliases") is None:
        new.pop("aliases", None)
    return new
