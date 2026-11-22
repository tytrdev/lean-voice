"""Local TTS daemon: keeps Kokoro (MLX) warm and speaks text sent over a Unix socket.

Protocol: one JSON object per connection line.
  {"cmd": "say", "text": "..."}        queue text (cleaned, split into sentences)
  {"cmd": "preview", "voice": "..."}   stop, then speak a sample in that voice
  {"cmd": "stop"}                      drop everything queued and cut playback now
  {"cmd": "ping"}                      replies {"ok": true, "speaking": bool, ...}

Voice and speed come from ~/.config/lean-voice/config.json, re-read on every
"say", so changing the voice takes effect without a restart.

Playback uses one persistent output stream with a generous buffer. Opening a
stream per sentence, with a small buffer, crackled whenever synthesis of the
next sentence starved the audio callback.
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd

SOCK = Path(os.environ.get("LEAN_VOICE_TTS_SOCK", Path.home() / ".cache/lean-voice/tts.sock"))
CONFIG = Path.home() / ".config/lean-voice/config.json"
MODEL = os.environ.get("LEAN_VOICE_TTS_MODEL", "prince-canuma/Kokoro-82M")
DEFAULTS = {"voice": "af_heart", "speed": 1.1}
RATE = 24000
FADE = int(0.008 * RATE)        # 8 ms fade at sentence edges: no clicks
GAP = np.zeros(int(0.12 * RATE), dtype=np.float32)  # pause between sentences
PREVIEW = "Hi, I'm {name}. In the successor case, you need n plus one plus b to equal b plus n plus one."


def config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text())}
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save_config(**changes) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({**config(), **changes}, indent=2) + "\n")


# ---------------------------------------------------------------- text cleanup

_SYMBOLS = {
    "⊢": " we need ", "→": " implies ", "↔": " iff ", "∀": " for all ", "∃": " there exists ",
    "¬": " not ", "∧": " and ", "∨": " or ", "≤": " at most ", "≥": " at least ", "≠": " not equal to ",
    "∈": " in ", "ℕ": " N ", "ℤ": " Z ", "ℝ": " R ", "λ": " lambda ", "=": " equals ",
}


def clean(text: str) -> str:
    """Make markdown-ish assistant text speakable: no code, no markup."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)          # fenced code: skip entirely
    text = re.sub(r"```.*", " ", text, flags=re.S)               # unterminated fence
    text = re.sub(r"`([^`]{1,40})`", r"\1", text)                # short inline code: say it
    text = re.sub(r"`[^`]*`", " ", text)                         # long inline code: skip
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)         # links -> link text
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)    # headers
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)         # bullets
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)      # tables
    text = re.sub(r"[*_~]{1,3}", "", text)                       # emphasis
    text = re.sub(r"[\w./-]+\.(?:lean|py|lua|json|md)(?::\d+)?", lambda m: Path(m.group().split(":")[0]).stem, text)
    for sym, word in _SYMBOLS.items():
        text = text.replace(sym, word)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?:;])\s+|\n+", text)
    return [p.strip() for p in parts if re.search(r"\w", p)]


# ---------------------------------------------------------------- playback

class Player:
    """One persistent output stream fed from a sample buffer."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.chunks: list[np.ndarray] = []
        self.pos = 0  # read offset into chunks[0]
        self.underflows = 0
        self.device = None
        self.stream = None
        self._open()

    def _open(self) -> None:
        """(Re)open the stream on the current default output device.

        PortAudio caches the device list and a stream stays on the device it was
        opened on, so re-initialize to follow headphones being plugged in or
        AirPods connecting.
        """
        self.device = sd.query_devices(kind="output")["name"]
        self.stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="float32",
                                      latency=0.25, callback=self._callback)
        self.stream.start()
        print(f"output device: {self.device}", flush=True)

    def follow_default_device(self) -> None:
        """Called before a new utterance starts (player idle)."""
        if self.busy:
            return
        self.stream.close()
        sd._terminate()   # refresh PortAudio's cached device list
        sd._initialize()
        self._open()

    def _callback(self, out, frames, _time, status) -> None:
        if status.output_underflow:
            self.underflows += 1
        out.fill(0)
        n = 0
        with self.lock:
            while n < frames and self.chunks:
                c = self.chunks[0]
                take = min(frames - n, len(c) - self.pos)
                out[n:n + take, 0] = c[self.pos:self.pos + take]
                n += take
                self.pos += take
                if self.pos >= len(c):
                    self.chunks.pop(0)
                    self.pos = 0

    def push(self, audio: np.ndarray) -> None:
        with self.lock:
            self.chunks.append(audio)

    def clear(self) -> None:
        with self.lock:
            self.chunks.clear()
            self.pos = 0

    @property
    def busy(self) -> bool:
        return bool(self.chunks)


def shape(audio: np.ndarray) -> np.ndarray:
    audio = np.clip(audio.astype(np.float32), -1.0, 1.0)
    if len(audio) > 2 * FADE:
        ramp = np.linspace(0.0, 1.0, FADE, dtype=np.float32)
        audio[:FADE] *= ramp
        audio[-FADE:] *= ramp[::-1]
    return np.concatenate([audio, GAP])


# ---------------------------------------------------------------- engine

class Speaker:
    def __init__(self) -> None:
        from mlx_audio.tts.utils import load_model

        self.model = load_model(MODEL)
        self.player = Player()
        self.text_q: queue.Queue[tuple[int, str, str, float]] = queue.Queue()
        self.gen = 0  # bumped on stop; stale items are dropped
        self._synth("Ready.", DEFAULTS["voice"], 1.0)  # warm up, not played
        threading.Thread(target=self._synth_loop, daemon=True).start()

    def say(self, text: str, voice: str | None = None) -> None:
        cfg = config()
        voice = voice or cfg["voice"]
        if not self.speaking:
            self.player.follow_default_device()
        for s in sentences(clean(text)):
            self.text_q.put((self.gen, s, voice, float(cfg["speed"])))

    def stop(self) -> None:
        self.gen += 1
        while True:
            try:
                self.text_q.get_nowait()
            except queue.Empty:
                break
        self.player.clear()

    @property
    def speaking(self) -> bool:
        return self.player.busy or not self.text_q.empty()

    def _synth(self, text: str, voice: str, speed: float) -> np.ndarray:
        chunks = [np.asarray(c.audio, dtype=np.float32) for c in
                  self.model.generate(text=text, voice=voice, speed=speed, lang_code=voice[0])]
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def _synth_loop(self) -> None:
        while True:
            gen, s, voice, speed = self.text_q.get()
            if gen != self.gen:
                continue
            print(f"say[{voice}]: {s}", flush=True)
            try:
                audio = self._synth(s, voice, speed)
            except Exception as exc:  # bad voice name etc.: skip the sentence
                print(f"synth error: {exc}", flush=True)
                continue
            if len(audio) and gen == self.gen:
                self.player.push(shape(audio))


# ---------------------------------------------------------------- server

def serve() -> None:
    speaker = Speaker()
    SOCK.parent.mkdir(parents=True, exist_ok=True)
    SOCK.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK))
    srv.listen(64)
    print(f"lean-voice tts listening on {SOCK} (voice={config()['voice']})", flush=True)
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                handle(conn, speaker)
            except (OSError, ValueError) as exc:  # client hung up, bad JSON: never kill the daemon
                print(f"request error: {exc!r}", flush=True)


def handle(conn: socket.socket, speaker: Speaker) -> None:
    msg = json.loads(conn.makefile().readline() or "{}")
    cmd = msg.get("cmd")
    if cmd == "say":
        speaker.say(msg.get("text", ""))
    elif cmd == "preview":
        voice = msg.get("voice") or config()["voice"]
        speaker.stop()
        speaker.say(PREVIEW.format(name=voice.split("_", 1)[-1].title()), voice=voice)
    elif cmd == "stop":
        speaker.stop()
    reply = {"ok": True, "speaking": speaker.speaking, "underflows": speaker.player.underflows}
    conn.sendall((json.dumps(reply) + "\n").encode())


def send(msg: dict) -> dict | None:
    """Client helper. Returns None if the daemon isn't running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect(str(SOCK))
            s.sendall((json.dumps(msg) + "\n").encode())
            return json.loads(s.makefile().readline() or "null")
    except OSError:
        return None


ENGLISH_VOICES = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]


def main() -> None:
    import sys

    args = sys.argv[1:]
    cmd = args[0] if args else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "say":
        print(send({"cmd": "say", "text": " ".join(args[1:])}))
    elif cmd == "preview":
        print(send({"cmd": "preview", "voice": args[1] if len(args) > 1 else None}))
    elif cmd == "voices":
        current = config()["voice"]
        for v in ENGLISH_VOICES:
            print(f"{v}{'  (current)' if v == current else ''}")
    elif cmd == "set":  # set voice NAME | set speed 1.2
        key, val = args[1], args[2]
        save_config(**{key: float(val) if key == "speed" else val})
        print(config())
    elif cmd == "stop":
        print(send({"cmd": "stop"}))
    else:
        print(send({"cmd": "ping"}))


if __name__ == "__main__":
    main()
