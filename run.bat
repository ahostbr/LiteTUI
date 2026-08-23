@echo off
REM Launch LiteTUI from the LOCKED project environment.
REM
REM This used to be `python src\app.py`, which runs whatever interpreter is
REM first on PATH. That is how the project came to have two disagreeing
REM environments: the global one had textual 8.0.2 / openai 2.36.0, the
REM project's .venv had 8.1.0 / 2.26.0, and onboarding said `uv sync` while
REM this file said otherwise. Whichever you hit was luck.
REM
REM `--locked` is the load-bearing flag: it refuses to run if uv.lock is out
REM of date with pyproject.toml rather than silently resolving something new,
REM so the app and the test gate cannot drift apart without someone being told.
cd /d "%~dp0"
uv run --locked litetui %*
pause
