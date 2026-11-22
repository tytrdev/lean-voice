"""Start, stop and check the TTS and STT daemons.

The daemons are plain background processes (their own session, so closing a
terminal or quitting Neovim doesn't kill them). State lives in ~/.cache/lean-voice:
  <name>.sock   Unix socket the daemon serves on
  <name>.pid    process id
  <name>.log    stdout and stderr

  python -m lean_voice.daemons start|stop|restart|status [--json]
  python -m lean_voice.daemons setup     download the models (first run, ~2.9 GB)
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

CACHE = Path.home() / ".cache/lean-voice"
NAMES = ("tts", "stt")
MODELS = {
    "tts": os.environ.get("LEAN_VOICE_TTS_MODEL", "prince-canuma/Kokoro-82M"),
    "stt": os.environ.get("LEAN_VOICE_STT_MODEL", "mlx-community/parakeet-tdt-0.6b-v3"),
}


def sock(name: str) -> Path:
    return CACHE / f"{name}.sock"


def alive(name: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(sock(name)))
        return True
    except OSError:
        return False


def pid(name: str) -> int | None:
    try:
        p = int((CACHE / f"{name}.pid").read_text())
        os.kill(p, 0)
        return p
    except (OSError, ValueError):
        return None


def model_cached(name: str) -> bool:
    """True if the model's config is in the Hugging Face cache (the loaders fetch only what they use)."""
    from huggingface_hub import try_to_load_from_cache

    return isinstance(try_to_load_from_cache(MODELS[name], "config.json"), str)


def status() -> dict:
    return {n: {"running": alive(n), "pid": pid(n), "model": MODELS[n], "model_cached": model_cached(n),
                "log": str(CACHE / f"{n}.log")} for n in NAMES}


def start(wait: float = 60.0) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    missing = [MODELS[n] for n in NAMES if not model_cached(n)]
    if missing:
        raise RuntimeError("models not downloaded yet: " + ", ".join(missing)
                           + ". Run :LeanVoice setup (or python -m lean_voice.daemons setup)")
    for n in NAMES:
        if alive(n):
            continue
        log = open(CACHE / f"{n}.log", "a")
        proc = subprocess.Popen([sys.executable, "-m", f"lean_voice.{n}", "serve"],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True,
                                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONWARNINGS": "ignore",
                                     "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"})
        (CACHE / f"{n}.pid").write_text(str(proc.pid))
    deadline = time.time() + wait
    while time.time() < deadline:
        if all(alive(n) for n in NAMES):
            return
        dead = [n for n in NAMES if not alive(n) and pid(n) is None]
        if dead:
            raise RuntimeError(f"{', '.join(dead)} daemon exited; see {CACHE}/{dead[0]}.log")
        time.sleep(0.25)
    raise RuntimeError(f"daemons didn't come up within {wait:.0f}s; see logs in {CACHE}")


def stop() -> None:
    for n in NAMES:
        p = pid(n)
        if p:
            os.kill(p, signal.SIGTERM)
    for _ in range(40):
        if not any(pid(n) for n in NAMES):
            break
        time.sleep(0.1)
    for n in NAMES:
        sock(n).unlink(missing_ok=True)
        (CACHE / f"{n}.pid").unlink(missing_ok=True)


def setup() -> None:
    """Download both models with visible progress by loading each once (first run, ~2.9 GB)."""
    print(f"==> {MODELS['tts']} (text to speech)", flush=True)
    from mlx_audio.tts.utils import load_model

    from lean_voice.tts import fix_espeak_path

    fix_espeak_path()
    list(load_model(MODELS["tts"]).generate(text="Ready.", voice="af_heart", lang_code="a"))
    print(f"\n==> {MODELS['stt']} (speech to text)", flush=True)
    from lean_voice.stt import Transcriber

    Transcriber()
    print("\nlean-voice is ready. Start Claude with lv-claude.", flush=True)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "start":
            start()
        elif cmd == "stop":
            stop()
        elif cmd == "restart":
            stop()
            start()
        elif cmd == "setup":
            setup()
        elif cmd == "status":
            st = status()
            if "--json" in sys.argv:
                print(json.dumps(st))
            else:
                for n, s in st.items():
                    state = "running" if s["running"] else "stopped"
                    model = "cached" if s["model_cached"] else "NOT DOWNLOADED"
                    print(f"{n}: {state:8} model {s['model']} ({model})  log {s['log']}")
        else:
            print(__doc__)
    except RuntimeError as exc:
        print(f"lean-voice: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
