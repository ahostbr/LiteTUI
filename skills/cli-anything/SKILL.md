---
name: cli-anything
description: >-
  Use the CLI-Anything ecosystem — agent-native CLIs (cli-hub + cli-anything-<name> pip
  packages) that make professional software controllable from the command line: Blender,
  GIMP, Krita, Inkscape, FreeCAD, Godot, ComfyUI, Kdenlive, Shotcut, OBS, MuseScore,
  Audacity, LibreOffice, Obsidian, Zotero, Joplin, QGIS, Ollama, n8n, and ~90 more.
  Triggers on 'use the cli for', 'cli-hub', 'cli-anything', 'drive blender/gimp/obsidian
  from the command line', 'find a CLI for X', 'install a cli-hub tool', or any task
  whose target software has an agent harness here.
---

# CLI-Anything — drive professional software from the CLI

CLI-Anything (HKUDS) is a marketplace of **agent-native CLI harnesses**: each
one wraps a piece of professional software (Blender, GIMP, Kdenlive, Obsidian,
…) as a `click`-based command-line tool with `--json` output, a REPL, and
stateful JSON project files. 103 CLIs, 35 categories, plus 5 **matrices**
(workflow bundles of capabilities × providers).

- Web hub: https://clianything.cc
- Repo: https://github.com/HKUDS/CLI-Anything
- **Local clone on this box:** `E:\SAS\REPO_CLONES\CLI-Anything`
  (full source + every CLI's SKILL.md under `skills\cli-anything-<name>\`)

## This machine's state (verified 2026-07)

- `cli-hub` **0.4.1 installed and on PATH** (global Python 3.11,
  `C:\Users\Ryan\AppData\Local\Programs\Python\Python311\Scripts`).
  `python` on PATH resolves to the LiteTUI venv — irrelevant here because
  you invoke the `cli-anything-<name>` executables directly, and
  `cli-hub install` uses its own interpreter.
- Nothing from the hub is installed yet (all matrices show `0/N installed`).
- The local clone is the source of truth for per-CLI command docs, even
  before you install anything.

## The loop

1. **Discover** (live catalog, no install needed):
   ```
   cli-hub list                          # all CLIs by category
   cli-hub search "image editing"        # keyword search
   cli-hub info <name>                   # requires / entry point / skill doc / status
   ```
   `info` tells you the **prerequisite software** (e.g. GIMP wants
   `gimp` — on this Windows box that's `winget install GIMP.GIMP`, not apt).

2. **Install** (each CLI is its own pip package):
   ```
   cli-hub install <name>                # -> pip package cli-anything-<name>
   cli-hub update <name> / uninstall <name>
   ```

3. **Read the per-CLI skill doc BEFORE guessing commands.**
   Canonical location in the local clone:
   ```
   E:\SAS\REPO_CLONES\CLI-Anything\skills\cli-anything-<name>\SKILL.md
   ```
   (Same docs ship inside the installed package.) This is the difference
   between `cli-anything-blender scene info` and flailing.

4. **Run one-shot commands** — never the bare REPL from an agent (it blocks
   on stdin):
   ```
   cli-anything-<name> <group> <command> [args]
   cli-anything-<name> --json <group> <command>   # machine-readable output
   ```
   Many CLIs are **stateful**: create a project file first
   (`--project scene.json` / `scene new -o scene.json`) and pass it back on
   later calls.

5. **Verify** the result (output JSON, file written, software state), then
   report.

## Matrices — multi-tool workflows

A matrix maps capabilities (e.g. `text.transcribe`, `visual.generate`) to
providers across many CLIs. Reach for one when a task spans several tools.

```
cli-hub matrix list                       # 3d-cad, game-development, image-design,
                                          # knowledge-research, video-creation
cli-hub can "transcribe audio"            # find the capability
cli-hub matrix preflight video-creation --json        # exit 3 = gaps here
cli-hub matrix install video-creation --capability text.transcribe   # scoped install
cli-hub matrix doctor <name>              # audit an install
```

**Scope installs** — `--capability <id>` / `--only a,b` / `--dry-run` to
preview. Never bulk-install a 14-CLI matrix for a one-capability job.
Exit codes: 0 ok · 1 failure · 2 usage · 3 partial/gaps.
Retry with `--resume`. After install, the matrix renders a local SKILL.md
with provider-selection rules — read it.

## Preview workflows

Some harnesses publish preview artifacts:

```
cli-anything-<name> --json --project <p>.json preview capture --recipe quick
cli-hub previews inspect <bundle-or-session>
cli-hub previews html <bundle> -o page.html
cli-hub previews watch <session> --open
```

## Commonly useful (from the live catalog)

| Need | CLIs |
|---|---|
| 3D / CAD | blender, freecad (258 commands), meerk40t (laser) |
| Image | gimp, krita, inkscape, sketch, drawio, mermaid |
| AI media | comfyui, minimax, openwebui, ollama, notebooklm, generate-veo-video |
| Video | kdenlive, shotcut, openscreen, obs-studio, streamlabs, video-captioner |
| Audio / music | audacity (via sox), musescore, wavetone |
| Productivity | libreoffice, obsidian, zotero, joplin, siyuan, mubu |
| GIS / data | qgis, openrefine, py4csr, chromadb |
| Dev / web | godot, n8n, dify-workflow, wiremock, browser, safari, lldb, renderdoc |
| Comms | zoom, feishu, x-twitter-scraper |

Full list: `cli-hub list`.

## Gotchas (learned on this box)

- **Prerequisites are real.** The harness drives the actual software
  (`blender --background --python`, `gimp -i -b`, sox, …). If the app isn't
  installed the harness errors — check `cli-hub info` first; on Windows the
  apt hints don't apply (`winget install` instead).
- **REPL trap:** bare `cli-anything-<name>` = interactive prompt-toolkit
  session. In automated context that's a hang. Always pass a subcommand.
- **`--json` is the agent contract** — parse it; human text is for display.
- **Registry install hints** say `pip install …` — fine, but on this box
  prefer `cli-hub install <name>` so the hub tracks the install
  (`info`/`update`/`uninstall` then work against it).
- Some CLIs need **API keys or a running service** (ollama, n8n, chromadb,
  mailchimp, exa) — `info` + the skill doc say which.
- **Building a new harness** for software that isn't in the catalog: the
  repo has the pattern — per-app `agent-harness/` dirs (setup.py +
  `cli_anything/<name>/` click package), CONTRIBUTING.md, and the
  `/cli-anything` generation workflow. Read one close analog (e.g.
  `blender/agent-harness`) before starting.

## Quick start from zero

```
pip install cli-anything-hub        # if cli-hub not on PATH
cli-hub search <what-you-need>
cli-hub info <name>                 # check prerequisites
cli-hub install <name>
# read E:\SAS\REPO_CLONES\CLI-Anything\skills\cli-anything-<name>\SKILL.md
cli-anything-<name> --json <group> <command>
```
