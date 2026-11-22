"""Claude Code hook: speak new assistant text via the lean-voice TTS daemon.

Events:
  UserPromptSubmit  stop any speech (barge-in by typing) and mark the transcript as read
  PreToolUse        speak assistant text written since the last hook
  Stop              speak the rest of the reply

Stdlib only, so it starts fast. Silently does nothing if the daemon isn't up.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import time
from pathlib import Path

CACHE = Path.home() / ".cache/lean-voice"
SOCK = Path(os.environ.get("LEAN_VOICE_TTS_SOCK", CACHE / "tts.sock"))
LOG = CACHE / "hooks.log"


def send(msg: dict) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(SOCK))
            s.sendall((json.dumps(msg) + "\n").encode())
            s.recv(256)
    except OSError:
        pass


def text_blocks(transcript: Path) -> list[tuple[str, str]]:
    """(uuid, text) for every assistant text block in the transcript, in order."""
    out = []
    try:
        lines = transcript.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        for block in e.get("message", {}).get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                out.append((e.get("uuid", ""), block["text"]))
    return out


def log(payload: dict) -> None:
    if os.environ.get("LEAN_VOICE_HOOK_LOG") != "1":
        return
    with LOG.open("a") as f:
        f.write(json.dumps({"t": time.time(), **payload}) + "\n")


def main() -> None:
    payload = json.load(sys.stdin)
    log(payload)
    event = payload.get("hook_event_name")
    transcript = Path(payload.get("transcript_path", ""))
    state_path = CACHE / "spoken" / f"{payload.get('session_id', 'unknown')}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with open(state_path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # parallel PreToolUse hooks must not double-speak
        fh.seek(0)
        try:
            spoken = set(json.loads(fh.read() or "[]"))
        except json.JSONDecodeError:
            spoken = set()

        if event == "UserPromptSubmit":
            send({"cmd": "stop"})
            spoken |= {u for u, _ in text_blocks(transcript)}
        else:
            blocks = text_blocks(transcript)
            new = [(u, t) for u, t in blocks if u not in spoken]
            if event == "Stop":
                # The final text block usually lands in the transcript just after Stop
                # fires, but the payload carries it as last_assistant_message.
                last = payload.get("last_assistant_message")
                if isinstance(last, str) and last.strip() and all(t.strip() != last.strip() for _, t in blocks):
                    new.append(("last:" + payload.get("prompt_id", str(time.time())), last))
            if new:
                send({"cmd": "say", "text": "\n".join(t for _, t in new)})
                spoken |= {u for u, _ in new}

        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(sorted(spoken)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never break the Claude session over speech
        print(f"lean-voice speak hook: {exc}", file=sys.stderr)
    sys.exit(0)
