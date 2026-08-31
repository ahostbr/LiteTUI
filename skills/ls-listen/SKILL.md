---
name: ls-listen
description: Use the local `listen` tool (Qwen2-Audio-7B) to hear audio — describe sounds, transcribe speech, characterize music. Triggers on 'what is playing', 'listen to this file', 'transcribe this audio', 'what do you hear'. Knows when its own numbers are lies and routes precise measurement to ffmpeg instead.
---

# Listen — local audio perception

You have ears: the `listen` tool runs Qwen2-Audio-7B locally (standalone
llama-server on port 8090, same GGUF+mmproj LM Studio downloaded). No cloud,
no API keys. Each real listen suspends your own model and restores it —
expect ~2–3 min per call and a slow first token afterwards (KV cache gone;
say so if the human wonders why you were quiet for a moment).

## The loop

1. **Check readiness when unsure** (cheap, never touches the seat):
   ```
   listen(action="status")
   ```
   Reports the server binary, model files, ffmpeg, and whether your own seat
   is idle enough to swap. If it says NOT FOUND or MISSING, tell the human —
   `winget install ggml.llamacpp` fixes the binary; the model files come from
   LM Studio's models dir (mradermacher/Qwen2-Audio-7B-Instruct-GGUF).

2. **Listen.**
   ```
   listen(action="file", path="C:\\path\\to\\track.mp3", question="...")
   listen(action="record", seconds=5, question="...")   # mic capture
   ```
   Any format ffmpeg reads works (wav/mp3/m4a/ogg). `device` overrides the
   microphone for record (default = first Direct Show input; list them with
   `ffmpeg -f dshow -list_devices 1 -i dummy`). Keep `question` specific —
   "transcribe exactly what is spoken" beats "describe this".

3. **Report what it heard, with the caveats below applied.**

## Trust rules (measured 2026-08-30 — do not re-litigate)

- **Trust content descriptions.** Speech transcription came back word-for-word
  on a test clip; music/sound characterization is its strong suit.
- **Never trust exact numbers it reports.** A pure 440 Hz sine was reported as
  "60 bpm", then "120 bpm" on re-run — fabricated precision, not measurement.
  If the human needs frequency, tempo, or loudness, measure with ffmpeg:
  ```
  ffmpeg -i file.wav -af volumedetect -f null -        # peak/mean dB
  ```
  (for pitch: `sox` or a short Python FFT — not the model).
- **Silence is already guarded.** Peak < −60 dB bails before any seat swap and
  reports "digital silence" — if you get that, there was nothing to hear; do
  not retry with different wording.
- **A confident description of a surprising claim** (e.g. "an ambient pop
  track in Bb minor") is worth cross-checking against the file itself before
  repeating it as fact. The model never says "I hear nothing" — it invents.

## When NOT to use it

- Long audio: Qwen2-Audio's context is 8192 tokens — fine for a track, not for
  hour-long transcription (chunk first).
- You only need duration/size/format: `ffprobe` answers in milliseconds with no
  seat swap.
- Your own seat is busy (`status` says so): the tool refuses rather than kill
  another request mid-stream — retry when idle, don't work around it.

## Troubleshooting

- **HTTP error from llama-server**: the tool returns the server's error body
  plus a log tail; full log at `C:\Projects\LiteTUI\temp-working-dir\llama_server.log`.
- **Port 8090 busy**: something else is squatting on it — check and move on.
- **"seat suspend failed" / "not suspending the seat"**: your own model was
  mid-request or another seat shares it (parallel=4). Wait, retry once.
- **Why not LM Studio's API?** Its OpenAI endpoint rejects audio content parts
  at the schema level on this build ("type must be 'text' or 'image_url'" —
  measured HTTP 400). llama.cpp accepts them; that is why a standalone server
  exists for this. If a future LM Studio build adds input_audio, the tool can
  drop the sidecar — check `listen(action="status")` era notes before assuming.
