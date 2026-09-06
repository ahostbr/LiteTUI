---
name: codex-imagegen
description: Generate or edit images through Codex's ChatGPT OAuth (no OPENAI_API_KEY). codex_image.py hits chatgpt.com/backend-api/codex and refreshes ~/.codex/auth.json. Triggers on 'codex image', 'imagegen'.
---

# Codex ImageGen — ChatGPT-credit images, no API key

Generate or edit PNGs through Ryan's Codex/ChatGPT login by spoofing the
`codex_cli_rs` client against `https://chatgpt.com/backend-api/codex`.
Stdlib-only Python: `C:\Projects\LiteTUI\skills\codex-imagegen\codex_image.py`.

## The loop

1. **Generate** (default backend = `responses`, model gpt-5.5):
   ```
   python codex_image.py "a red fox on a mossy rock at dawn" --out fox.png
   ```

2. **Edit with reference image(s)** — repeatable, png/jpg/gif/webp:
   ```
   python codex_image.py "restyle this fox in watercolor, keep the pose" --image fox.png
   ```
   `--image` routes to the edit shape automatically: `responses` gets inline
   `input_image` content parts; `images` goes to `/images/edits`.

3. **Typed endpoint** (OpenAI-style JSON, model gpt-image-2):
   ```
   python codex_image.py "..." --backend images [--model gpt-image-2]
   ```

4. **Token only**: `python codex_image.py --refresh-only` — refreshes the
   OAuth token and writes it back to `~/.codex/auth.json`.

## How auth works (measured 2026-09-05)

- Tokens live in `~/.codex/auth.json` → `tokens.{access_token, refresh_token, account_id}`.
- Access tokens are short-lived (~15 min); the script checks the JWT `exp`, and on a
  401 it force-refreshes via `POST https://auth.openai.com/oauth/token`
  (`grant_type=refresh_token`, client_id `app_EMoamEEZ73f0CkXaXp7hrann`) and retries once.
- The refresh token ROTATES — the script writes both back, so Codex CLI stays happy too.

## Trust rules (measured 2026-09-05)

- **Both generation paths work**: `responses` (gpt-5.5) and `/images/generations`
  (gpt-image-2). Real PNGs, ~20–30 s each.
- **Both edit paths work**: fox reference → watercolor restyle kept the exact pose
  and composition on both backends — the model genuinely sees the reference.
- **Model gotcha**: `gpt-5.4` returns HTTP 400 on this account; use gpt-5.5 (responses)
  or gpt-image-2 (images). Available models: `~/.codex/models_cache.json`.

## Troubleshooting

- **HTTP 401 twice / refresh failed**: no usable `refresh_token` in auth.json —
  re-login with `codex login`, then retry.
- **HTTP 400 on a model name**: wrong model for the account; check models_cache.json.
- **"stream ended without a completed image_generation_call"**: the model answered
  in text instead of calling the tool — retry once, or force `--backend images`.
