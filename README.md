# lean-voice

Work on Lean proofs with Claude Code by talking to it. Press a key in Neovim, say what you want, and the agent reads the goal through Lean, does the work, and answers out loud. Speech recognition and synthesis both run locally on Apple Silicon.

<p align="center">
  <img src="docs/architecture.svg" alt="Speech goes from you and Neovim to the STT daemon, which types it into Claude Code with rex send. Claude reads goals from lean-lsp-mcp and sees your cursor through claudecode.nvim. Its replies go through Claude Code hooks to the TTS daemon and out your speakers." width="820">
</p>

## What a turn sounds like

You put the cursor on a `sorry`, press `<leader>vv` and say *"what's the goal here, would simp close it?"* A float shows the transcript as you speak. Enter sends it.

Claude says *"Let me check."*, calls `lean_goal` and `lean_multi_attempt` without narrating each step, and then says something like *"You need n plus one plus b to equal b plus n plus one, with the induction hypothesis for n. Simp makes no progress; omega closes it."* Pressing `<leader>vv` again while it talks cuts it off.

## Pieces

| | Runs | Does |
|---|---|---|
| `lua/lean_voice` | Neovim | push-to-talk float, `:LeanVoice` command, auto-saves `*.lean` on pause |
| `src/lean_voice/stt.py` | daemon | owns the mic, transcribes with [Parakeet](https://github.com/senstella/parakeet-mlx) on MLX, delivers text with `rex send` |
| `src/lean_voice/tts.py` | daemon | speaks with [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) on MLX, one sentence ahead of playback |
| `hooks/speak.py` | Claude Code hook | speaks new assistant text before each tool call and at the end of the turn |
| `claude/voice-prompt.md` | Claude Code | appended system prompt: short spoken replies, no code read aloud, goals in words |
| `bin/lv-claude` | your shell | starts the daemons, labels the pane, runs `claude --ide` with the hooks and prompt |

The hooks only load in sessions started with `lv-claude`. Your other Claude Code sessions stay silent.

## Requirements

- Apple Silicon Mac (MLX)
- [Rex](https://rex.superlogical.com) as the terminal
- Neovim 0.11+ with [lazy.nvim](https://github.com/folke/lazy.nvim) and [lean.nvim](https://github.com/Julian/lean.nvim)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [uv](https://docs.astral.sh/uv/), `fzf`
- Headphones. There's no echo cancellation yet, so speakers will feed the mic.

The first run downloads Parakeet (2.5 GB) and Kokoro (0.36 GB) from Hugging Face.

## Install

**1. The plugin.** Add a spec to your lazy.nvim config. `build` creates the Python environment inside the plugin directory.

```lua
{
  "tytrdev/lean-voice",
  ft = "lean",
  build = "uv sync",
  dependencies = {
    {
      "coder/claudecode.nvim", -- lets Claude see your file, cursor and selection
      dependencies = { "folke/snacks.nvim" },
      opts = { terminal = { provider = "none" } }, -- Claude runs in its own pane
    },
  },
  opts = {},
}
```

**2. Lean tools for Claude.**

```sh
claude mcp add --scope user lean-lsp -- uvx lean-lsp-mcp
```

**3. A launcher on your PATH.**

```sh
ln -s ~/.local/share/nvim/lazy/lean-voice/bin/lv-claude ~/.local/bin/lv-claude
```

## Use

In a Lean project, open Neovim in one Rex pane and run `lv-claude` from the same directory in another. Claude only connects to Neovim when its working directory is inside Neovim's.

| Key | Command | |
|---|---|---|
| `<leader>vv` | `:LeanVoice talk` | push to talk. `<CR>` sends, `<Esc>` cancels |
| `<leader>vo` | `:LeanVoice toggle` | listening off (mic never opens) or back on |
| `<leader>vc` | `:LeanVoice voice` | pick a voice. Moving the cursor plays a sample |
| | `:LeanVoice restart` | restart both daemons |

Messages sent by voice start with `[command]`. The prompt tells Claude to answer those with a short acknowledgement and then report back when it's done.

## Configure

Plugin options, shown with their defaults:

```lua
opts = {
  autosave = true, -- lean-lsp-mcp reads files from disk, so unsaved edits are invisible to it
  keys = { talk = "<leader>vv", toggle = "<leader>vo", voice = "<leader>vc" }, -- false disables one
}
```

Voice settings live in `~/.config/lean-voice/config.json` and are re-read for every reply, so changes apply immediately:

```json
{ "voice": "bf_lily", "speed": 1.1, "listen": "ptt" }
```

| Environment variable | Default | |
|---|---|---|
| `LEAN_VOICE_TARGET` | `claude-voice` | Rex pane label that receives transcripts |
| `LEAN_VOICE_TTS_MODEL` | `prince-canuma/Kokoro-82M` | |
| `LEAN_VOICE_STT_MODEL` | `mlx-community/parakeet-tdt-0.6b-v3` | |
| `LEAN_VOICE_HOOK_LOG` | unset | `1` logs every hook payload to `~/.cache/lean-voice/hooks.log` |

The daemons run as tabs in a Rex session called `lean-voice-daemons`, which the app doesn't show unless you open it. Their sockets are in `~/.cache/lean-voice/`, and both speak newline-delimited JSON, so another editor could drive them. The protocol is in the docstrings at the top of `stt.py` and `tts.py`.

## Notes on Claude Code hooks

These took some poking to find out:

- At `PreToolUse`, text the model wrote before the tool call is already in the transcript.
- At `Stop`, the final text block usually isn't in the transcript yet, but the payload carries it as `last_assistant_message`.
- The transcript JSONL holds one content block per line, each with its own `uuid`, which makes "what's new since last time" a set difference.

## Roadmap

- Conversation mode: always listening, with turn detection and interrupting by voice
- A "hey Claude" wake word ([openWakeWord](https://github.com/dscripka/openWakeWord)), behind the same off switch
- A visual window next to the terminal for goal states and ProofWidgets
- A typed scene DSL for mathematical exposition

## License

[MIT](LICENSE)
