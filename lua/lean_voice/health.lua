-- :checkhealth lean_voice
local M = {}

local h = vim.health

local function run(args)
  local ok, res = pcall(function() return vim.system(args, { text = true }):wait(15000) end)
  if not ok or res.code ~= 0 then return nil, ok and (res.stderr or "") or tostring(res) end
  return vim.trim(res.stdout or "")
end

function M.check()
  local root = require("lean_voice").root
  local py = root .. "/.venv/bin/python"

  h.start("lean-voice: platform")
  if vim.fn.has("nvim-0.11") == 1 then h.ok("Neovim " .. tostring(vim.version())) else h.error("Neovim 0.11+ required") end
  local u = vim.uv.os_uname()
  if u.sysname == "Darwin" and u.machine == "arm64" then
    h.ok("Apple Silicon macOS")
  else
    h.error("MLX needs Apple Silicon macOS; found " .. u.sysname .. " " .. u.machine)
  end

  h.start("lean-voice: tools")
  for _, t in ipairs({
    { "uv", "builds the Python environment" },
    { "claude", "Claude Code CLI" },
    { "fzf", "voice picker (:LeanVoice voice)" },
  }) do
    if vim.fn.executable(t[1]) == 1 then h.ok(t[1]) else h.error(t[1] .. " not found: " .. t[2]) end
  end
  if vim.fn.executable(py) == 1 then
    h.ok("Python environment: " .. root .. "/.venv")
  else
    h.error("Python environment missing", { "Run :Lazy build lean-voice (runs `uv sync` in the plugin directory)" })
    return
  end

  h.start("lean-voice: Claude Code")
  local claude_json = vim.fn.expand("~/.claude.json")
  local ok_read, lines = pcall(vim.fn.readfile, claude_json)
  if ok_read and table.concat(lines, "\n"):find("lean-lsp-mcp", 1, true) then
    h.ok("lean-lsp-mcp is registered")
  else
    h.warn("lean-lsp-mcp not found in ~/.claude.json", { "claude mcp add --scope user lean-lsp -- uvx lean-lsp-mcp" })
  end
  if pcall(require, "claudecode") then
    h.ok("claudecode.nvim is installed (Claude sees your cursor and selection)")
  else
    h.warn("claudecode.nvim not installed; Claude won't see your cursor or selection")
  end

  h.start("lean-voice: terminal multiplexer")
  local mux = run({ py, "-m", "lean_voice.mux", "detect" })
  if mux and mux ~= "none" then
    h.ok("Neovim is running in " .. mux)
    if vim.fn.executable(mux) == 0 then h.error(mux .. " CLI not on PATH") end
    if mux == "cmux" then h.warn("the cmux adapter hasn't been tested against a real cmux yet") end
  else
    h.warn("no supported multiplexer detected (tmux, zellij, herdr, cmux, rex)",
      { "Voice input is typed into Claude's pane through the multiplexer.", "Set LEAN_VOICE_MUX to force one." })
  end
  local target = run({ py, "-m", "lean_voice.mux", "target" })
  if target and target ~= "none" then
    local t = vim.json.decode(target)
    h.ok(("voice input goes to %s pane %s (%s)"):format(t.mux, t.pane, t.cwd))
  else
    h.info("no Claude pane registered yet; it registers itself when you run lv-claude")
  end

  h.start("lean-voice: models and daemons")
  local out, err = run({ py, "-m", "lean_voice.daemons", "status", "--json" })
  if not out then return h.error("status check failed: " .. (err or "")) end
  local st = vim.json.decode(out)
  for _, name in ipairs({ "stt", "tts" }) do
    local s = st[name]
    if not s.model_cached then
      h.error(name .. " model not downloaded: " .. s.model, { "Run :LeanVoice setup (about 2.9 GB, first time only)" })
    elseif s.running then
      h.ok(name .. " daemon running (pid " .. tostring(s.pid) .. ")")
    else
      h.info(name .. " daemon stopped; lv-claude starts it, or :LeanVoice start. Log: " .. s.log)
    end
  end
  local cfg_ok, cfg = pcall(function()
    return vim.json.decode(table.concat(vim.fn.readfile(vim.fn.expand("~/.config/lean-voice/config.json")), "\n"))
  end)
  local listen = (cfg_ok and cfg.listen) or "ptt"
  h.info("listening mode: " .. listen .. (listen == "off" and " (mic never opens; <leader>vo turns it back on)" or ""))
end

return M
