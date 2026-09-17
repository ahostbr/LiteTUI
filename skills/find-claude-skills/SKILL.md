---
name: find-claude-skills
description: Find and run a Claude skill that lives in the user's .claude directory (C:\Users\Ryan\.claude) — the top-level skills folder and the plugin cache. Use when the user says "use the <name> skill", "read that skill", "grab a transcript with the skill", or references a skill that is not in this project's local skills list.
---

# Find Claude skills

A skill is a folder with a `SKILL.md` (often plus helper scripts like a
`.ps1`). On this machine the skills Ryan has installed live under
`C:\Users\Ryan\.claude`, **not** in this project. There are two places to
look, and one index that helps but can lie.

## Where skills live

1. **User skills** — `C:\Users\Ryan\.claude\skills\<name>\SKILL.md`
   (ls-arch, handoff, sandbox-this, pitborn-doctrine, sentinel, ...).
   Most are folders, but a few are bare `.md` files (e.g.
   `rebuild-release.md`) — read those directly.

2. **Plugin skills** —
   `C:\Users\Ryan\.claude\plugins\cache\<marketplace>\<plugin>\<version>\skills\<name>\SKILL.md`
   e.g. `liteharness\liteharness\1.0.14\skills\ls-youtube-transcript`.
   The cache keeps **every** version a plugin has shipped.

3. **Description index** — `C:\Users\Ryan\.claude\skill-descriptions.md`
   a generated list of every skill's description string. Good for the
   "which skill handles X?" question. It is a snapshot (generated
   2026-03-07), so trust the folders when they disagree.

## Find a skill

```
dir "C:\Users\Ryan\.claude\skills" /b
```

If it is not there, search the plugin cache by skill folder name:

```
powershell -NoProfile -Command "Get-ChildItem 'C:\Users\Ryan\.claude\plugins\cache' -Recurse -Directory | Where-Object { $_.Name -eq '<name>' } | Select-Object -ExpandProperty FullName"
```

Several versions will come back (liteharness has 1.0.9, 1.0.12, 1.0.13,
1.0.14). **Use the highest version.**

## Use a skill

1. **Read `SKILL.md` in full.** Frontmatter first: `description` says what
   it does, `allowed-tools` says what it is allowed to run. The body is the
   procedure. Follow it exactly, including its exact commands — do not
   improvise alternatives to the skill's tested path.
2. **Run the skill's own scripts from its own folder.** They are written
   relative to it. Example:
   ```
   powershell -ExecutionPolicy Bypass -File "C:\Users\Ryan\.claude\plugins\cache\liteharness\liteharness\1.0.14\skills\ls-youtube-transcript\Get-YouTubeTranscript.ps1" -Url "<url>"
   ```
3. **Handle the skill's error codes.** The SKILL.md usually carries a table
   (e.g. exit 2 = no English subtitles → run `yt-dlp --list-subs` and offer
   the available languages). Follow that table instead of guessing.

## Pitfalls

- **Masked output.** Long skill output gets masked by the harness; the mask
  line gives you the full text at a `tool-raw\*.txt` path. Read that file in
  small chunks — `type`-ing the whole thing at once just gets masked again.
- **Version drift.** A plugin update adds a new cache folder, not a
  replacement. Always resolve the highest version before running.
- **The index is a snapshot.** A skill can be added, renamed, or moved after
  `skill-descriptions.md` was generated. The folders are the source of truth.
