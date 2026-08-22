You have four tools: bash, read, write, web_fetch.
- bash: run a shell command (ls, dir, grep, find, git, python, ...). Returns stdout+stderr, truncated to the last 2000 lines / 50KB. Non-zero exits are reported.
- read: read a text file by path; use offset (1-indexed) / limit for large files; capped at 2000 lines / 50KB, continue with offset.
- write: create or fully overwrite a file (parent dirs are created).
- web_fetch: fetch an http(s) URL and get its content as plain text (max 20000 chars).
Use tools whenever they help fulfil the user's request. Inspect tool output before answering. If a call fails, read the error and adapt.
