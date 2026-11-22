-- lean-voice: the Neovim side of a voice loop with Claude Code.
--
-- The STT daemon (python -m lean_voice.stt) owns the mic, transcription and delivery
-- to Claude. This module draws the live-transcript float, sends "send"/"cancel",
-- and auto-saves Lean buffers so lean-lsp-mcp (which reads from disk) sees edits.

local M = {}

local root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":h:h:h")
local sock = vim.fn.expand("~/.cache/lean-voice/stt.sock")

local defaults = {
  autosave = true, -- save *.lean buffers when you pause, so Lean tools reading from disk stay current
  keys = {
    talk = "<leader>vv", -- push to talk
    toggle = "<leader>vo", -- listening on/off
    voice = "<leader>vc", -- choose the TTS voice
  },
}

M.config = vim.deepcopy(defaults)

local state = { pipe = nil, buf = nil, win = nil, text = "" }

-- ---------------------------------------------------------------- float

local function close_float()
  if state.win and vim.api.nvim_win_is_valid(state.win) then
    vim.api.nvim_win_close(state.win, true)
  end
  state.win, state.buf = nil, nil
end

local function render(title, text)
  if not (state.buf and vim.api.nvim_buf_is_valid(state.buf)) then return end
  local width = vim.api.nvim_win_get_width(state.win)
  local lines = {}
  for _, l in ipairs(vim.split(text ~= "" and text or "…", "\n")) do
    while #l > width - 2 do
      local cut = l:sub(1, width - 2):match(".*()%s") or (width - 2)
      table.insert(lines, l:sub(1, cut))
      l = l:sub(cut + 1)
    end
    table.insert(lines, l)
  end
  vim.bo[state.buf].modifiable = true
  vim.api.nvim_buf_set_lines(state.buf, 0, -1, false, lines)
  vim.bo[state.buf].modifiable = false
  vim.api.nvim_win_set_config(state.win, {
    title = " " .. title .. " ", title_pos = "left",
    footer = " <CR> send · <Esc> cancel ", footer_pos = "right",
    height = math.max(2, math.min(#lines, 8)),
  })
end

local function open_float()
  state.buf = vim.api.nvim_create_buf(false, true)
  local width = math.min(80, math.floor(vim.o.columns * 0.7))
  state.win = vim.api.nvim_open_win(state.buf, true, {
    relative = "editor", style = "minimal", border = "rounded",
    width = width, height = 2,
    row = vim.o.lines - 8, col = math.floor((vim.o.columns - width) / 2),
  })
  vim.wo[state.win].wrap = true
  local opts = { buffer = state.buf, nowait = true, silent = true }
  vim.keymap.set("n", "<CR>", function() M.finish("send") end, opts)
  vim.keymap.set("n", "<Esc>", function() M.finish("cancel") end, opts)
  vim.keymap.set("n", "q", function() M.finish("cancel") end, opts)
end

-- ---------------------------------------------------------------- daemon protocol

local function write(obj)
  if state.pipe and not state.pipe:is_closing() then
    state.pipe:write(vim.json.encode(obj) .. "\n")
  end
end

local function on_message(msg)
  if msg.error then
    vim.notify("lean-voice: " .. msg.error, vim.log.levels.WARN)
    close_float()
  elseif msg.recording then
    render("● listening", "")
  elseif msg.partial then
    state.text = msg.partial
    render("● listening", msg.partial)
  elseif msg.final ~= nil then
    if msg.cancelled then
      close_float()
    elseif msg.delivered then
      render("✓ sent to Claude", msg.final)
      vim.defer_fn(close_float, 700)
    else
      render(msg.final == "" and "nothing heard" or "✗ not delivered", msg.final)
      vim.defer_fn(close_float, 1500)
    end
    if state.pipe and not state.pipe:is_closing() then state.pipe:close() end
    state.pipe = nil
  end
end

local function not_running()
  vim.notify("lean-voice: daemons aren't running. Start them with :LeanVoice start (first time: :LeanVoice setup)", vim.log.levels.WARN)
end

--- Start a push-to-talk recording with a live transcript float.
function M.talk()
  if state.pipe then return end
  open_float()
  render("connecting", "")
  local pipe = vim.uv.new_pipe(false)
  state.pipe = pipe
  state.text = ""
  local pending = ""
  pipe:connect(sock, function(err)
    if err then
      vim.schedule(function()
        not_running()
        close_float()
        state.pipe = nil
      end)
      return
    end
    pipe:read_start(function(_, data)
      if not data then return end
      pending = pending .. data
      while true do
        local nl = pending:find("\n")
        if not nl then break end
        local line = pending:sub(1, nl - 1)
        pending = pending:sub(nl + 1)
        local ok, msg = pcall(vim.json.decode, line)
        if ok then vim.schedule(function() on_message(msg) end) end
      end
    end)
    write({ cmd = "record" })
  end)
end

--- Finish the current recording: "send" delivers it to Claude, "cancel" drops it.
function M.finish(action)
  if not state.pipe then return close_float() end
  render(action == "send" and "sending…" or "cancelling", state.text)
  write({ cmd = action })
end

local function request(obj, cb)
  local pipe = vim.uv.new_pipe(false)
  pipe:connect(sock, function(err)
    if err then
      vim.schedule(not_running)
      return pipe:close()
    end
    pipe:read_start(function(_, data)
      if data then
        local ok, msg = pcall(vim.json.decode, vim.trim(data))
        if ok then vim.schedule(function() cb(msg) end) end
      end
      pipe:close()
    end)
    pipe:write(vim.json.encode(obj) .. "\n")
  end)
end

--- Turn listening off (mic never opens) or back on (push-to-talk).
function M.toggle_listening()
  request({ cmd = "mode", set = "toggle" }, function(msg)
    vim.notify("lean-voice listening: " .. msg.mode)
  end)
end

-- ---------------------------------------------------------------- terminal helpers

local function float_term(cmd)
  local buf = vim.api.nvim_create_buf(false, true)
  local w, h = math.min(90, vim.o.columns - 8), math.min(24, vim.o.lines - 6)
  vim.api.nvim_open_win(buf, true, {
    relative = "editor", style = "minimal", border = "rounded", title = " lean-voice ",
    width = w, height = h, row = math.floor((vim.o.lines - h) / 2), col = math.floor((vim.o.columns - w) / 2),
  })
  vim.fn.jobstart(cmd, {
    term = true,
    on_exit = function()
      vim.schedule(function()
        if vim.api.nvim_buf_is_valid(buf) then vim.api.nvim_buf_delete(buf, { force = true }) end
      end)
    end,
  })
  vim.cmd.startinsert()
end

--- Pick the TTS voice (fzf list; moving the cursor plays a sample).
function M.choose_voice()
  float_term({ root .. "/bin/lv-voice" })
end

local function daemons(args, done_msg)
  vim.system(vim.list_extend({ root .. "/bin/lv-daemons" }, args), { text = true }, function(res)
    vim.schedule(function()
      if res.code == 0 then
        if done_msg then vim.notify("lean-voice: " .. done_msg) end
      else
        vim.notify(vim.trim(res.stderr or ""), vim.log.levels.ERROR)
      end
    end)
  end)
end

--- Start the daemons (no-op for ones already running).
function M.start() daemons({ "start" }, "daemons running") end
--- Stop the daemons.
function M.stop() daemons({ "stop" }, "daemons stopped") end
--- Restart the daemons.
function M.restart() daemons({ "restart" }, "daemons restarted") end

--- First run: download the models (~2.9 GB) in a floating terminal.
function M.setup_models() float_term({ root .. "/bin/lv-daemons", "setup" }) end

--- Show daemon and model status.
function M.status()
  local res = vim.system({ root .. "/bin/lv-daemons", "status" }, { text = true }):wait()
  vim.notify(vim.trim((res.stdout or "") .. (res.stderr or "")))
end

--- Open the daemon logs.
function M.logs()
  local dir = vim.fn.expand("~/.cache/lean-voice/")
  vim.cmd.tabnew(dir .. "stt.log")
  vim.cmd.vsplit(dir .. "tts.log")
  for _, w in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    vim.api.nvim_win_call(w, function() vim.cmd("normal! G") end)
  end
end

-- ---------------------------------------------------------------- setup

local function setup_autosave()
  vim.api.nvim_create_autocmd({ "InsertLeave", "TextChanged", "CursorHold", "CursorHoldI" }, {
    group = vim.api.nvim_create_augroup("lean_voice_autosave", { clear = true }),
    pattern = "*.lean",
    callback = function(ev)
      if vim.bo[ev.buf].modified and vim.bo[ev.buf].buftype == "" then
        vim.api.nvim_buf_call(ev.buf, function() vim.cmd("silent! noautocmd update") end)
      end
    end,
  })
end

local subcommands = {
  talk = M.talk,
  toggle = M.toggle_listening,
  voice = M.choose_voice,
  setup = M.setup_models,
  start = M.start,
  stop = M.stop,
  restart = M.restart,
  status = M.status,
  logs = M.logs,
}

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", defaults, opts or {})
  local k = M.config.keys
  if k.talk then vim.keymap.set("n", k.talk, M.talk, { desc = "Voice: push to talk" }) end
  if k.toggle then vim.keymap.set("n", k.toggle, M.toggle_listening, { desc = "Voice: listening on/off" }) end
  if k.voice then vim.keymap.set("n", k.voice, M.choose_voice, { desc = "Voice: choose voice" }) end
  if M.config.autosave then setup_autosave() end

  vim.api.nvim_create_user_command("LeanVoice", function(cmd)
    local f = subcommands[cmd.args]
    if f then f() else vim.notify("LeanVoice: unknown subcommand " .. cmd.args, vim.log.levels.ERROR) end
  end, {
    nargs = 1,
    complete = function() return vim.tbl_keys(subcommands) end,
    desc = "lean-voice: talk | toggle | voice | setup | start | stop | restart | status | logs",
  })
end

return M
