"""relayctl - a small TUI to start, kill, restart and watch the PyBridge relay.

    python relayctl.py

Keys:  s start   k kill   r restart   c clear log   q quit

No dependencies beyond what bridge.py already needs. Liveness is probed by
talking the real protocol to the relay's script port rather than by trusting a
pidfile - a stale pidfile and a running relay look identical otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

from websockets.asyncio.client import connect as ws_connect

from bridge import DEFAULT_CLIENT_PORT, DEFAULT_HOST, DEFAULT_PORT, HERE, PID_FILE

OUT_LOG = os.path.join(HERE, "relay.out.log")
ERR_LOG = os.path.join(HERE, "relay.err.log")
BRIDGE = os.path.join(HERE, "bridge.py")

REFRESH_SECONDS = 2.0
LOG_LINES = 12

RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
GREEN, YELLOW, RED, CYAN = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[36m"


# --------------------------------------------------------------- probing

def probe(timeout: float = 1.5) -> tuple[str, str]:
    """Returns one of: up / no-extension / down, plus a detail string.

    Deliberately NOT bridge.Chrome: that would try to bind 7461 first, and if
    the relay were down it would silently become the server itself - the tool
    would then report healthy because it had taken the port it was checking.
    """
    try:
        return asyncio.run(_probe(timeout))
    except Exception as exc:
        return "down", str(exc)


async def _probe(timeout: float) -> tuple[str, str]:
    url = "ws://{}:{}".format(DEFAULT_HOST, DEFAULT_CLIENT_PORT)
    try:
        async with ws_connect(url, open_timeout=timeout) as ws:
            await ws.send(json.dumps({"id": 1, "cmd": "ping", "args": {}}))
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("ok"):
                return "up", "extension v{}".format(
                    (msg.get("result") or {}).get("version", "?")
                )
            return "no-extension", str(msg.get("error", ""))
    except OSError:
        return "down", "nothing listening on {}".format(DEFAULT_CLIENT_PORT)
    except asyncio.TimeoutError:
        return "no-extension", "relay answered nothing within {}s".format(timeout)


def read_pid() -> int | None:
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


# ------------------------------------------------------------- lifecycle

def start() -> str:
    state, _ = probe(1.0)
    if state != "down":
        return "already running"

    flags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP - so the relay outlives
        # this TUI and never inherits its console. An attached child would spray
        # its output into whatever terminal started us.
        flags = 0x00000008 | 0x00000200

    out = open(OUT_LOG, "a", encoding="utf-8")
    err = open(ERR_LOG, "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, BRIDGE, "serve"],
        stdout=out,
        stderr=err,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        cwd=HERE,
        close_fds=True,
    )
    for _ in range(20):
        time.sleep(0.25)
        if probe(0.5)[0] != "down":
            return "started"
    return "started (not answering yet)"


def wait_for_extension(seconds: float = 12.0) -> str:
    """Block until the Chrome extension has connected, or say it did not.

    start() returns when the RELAY answers. The extension is a separate client
    that reconnects on its own timer, so there is a window where the port is
    open and every command still fails. Callers that report "started" during
    that window are reporting readiness nobody has.
    """
    deadline = time.time() + seconds
    state, detail = probe(1.0)
    while state != "up" and time.time() < deadline:
        time.sleep(0.5)
        state, detail = probe(1.0)
    if state == "up":
        return "extension connected ({})".format(detail)
    return "relay up, extension NOT connected yet ({}) - it reconnects on its own timer; retry".format(detail)


def port_owner(port: int) -> int | None:
    """PID actually LISTENING on a port. The pidfile is a claim; this is the fact."""
    if sys.platform != "win32":
        try:
            out = subprocess.run(
                ["lsof", "-t", "-iTCP:{}".format(port), "-sTCP:LISTEN"],
                capture_output=True, text=True, check=False,
            ).stdout.split()
            return int(out[0]) if out else None
        except (OSError, ValueError):
            return None
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return None
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0] == "TCP" and f[1].endswith(":" + str(port)) and f[3] == "LISTENING":
            try:
                return int(f[4])
            except ValueError:
                return None
    return None


def kill() -> str:
    pid = read_pid()

    # A stale pidfile next to a LIVE relay is possible whenever a relay was
    # started outside relayctl, or by an older build that stamped its pid before
    # binding. Trusting the file alone made kill report success while the relay
    # kept running, so fall through to whoever actually holds the port.
    if pid is None or not pid_alive(pid):
        stale = pid
        pid = port_owner(DEFAULT_PORT)
        if pid is None:
            try:
                if os.path.exists(PID_FILE):
                    os.remove(PID_FILE)
            except OSError:
                pass
            return (
                "nothing listening - nothing to kill"
                if stale is None
                else "stale pidfile cleared (pid {} was dead)".format(stale)
            )

    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        os.kill(pid, 15)

    for _ in range(20):
        time.sleep(0.2)
        if not pid_alive(pid):
            break
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except OSError:
        pass
    return "killed pid {}".format(pid)


def restart() -> str:
    return "{}; {}".format(kill(), start())


def clear_logs() -> str:
    for path in (OUT_LOG, ERR_LOG):
        try:
            open(path, "w", encoding="utf-8").close()
        except OSError:
            pass
    return "logs cleared"


# ---------------------------------------------------------------- render

def tail(path: str, n: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip() for ln in f.readlines()[-n:]]
    except OSError:
        return []


def render(message: str) -> None:
    state, detail = probe(1.0)
    pid = read_pid()
    alive = pid is not None and pid_alive(pid)

    if state == "up":
        badge, colour = "RUNNING", GREEN
    elif state == "no-extension":
        badge, colour = "NO EXTENSION", YELLOW
    else:
        badge, colour = "STOPPED", RED

    uptime = ""
    if alive:
        try:
            uptime = "  up {}".format(_hms(time.time() - os.path.getmtime(PID_FILE)))
        except OSError:
            pass

    sys.stdout.write("\x1b[H\x1b[2J")
    w = 74
    print("{}{}  PyBridge relay {}{}".format(BOLD, CYAN, RESET, DIM + HERE + RESET))
    print(DIM + "-" * w + RESET)
    print("  status   {}{}{}  {}{}".format(colour + BOLD, badge, RESET, DIM + detail + RESET, uptime))
    print("  pid      {}".format(pid if alive else (DIM + "none" + RESET if pid is None else RED + "{} (dead)".format(pid) + RESET)))
    print("  ports    {} extension   {} scripts".format(DEFAULT_PORT, DEFAULT_CLIENT_PORT))
    print(DIM + "-" * w + RESET)

    lines = tail(OUT_LOG, LOG_LINES)
    errs = tail(ERR_LOG, 3)
    print("  {}log{} ({})".format(BOLD, RESET, os.path.basename(OUT_LOG)))
    if not lines:
        print("    " + DIM + "(empty)" + RESET)
    for ln in lines:
        print("    " + ln[: w - 4])
    if any(errs):
        print("  {}stderr{}".format(RED + BOLD, RESET))
        for ln in errs:
            if ln:
                print("    " + RED + ln[: w - 4] + RESET)

    print(DIM + "-" * w + RESET)
    print("  {}s{} start   {}k{} kill   {}r{} restart   {}c{} clear log   {}q{} quit".format(
        BOLD, RESET, BOLD, RESET, BOLD, RESET, BOLD, RESET, BOLD, RESET))
    if message:
        print("  " + CYAN + message + RESET)
    sys.stdout.flush()


def _hms(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return "{}s".format(s)
    if s < 3600:
        return "{}m {}s".format(s // 60, s % 60)
    return "{}h {}m".format(s // 3600, (s % 3600) // 60)


# ----------------------------------------------------------------- input

def read_key(timeout: float) -> str | None:
    """One keypress, or None once timeout expires. Never blocks the refresh."""
    if sys.platform == "win32":
        import msvcrt

        deadline = time.time() + timeout
        while time.time() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                return ch.lower()
            time.sleep(0.05)
        return None

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if select.select([sys.stdin], [], [], timeout)[0]:
            return sys.stdin.read(1).lower()
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main() -> int:
    message = ""
    try:
        while True:
            render(message)
            message = ""
            key = read_key(REFRESH_SECONDS)
            if key is None:
                continue
            if key == "q":
                break
            if key == "s":
                message = start()
            elif key == "k":
                message = kill()
            elif key == "r":
                message = restart()
            elif key == "c":
                message = clear_logs()
    except KeyboardInterrupt:
        pass
    sys.stdout.write("\x1b[?25h")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
