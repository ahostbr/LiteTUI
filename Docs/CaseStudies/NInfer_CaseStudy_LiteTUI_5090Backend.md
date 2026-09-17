# Case Study: NInfer Patterns for a LiteTUI 5090 Backend

Intent

- This is a case study of NInfer (`E:\SAS\REPO_CLONES\ninfer`, upstream `github.com/Neroued/ninfer`), written as a pattern reference for **building NInfer as a specially-supported backend in LiteTUI for RTX 5090 end users** (Ryan's own rig: RTX 5090 32 GB, sm_120, compute capability 12.0, Windows 11).
- Scope: the HTTP serve surface, startup/capacity model, artifact format, hybrid-attention architecture, and error contract — everything a wrapper must integrate against. Excluded: kernel internals of `src/ops` (read only as far as they explain capacity), test/bench harnesses, the CLI app.

Scope and evidence sources

- `E:\SAS\REPO_CLONES\ninfer\README.md:5-8` — what NInfer is in its own words; `:25` — platform gate; `:14-18` — registered artifact identities; `:41` — no packaged binary; `:46` — artifact download command.
- `E:\SAS\REPO_CLONES\ninfer\CMakeLists.txt:9-13` — hard sm_120a build gate; `:36-40` — CUDA ≥ 13.1 gate; `:59-63` — FFmpeg/libcurl requirements.
- `E:\SAS\REPO_CLONES\ninfer\apps\serve\main.cpp:57-90` — the startup sequence (bind → engine → warmup → attach → listen → signal handlers).
- `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.h:18-21` — protocol defaults (max tokens, body cap, response-store bounds); `:35-37` — concurrency/pending fields.
- `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.cpp:48-54` — `parse_kv_dtype` accepted values; `:81` — the full flag line; `:264-269` — `--spec`/`--draft-tokens`; `:342-345` — capacity/concurrency range checks; `:368` — `resolve_public_model_id`.
- `E:\SAS\REPO_CLONES\ninfer\src\serve\http_server.h:37-50` — `HttpServer` lifecycle (bind/attach/listen/stop).
- `E:\SAS\REPO_CLONES\ninfer\src\serve\http_server.cpp:335-475` — `register_routes()`, exact path→handler wiring (routes at `:428-475`).
- `E:\SAS\REPO_CLONES\ninfer\src\serve\request.h:26-31` — `ApiError` shape.
- `E:\SAS\REPO_CLONES\ninfer\src\serve\generation_service.cpp:41-96` — RequestError → HTTP status/code mapping (the error contract); `:314` — `vision_disabled`.
- `E:\SAS\REPO_CLONES\ninfer\src\serve\anthropic_messages_response.cpp:137-144` — Anthropic overload remap to HTTP 529 / 504.
- `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:53-66` — endpoint table; `:68-70` — health semantics; `:72` — `x-request-id`; `:75-80` — keepalive/TCP_USER_TIMEOUT; `:37-50` — startup-frozen capabilities; `:125-126` — rejection list; `:157` — model-ID rule; `:228-240` — Chat streaming; `:238` — `timings`; `:335` — 413 body limit; `:584` — local Response state; `:711-713` — Anthropic streaming; `:742-748` — auth; `:788-790` — state-slot flags; `:807-808` — startup abort.
- `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\engine-architecture.md:19-27` — fixed execution model; `:43-45` — concurrency semantics; `:46-47` — out-of-contract list; `:52-70` — the four boundaries; `:213-233` — request state machine; `:284-307` — single mutation owner; `:445-458` — cancellation; `:473-481` — engine-wide failure; `:500-510` — invariants.
- `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.8-27b-artifact.md:26-27` — baked components; `:58-59` — layer split; `:198` — six frontend resources; `:507` — 1190-object total.
- `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.6-27b-model.md:40` — layer rule `(i+1) % 4 == 0` → full attention.
- `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\replayssm-gdn.md:1-20` — ReplaySSM raw-input replay concept; `:35` — GDN state 144 MiB; `:577` — ≈86× record-size ratio.
- `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\artifact-container.md:14-16` — container layout; `:28` — closed directory schema.
- `E:\SAS\REPO_CLONES\ninfer\docs\performance\qwen3.8-27b.md:70` — nvfp4 MTP0 TTFT/decode at 260K context; `:123`/`:164` — DFlash2 Structured; `:152` — DFlash2 K=7 long decode; `:143` — MTP3 Code.
- `E:\SAS\REPO_CLONES\ninfer\docs\performance\methodology.md:13-25` — common 5090 serving profile.

Evidence glossary

- `README.md:5-8` — self-description: from-scratch C++/CUDA engine, one GPU, one resident model, startup-fixed capacity 1–8 requests.
- `CMakeLists.txt:9-13` — any build with `CMAKE_CUDA_ARCHITECTURES != 120a` is a `FATAL_ERROR`; the gate is at configure time, not runtime.
- `serve_options.h:18-21` — protocol defaults: 8192 max tokens, 384 MiB max request body, 1024 / 256 MiB response-store bounds.
- `main.cpp:57-90` — bind happens *before* model load; engine warmup failure is a hard `return 1` before `listen()`.
- `http_server.cpp:428-475` — one `register_routes()` defines the entire wire surface; no dynamic route registration.
- `generation_service.cpp:41-96` — every engine-level failure maps to one of eight typed codes with fixed status/type; this is the stable integration contract.
- `qwen3.6-27b-model.md:40` — 3 of every 4 layers are GDN (linear attention); only 1/4 of layers grow a KV cache — the architectural reason 262K context fits in 32 GB.
- `replayssm-gdn.md:35` — one full recurrent-state image is 144 MiB for 27B; `:577` — speculative replay records raw transition inputs instead of per-position state snapshots (≈86× smaller).
- `performance/qwen3.8-27b.md:70` — nvfp4, 260,096 prompt tokens: 118.4 s server TTFT, 52.9 tok/s decode, single request.
- `performance/qwen3.8-27b.md:152` — DFlash2 K=7, AIME long decode (`long_decode_aime26_01`): 321.1 tok/s on nvfp4.

---

## 1) What this is (plugin/module framing)

- NInfer is a from-scratch C++/CUDA inference engine for **explicitly registered** Qwen checkpoints on a single RTX 5090, deliberately specialized: "one GPU, one resident model, and a startup-fixed capacity of one to eight active requests." Evidence: `E:\SAS\REPO_CLONES\ninfer\README.md:5-8`.
- It is *not* a general-purpose runtime. The build hard-rejects any CUDA architecture other than `120a` (sm_120a) at configure time, and the CUDA compiler must be ≥ 13.1. Evidence: `E:\SAS\REPO_CLONES\ninfer\CMakeLists.txt:9-13`, `E:\SAS\REPO_CLONES\ninfer\CMakeLists.txt:36-40`.
- Registered artifact identities include Qwen3.6-27B and Qwen3.8-27B in `groupwise-int`/`nvfp4` weight profiles, plus Qwen3.6-35B-A3B `groupwise-int`. Evidence: `E:\SAS\REPO_CLONES\ninfer\README.md:14-18`.
- The runtime is organized into four boundaries — Gateway (HTTP/CLI), Frontend (tokenizer/template/Vision semantics), Engine (queue, lifecycle, publication), Program (physical execution) — with single-ownership rules per boundary. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\engine-architecture.md:52-70`.

## 2) Platform and deployment gates (the 5090 lock)

- The build requires `CMAKE_CUDA_ARCHITECTURES=120a` exactly; anything else is `FATAL_ERROR` before CUDA compiler detection, so no generic fallback PTX is ever produced. Evidence: `E:\SAS\REPO_CLONES\ninfer\CMakeLists.txt:9-13`.
- The documented platform is "64-bit Linux, an NVIDIA GeForce RTX 5090, CUDA Toolkit 13.1 or newer, CMake 3.28 or newer"; FFmpeg dev libraries (libavformat ≥ 60, libavcodec ≥ 60, libavutil ≥ 58, libswscale ≥ 7) and libcurl ≥ 7.85 are required. Evidence: `E:\SAS\REPO_CLONES\ninfer\README.md:25`; FFmpeg/libcurl enforcement: `E:\SAS\REPO_CLONES\ninfer\CMakeLists.txt:59-63`.
- There is no install target or packaged binary distribution; the server runs from the source build tree. Evidence: `E:\SAS\REPO_CLONES\ninfer\README.md:41`.
- The `.ninfer` model files download from Hugging Face (`neroued/Qwen3.8-27B-nvfp4-NInfer`, etc.) via `hf download`. Evidence: `E:\SAS\REPO_CLONES\ninfer\README.md:14-18`, `E:\SAS\REPO_CLONES\ninfer\README.md:46`.

## 3) Artifact model (.ninfer)

- A `.ninfer` file is one self-contained image: 16-byte binary prefix, UTF-8 JSON object directory, padding to a 4096-byte payload boundary, then tensor and resource payloads. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\artifact-container.md:14-16`.
- The directory is a **closed schema** — "not a manifest for arbitrary metadata," no model graph, no kernel choice; it names objects, shapes, formats, layouts, and byte offsets. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\artifact-container.md:28`.
- The Qwen3.8-27B artifact bakes in Text + MTP + Vision + DFlash2 draft model + optimized proposal head + six frontend resources. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.8-27b-artifact.md:26-27`, `:198`.
- Model identity is read from the version-2 artifact directory (`model_id = qwen3.8-27b`, `weights_id = nvfp4`, `recipe_id = qwen3_8_27b_nvfp4-v2`); filename, object count, and tensor shapes never select the model. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.8-27b-artifact.md:20-23`, `:43`.
- The current artifact is one complete image of **1190 objects** (`1184 + 6`). Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.8-27b-artifact.md:507`.
- Qwen3.8-27B is a **hybrid** model: 64 text layers, of which exactly 16 are full-attention and 48 are Gated DeltaNet (linear attention); the rule is `(i+1) % 4 == 0` → full attention. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.6-27b-model.md:40`, `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.8-27b-artifact.md:58-59`.
- Consequence for capacity: the growing KV cache exists on only 1/4 of the layers, while each GDN layer holds a fixed-size FP32 recurrent state (one full state image = 144 MiB for 27B). Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\replayssm-gdn.md:35`.

## 4) HTTP API surface (the integration contract)

- The complete route table, registered in one place (`register_routes()`): `GET /health`, `GET /v1/models`, `GET /v1/models/{id}`, `POST /v1/chat/completions`, `POST /v1/responses`, `POST /v1/responses/input_tokens`, `GET/DELETE /v1/responses/{id}`, `GET /v1/responses/{id}/input_items`, `POST /v1/responses/{id}/cancel`, `POST /v1/responses/compact`, `POST /v1/messages`, `POST /v1/messages/count_tokens`. Evidence: `E:\SAS\REPO_CLONES\ninfer\src\serve\http_server.cpp:428-475`; doc table at `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:53-66`.
- The request `model` field **must equal the public model ID** — the artifact's `identity.model_id` (e.g. `qwen3.8-27b`) unless overridden with `--model-id`, which is an HTTP alias only and does not select the artifact. Evidence: `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.cpp:368`, `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:157`, `:405`.
- Authentication: optional `--api-key`; when set, it is required as an OpenAI bearer token or Anthropic `x-api-key` on all endpoints except `GET /health` and CORS preflight; `--cors` adds permissive browser headers, off by default. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:742-748`, `E:\SAS\REPO_CLONES\ninfer\src\serve\http_server.cpp:362-377` (pre-routing auth check).
- Every OpenAI-compatible response carries a unique `x-request-id` header, including streaming and error responses; Anthropic endpoints use their own `request-id` contract. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:72`.
- All three generation SSE endpoints emit a standard `: keep-alive` comment after 5 s of protocol silence; on Linux, accepted connections additionally use TCP keepalive + 15 s `TCP_USER_TIMEOUT`, cancelling a dead peer within ~20 s. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:75-80`.
- Streaming shapes: Chat Completions begins with an assistant-role chunk then reasoning/content deltas; the Responses API streams typed semantic events with a monotonically increasing `sequence_number`; Anthropic emits `message_start` after Engine admission commits the prefix selection. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:228-240`, `:560`, `:711-713`.
- A llama.cpp-compatible `timings` object (prompt/decode tok/s, cache hit counts) is attached to successful responses — a useful observability hook for a wrapper. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:238`.
- `GET /health` returns 200 `{"status":"ok"}` while the engine can accept work and 503 `{"status":"unavailable"}` after an engine-wide failure; temporary queue saturation does *not* flip it. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:68-70`.

## 5) Startup-fixed capability model (flags freeze behavior)

- Capabilities are chosen once at process start: "A later request cannot enable a capability [omitted at startup]." Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:49-50`.
- `--vision` is off by default; without it, media and token-count requests fail with HTTP 400 `vision_disabled`. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:37-45`, `E:\SAS\REPO_CLONES\ninfer\src\serve\generation_service.cpp:314`.
- Speculative decoding is likewise frozen: `--spec mtp|dflash|dflash2` + `--draft-tokens`. Omitting `--spec` loads no speculative backend. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:45-48`, `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.cpp:264-269`, `:81`.
- KV-cache dtype is a startup choice from `bf16|int8|fp8|nvfp4|k8v4`. Evidence: `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.cpp:48-54`, `:81`.
- Context/KV sizing: `--max-context` is the per-request logical ceiling; `--kv-capacity` (or `auto`) sizes the shared Main-Text KV pool and must be ≥ `--max-context`. Evidence: `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.cpp:342`.
- Concurrency: `--max-concurrency` is 1–8 (hard range check); `--max-pending-requests` (default 16) bounds the FIFO wait queue; `--pending-timeout-ms` (default 30,000) bounds prepare+admission wait. Evidence: `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.cpp:345`, `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.h:35-37`.
- Body limit: `--max-request-mib` (default 384) is enforced **before JSON parsing**; exceeding it yields HTTP 413 `request_too_large`. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:335`, `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.h:19`.
- Startup sequence is deterministic: parse options → `HttpServer.bind()` (reserves the address before model load) → `GenerationService` (loads artifact) → `service.warmup()` (failure = `return 1`) → `server.attach()` → `listen()`. SIGINT/SIGTERM call `stop()` for a clean shutdown. Evidence: `E:\SAS\REPO_CLONES\ninfer\apps\serve\main.cpp:57-90`, `E:\SAS\REPO_CLONES\ninfer\src\serve\http_server.h:37-50`.
- A malformed `--context-cost-presets` file aborts startup; a bad artifact or an over-budget capacity configuration means the engine never advertises readiness — the "refuses to start rather than OOM mid-session" behavior. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:807-808`, `E:\SAS\REPO_CLONES\ninfer\apps\serve\main.cpp:57-90`.

## 6) Error model (typed, stable, machine-readable)

- All API errors are one struct: `{status, type, message, param?, code?}`. Evidence: `E:\SAS\REPO_CLONES\ninfer\src\serve\request.h:26-31`.
- Engine-level failures map to a fixed code table (the stable contract a wrapper should switch on): `context_length_exceeded` → 400; `thinking_budget_capacity_insufficient` → 400; `media_budget_exceeded` → 400; `invalid_media` → 400; `server_overloaded` → 429 `rate_limit_error`; `request_queue_timeout` → 503 `server_error`; `client_disconnected` (cancellation) → 499; `service_unavailable` → 503. Evidence: `E:\SAS\REPO_CLONES\ninfer\src\serve\generation_service.cpp:41-96`.
- Anthropic-facing overload remaps `server_overloaded`/429 to HTTP 529 `overloaded_error`; queue/media timeouts become 504. Evidence: `E:\SAS\REPO_CLONES\ninfer\src\serve\anthropic_messages_response.cpp:137-144`.
- Capability rejections are explicit, field-specific 400s — JSON constrained output, `logit_bias ≠ 0`, logprobs, audio, `strict:true`, named tool choice, web search, etc. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:125-126`, `:520-521`.
- Unknown top-level fields are rejected with `unknown_parameter` on the Responses endpoint. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:432`.
- Engine-wide failure (unrecoverable shared physical state) is terminal for the process: internal invariant errors "cannot be demoted to cache miss, wait, or retry," and `/health` flips to 503. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\engine-architecture.md:473-481`, `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:68-70`.

## 7) Capacity, scheduling, and lifecycle (how one GPU serves 1–8 lanes)

- Request lifecycle: `Waiting → Materializing → Prefill → DecodeReady|ControlReady → TerminalPending → Finished`; a request enters Active only after its complete execution resources are guaranteed, and never loses that guarantee once active (no preemption). Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\engine-architecture.md:213-233`, `:27`.
- `max_concurrency` caps active requests; it does **not** partition the shared KV pool per lane. Two requests run concurrently "when their complete reservations fit." Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\engine-architecture.md:43-45`, `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:24-30`.
- Device checkpoint capacity is `C + H` StateImages (C = `--max-concurrency`, H = `--device-state-slots`); Host-side retention is `--host-state-slots` (default 8) pinned StateImages plus `--host-kv-mib` (default 8192) shared pinned Host KV. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:788-790`.
- Only one global resource-topology transition at a time; only the engine worker thread mutates request/scheduler/program state. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\engine-architecture.md:284-307`, `:506`.
- Cancellation is honored at worker boundaries: Waiting → dropped; Materializing → transition aborted; Active → finishes current unit then releases. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\engine-architecture.md:445-458`.
- OpenAI Responses state is **process-local** and LRU-bounded (default 1024 records / 256 MiB); lost on restart; `store:false` responses are unretrievable. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\serving.md:584`, `E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.h:20-21`.

## 8) Hybrid architecture and measured performance (why 262K fits, how fast it is)

- Architecture: 64 text layers, full-attention on every 4th layer (16 layers), GDN on the other 48 (16 k-heads × 128, 48 v-heads × 128). Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.6-27b-model.md:40`, `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\qwen3.8-27b-artifact.md:58-59`.
- One full GDN recurrent-state image is 144 MiB (27B) — fixed regardless of context length — so 262,144-token context is capacity-feasible on one 32 GB card with quantized KV (`nvfp4`/`k8v4`). Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\replayssm-gdn.md:35`.
- Speculative decoding over GDN uses **ReplaySSM**: verify records raw transition inputs per position instead of storing full state snapshots, then replays only the accepted prefix with the *identical* finite-precision transition to stay bit-exact. Record traffic is ≈86× smaller than per-position state snapshots for the 27B model. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\maintainer\replayssm-gdn.md:1-20`, `:577`.
- Measured (RTX 5090, nvfp4, single request, MTP0): at 260,096 prompt tokens — server TTFT 118.4 s, decode 52.9 tok/s; at 7,680 tokens — TTFT 0.93 s, decode 71.2 tok/s. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\performance\qwen3.8-27b.md:70`, `:66`.
- Measured (nvfp4, DFlash2 K=7): AIME long-reasoning decode 321.1 tok/s (`long_decode_aime26_01`); Structured category 356.8 tok/s; groupwise-int DFlash2 Structured 267.3 tok/s; MTP3 Code 194.3 tok/s. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\performance\qwen3.8-27b.md:152`, `:164`, `:123`, `:143`.
- Prefill throughput degrades gracefully with context (8,340 tok/s @ 7.7K → 2,203 tok/s @ 260K for nvfp4) — long-context TTFT is dominated by the full-attention suffix, consistent with the 16/64 attention-layer ratio. Evidence: `E:\SAS\REPO_CLONES\ninfer\docs\performance\qwen3.8-27b.md:66-70`.

## 9) LiteTUI 5090-backend design notes (adaptation guidance)

These are recommendations based on the observed patterns above, not requirements.

- **Pattern takeaway: treat the server as a frozen capability box.** Every interesting knob (vision, speculative backend, KV dtype, context ceiling, concurrency) is a startup flag, and the process cannot widen them later (`E:\SAS\REPO_CLONES\ninfer\docs\serving.md:49-50`). A LiteTUI backend profile for 5090 users should be a *named launch config* (e.g. `ninfer-5090-longctx`: `--max-context 240000 --kv-capacity 240000 --kv-dtype fp8 --spec dflash2 --draft-tokens 7 --lm-head-draft --vision --max-concurrency 2 --device-state-slots 2 --host-state-slots 8 --host-kv-mib 8192`), spawned per profile — not a mutable session.
- **Pattern takeaway: health + model-ID are the only discovery you need.** `GET /health` (unauthenticated) tells you readiness; `GET /v1/models` gives the exact public model ID you must echo in every request (`E:\SAS\REPO_CLONES\ninfer\docs\serving.md:68-70`, `:157`). A wrapper should poll `/health` after spawn, read `/v1/models`, and pin the ID — no capability-negotiation protocol exists.
- **Pattern takeaway: switch on the `code` field, not the status.** The code table is small, stable, and cross-dialect (`E:\SAS\REPO_CLONES\ninfer\src\serve\generation_service.cpp:41-96`): `context_length_exceeded` (trim/summarize), `server_overloaded` (429 — back off, the queue is full), `request_queue_timeout` (503 — retry), `service_unavailable` (engine-wide failure — the process is done; relaunch from the launch config). Mapping `service_unavailable` to a *relaunch* rather than a retry is the key lifecycle difference from vLLM/llama.cpp backends.
- **Pattern takeaway: use Chat Completions + `timings` for the main UI path, Responses for agent state.** The llama.cpp-compatible `timings` object (`E:\SAS\REPO_CLONES\ninfer\docs\serving.md:238`) gives prompt/decode tok/s and cache-hit tokens per request — a ready-made status line for a TUI. For multi-turn agent loops, `/v1/responses` with `previous_response_id` gives server-side continuation, but it is process-local and LRU-bounded (`E:\SAS\REPO_CLONES\ninfer\docs\serving.md:584`) — LiteTUI should still keep its own transcript and treat stored responses as a cache, not a source of truth.
- **Pattern takeaway: respect the 384 MiB body ceiling and 30 s pending timeout as hard client-side limits** (`E:\SAS\REPO_CLONES\ninfer\src\serve\serve_options.h:19`, `:37`): media should go as data-URIs below the body cap or via HTTP(S) URLs the server fetches; a UI spinner should assume up to ~30 s before admission plus a context-proportional TTFT (118 s at 260K tokens, `E:\SAS\REPO_CLONES\ninfer\docs\performance\qwen3.8-27b.md:70`).
- **Pattern takeaway: the Windows gap is the main deployment risk for "5090 end users."** Upstream documents 64-bit Linux only (`E:\SAS\REPO_CLONES\ninfer\README.md:25`) with a hard sm_120a gate (`E:\SAS\REPO_CLONES\ninfer\CMakeLists.txt:9-13`). Ryan's box is Windows 11 with an sm_120 5090, so options are: (a) build under WSL2 (kernel-level CUDA works on WSL2; the `TCP_USER_TIMEOUT` keepalive is Linux-side — acceptable), (b) track/use the community MSVC port (`github.com/headpiece747/ninfer-5090-windows`, a native Windows 5090 port with NVFP4 + MTP), or (c) document a remote-serve mode where LiteTUI talks to a Linux box over the LAN. The HTTP contract is platform-neutral, so the wrapper code is identical in all three cases — only the launcher differs.
- **Pattern takeaway: don't try to be vLLM.** NInfer explicitly excludes multi-GPU placement, preemption, priority/QoS, cross-engine context stores, and large-scale continuous batching from its contract (`E:\SAS\REPO_CLONES\ninfer\docs\maintainer\engine-architecture.md:46-47`). A LiteTUI backend that assumes single-user, 1–8 lanes, FIFO admission with a bounded pending queue matches the engine's actual model and its error codes.
