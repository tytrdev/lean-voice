"""Local STT daemon: owns the mic, transcribes with Parakeet (MLX), delivers text to Claude.

Editor-neutral: any client (the Neovim plugin, a CLI, later a hotkey app) talks
to it over a Unix socket with newline-delimited JSON.

Listening modes (persisted in ~/.config/lean-voice/config.json as "listen"):
  off    mic never opens; push-to-talk requests are refused
  ptt    mic opens only while a push-to-talk recording is in progress (default)
  wake   always listening for a wake word (not implemented yet)

Requests:
  {"cmd": "record", "rex_session": "..."}   start a push-to-talk recording on this
      connection. The daemon streams {"partial": "..."} lines; the client then
      sends {"cmd": "send"} (deliver to Claude) or {"cmd": "cancel"} on the same
      connection, and gets {"final": "...", "delivered": bool}.
  {"cmd": "mode"}                 report the mode
  {"cmd": "mode", "set": "off"}   change it ("toggle" flips off <-> ptt)
"""

from __future__ import annotations

import json
import os
import re
import select
import socket
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from lean_voice import tts

SOCK = Path(os.environ.get("LEAN_VOICE_STT_SOCK", Path.home() / ".cache/lean-voice/stt.sock"))
MODEL = os.environ.get("LEAN_VOICE_STT_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
RATE = 16000
MODES = ("off", "ptt", "wake")

# Parakeet hears tactic names as English words; put them back in Lean spelling.
_LEAN_WORDS = ["omega", "simp", "rfl", "linarith", "nlinarith", "ring", "norm_num", "decide",
               "aesop", "exact", "apply", "intro", "intros", "rintro", "rw", "induction", "cases",
               "rcases", "obtain", "calc", "positivity", "gcongr", "constructor", "use", "refine"]
_FIXES = {"norm num": "norm_num", "r f l": "rfl", "are f l": "rfl", "simp?": "simp?"}


def lean_fix(text: str) -> str:
    for k, v in _FIXES.items():
        text = re.sub(rf"\b{re.escape(k)}\b", v, text, flags=re.I)
    for w in _LEAN_WORDS:
        text = re.sub(rf"\b{w}\b", w, text, flags=re.I)
    return text.strip()


def mode() -> str:
    m = tts.config().get("listen", "ptt")
    return m if m in MODES else "ptt"


class Transcriber:
    def __init__(self) -> None:
        import mlx.core as mx
        from parakeet_mlx import from_pretrained
        from parakeet_mlx.audio import get_logmel

        self.mx, self.get_logmel = mx, get_logmel
        self.model = from_pretrained(MODEL)
        self.lock = threading.Lock()  # one transcription at a time
        self(np.zeros(RATE // 2, dtype=np.float32))  # warm up / compile

    def __call__(self, audio: np.ndarray) -> str:
        if len(audio) < RATE // 4:
            return ""
        with self.lock:
            mel = self.get_logmel(self.mx.array(audio), self.model.preprocessor_config)
            return lean_fix(self.model.generate(mel)[0].text)


def deliver(text: str, rex_session: str | None) -> bool:
    """Type the text into the Claude pane and press Enter."""
    target = os.environ.get("LEAN_VOICE_TARGET", "claude-voice")
    scope = ["-s", rex_session] if rex_session else []
    try:
        subprocess.run(["rex", "send", *scope, "-b", target, f"[command] {text}"], check=True, timeout=5)
        subprocess.run(["rex", "send-key", *scope, "-b", target, "enter"], check=True, timeout=5)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"deliver failed: {exc}", flush=True)
        return False


def record(conn: socket.socket, msg: dict, stt: Transcriber) -> None:
    def emit(obj: dict) -> None:
        conn.sendall((json.dumps(obj) + "\n").encode())

    if mode() == "off":
        emit({"error": "listening is off"})
        return
    tts.send({"cmd": "stop"})  # barge-in: stop speaking when the user starts talking

    chunks: list[np.ndarray] = []
    stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                            callback=lambda data, *_: chunks.append(data[:, 0].copy()))
    stream.start()
    emit({"recording": True})
    buf = b""
    action = None
    last = ""
    try:
        while action is None:
            ready, _, _ = select.select([conn], [], [], 0.35)
            if ready:
                data = conn.recv(4096)
                if not data:
                    action = "cancel"
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = json.loads(line or b"{}").get("cmd")
                    if cmd in ("send", "cancel"):
                        action = cmd
            if action is None and chunks:
                text = stt(np.concatenate(chunks))
                if text != last:
                    last = text
                    emit({"partial": text})
    finally:
        time.sleep(0.15)  # catch the tail of the last word
        stream.stop()
        stream.close()

    if action == "cancel":
        emit({"final": "", "delivered": False, "cancelled": True})
        return
    final = stt(np.concatenate(chunks)) if chunks else ""
    delivered = bool(final) and deliver(final, msg.get("rex_session"))
    print(f"heard: {final!r} delivered={delivered}", flush=True)
    emit({"final": final, "delivered": delivered})


def handle(conn: socket.socket, stt: Transcriber, busy: threading.Lock) -> None:
    with conn:
        try:
            line = conn.makefile().readline()
            msg = json.loads(line or "{}")
        except (json.JSONDecodeError, OSError):
            return
        cmd = msg.get("cmd")
        if cmd == "record":
            if not busy.acquire(blocking=False):
                conn.sendall(b'{"error": "already recording"}\n')
                return
            try:
                record(conn, msg, stt)
            finally:
                busy.release()
        elif cmd == "mode":
            new = msg.get("set")
            if new == "toggle":
                new = "off" if mode() != "off" else "ptt"
            if new in MODES:
                tts.save_config(listen=new)
                print(f"mode: {new}", flush=True)
            conn.sendall((json.dumps({"mode": mode()}) + "\n").encode())


def serve() -> None:
    stt = Transcriber()
    busy = threading.Lock()
    SOCK.parent.mkdir(parents=True, exist_ok=True)
    SOCK.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK))
    srv.listen(64)
    print(f"lean-voice stt listening on {SOCK} (mode={mode()})", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn, stt, busy), daemon=True).start()


def main() -> None:
    import sys

    args = sys.argv[1:]
    cmd = args[0] if args else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "mode":
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(SOCK))
            req = {"cmd": "mode", **({"set": args[1]} if len(args) > 1 else {})}
            s.sendall((json.dumps(req) + "\n").encode())
            print(s.makefile().readline().strip())


if __name__ == "__main__":
    main()
