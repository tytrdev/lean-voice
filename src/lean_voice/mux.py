"""Terminal multiplexer adapters: find the pane Claude runs in, and type into it.

Each adapter knows three things:
  detect   is this process running inside the muxer (from environment variables)?
  target   how to address the current pane later, from another process
  send     type text into that pane and press Enter

`lv-claude` calls `python -m lean_voice.mux register` from inside the Claude pane.
That writes ~/.cache/lean-voice/target.json, and the STT daemon delivers
transcripts there. To add a muxer, subclass Adapter and add it to ADAPTERS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

TARGET = Path.home() / ".cache/lean-voice/target.json"


@dataclass
class Target:
    mux: str
    pane: str
    env: dict[str, str]  # muxer env vars the CLI needs when called from outside the pane
    cwd: str = ""


class Adapter:
    name = ""
    detect_var = ""  # set inside the muxer's panes
    env_vars: tuple[str, ...] = ()  # forwarded so the CLI can find the right server/session
    tested = True

    def detect(self, env) -> bool:
        return bool(env.get(self.detect_var))

    def target(self, env) -> Target:
        return Target(self.name, env[self.detect_var], {k: env[k] for k in self.env_vars if env.get(k)})

    def commands(self, t: Target, text: str) -> list[list[str]]:
        raise NotImplementedError

    def send(self, t: Target, text: str) -> None:
        env = {**os.environ, **t.env}
        for i, cmd in enumerate(self.commands(t, text)):
            if i:
                time.sleep(0.05)  # let the TUI take the text before Enter arrives
            subprocess.run(cmd, env=env, check=True, timeout=5, capture_output=True)


class Tmux(Adapter):
    name, detect_var, env_vars = "tmux", "TMUX_PANE", ("TMUX",)

    def commands(self, t, text):
        sock = ["-S", t.env["TMUX"].split(",")[0]] if t.env.get("TMUX") else []
        return [["tmux", *sock, "send-keys", "-t", t.pane, "-l", text],
                ["tmux", *sock, "send-keys", "-t", t.pane, "Enter"]]


class Zellij(Adapter):
    name, detect_var, env_vars = "zellij", "ZELLIJ_PANE_ID", ("ZELLIJ_SESSION_NAME",)

    def commands(self, t, text):
        s = ["--session", t.env["ZELLIJ_SESSION_NAME"]] if t.env.get("ZELLIJ_SESSION_NAME") else []
        return [["zellij", *s, "action", "write-chars", "--pane-id", t.pane, text],
                ["zellij", *s, "action", "send-keys", "--pane-id", t.pane, "Enter"]]


class Herdr(Adapter):
    name, detect_var = "herdr", "HERDR_PANE_ID"
    env_vars = ("HERDR_SOCKET_PATH", "HERDR_SESSION", "HERDR_HOME")

    def commands(self, t, text):
        return [["herdr", "pane", "send-text", t.pane, text],
                ["herdr", "pane", "send-keys", t.pane, "enter"]]


class Cmux(Adapter):
    name, detect_var, env_vars = "cmux", "CMUX_SURFACE_ID", ("CMUX_SOCKET_PATH",)
    tested = False  # from cmux.com/docs/api; not run against a real cmux yet

    def commands(self, t, text):
        return [["cmux", "send", "--surface", t.pane, text],
                ["cmux", "send-key", "--surface", t.pane, "enter"]]


class Rex(Adapter):
    name, detect_var, env_vars = "rex", "REX_BLOCK", ("REX_SESSION", "REX_SERVER")

    def commands(self, t, text):
        s = ["-s", t.env["REX_SESSION"]] if t.env.get("REX_SESSION") else []
        return [["rex", "send", *s, "-b", t.pane, text],
                ["rex", "send-key", *s, "-b", t.pane, "enter"]]


# Innermost first: tmux inside Rex inherits REX_BLOCK, but the pane is tmux's.
ADAPTERS: dict[str, Adapter] = {a.name: a for a in (Tmux(), Zellij(), Herdr(), Cmux(), Rex())}


def detect(env=os.environ) -> Adapter | None:
    forced = env.get("LEAN_VOICE_MUX")
    if forced:
        return ADAPTERS.get(forced)
    return next((a for a in ADAPTERS.values() if a.detect(env)), None)


def register(env=os.environ) -> Target:
    adapter = detect(env)
    if adapter is None:
        raise RuntimeError("no supported multiplexer found (tmux, zellij, herdr, cmux, rex); "
                           "set LEAN_VOICE_MUX to force one")
    t = adapter.target(env)
    t.cwd = os.getcwd()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(t.__dict__, indent=2) + "\n")
    return t


def load() -> Target | None:
    try:
        return Target(**json.loads(TARGET.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def deliver(text: str) -> None:
    t = load()
    if t is None:
        raise RuntimeError("no Claude pane registered; start Claude with lv-claude")
    ADAPTERS[t.mux].send(t, text)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "detect"
    if cmd == "register":
        try:
            t = register()
        except RuntimeError as exc:
            print(f"lean-voice: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"lean-voice: voice input will go to {t.mux} pane {t.pane}", file=sys.stderr)
    elif cmd == "detect":
        a = detect()
        print(a.name if a else "none")
    elif cmd == "target":
        t = load()
        print(json.dumps(t.__dict__) if t else "none")
    elif cmd == "send":
        deliver(" ".join(sys.argv[2:]))


if __name__ == "__main__":
    main()
