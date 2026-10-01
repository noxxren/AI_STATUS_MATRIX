# AI_STATUS_WIDGET

**English** | [Polski](README.pl.md)

A minimal Windows desktop widget that shows what your AI agents are doing in their terminals.
Each session (terminal) gets its own square Matrix-style screen; the grid grows like text in a book:
1, 2×1, 2×2, 3×2… – a new terminal takes the next free slot.

| Screen | Meaning |
|---|---|
| white-turquoise `>_` cursor | agent is idle |
| green falling characters | agent is working |
| orange, pulsing `?` | agent is waiting for your reply |
| blue KITT-style scanner (“Knight Rider”) | agent is compacting the conversation context (`/compact` or automatic) |
| magenta `ERR` glitch | the turn ended with an API error, or the session is “working” with no events (stalled) |
| grey noise | no sessions at all – the agent is not running or the hooks are not installed |

Clicking a tile switches to that session's terminal; hovering shows its details.
Near a screen edge the widget grows towards the center.

The interface is available in English and Polish (Settings → System → Language; by default it follows Windows).

## How it works

```
Claude Code ──hook──▶ hook.py ──write──▶ %LOCALAPPDATA%\ai-traffic-light\sessions\<session_id>.json
                                                               │
                                   widget.pyw ◀──read every 300 ms
```

- `hook.py` – standard library only, always exits with code 0 and prints nothing, so it never blocks the agent.
- `widget.pyw` – PySide6 (venv in `.venv`). It sets the window layer with `SetWindowPos` and `SWP_NOACTIVATE`,
  so popping up never steals focus from your terminal.
- `install_hooks.py` – adds/removes our hooks in `~/.claude/settings.json` (with a backup).

## Installation

Requirements: Windows 10/11, Python 3.10+ (`python` on PATH).

```bat
git clone https://github.com/noxxren/AI_STATUS_MATRIX.git
cd AI_STATUS_MATRIX
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Running

1. Hooks: `python install_hooks.py` (or in the widget: right-click → Settings… → Claude Code hooks → Install).
   Removal: `python install_hooks.py --uninstall`. Preview without writing: `--dry-run`.
2. Widget: `start.bat`.
3. Hooks take effect in **newly started** Claude Code sessions.

Right-click the widget: go to the waiting agent, session list (click to switch to its terminal),
layer mode, **Do not disturb**, **Settings…**, Quit.
Left button: drag. Clicking a tile switches to that session's terminal; clicking next to the tiles switches
to the agent waiting longest. In “below windows” mode a click also sends the widget back under the windows.
The tray icon has the same menu; clicking it brings the widget to the front.

## What else the widget does

- **Jump to terminal** – the hook stores the PID of the agent process (`claude.exe`), and the widget walks up
  the process tree to the terminal window (Warp, Windows Terminal, VS Code…). Optional global shortcut (off by
  default, set it in Settings → Behavior → Agent shortcut, e.g. `Ctrl+Alt+L`).
  In tabbed terminals it switches to the window, not to the specific tab.
- **Interrupt (Esc)** – every ~2 s the widget checks the end of the transcript (`transcript_path`); a
  “Request interrupted by user” entry immediately switches the session to idle.
- **Turn error** – an `isApiErrorMessage` entry at the end of the transcript (e.g. “API Error: 500”, dropped
  connection) switches the session to error. The hook checks this on `Stop`, and the widget every ~2 s, because
  `Stop` does not always arrive after an error. The next prompt sent to the agent clears the error.
- **Closed terminal** – a session whose agent process is gone disappears right away (PID + process creation time).
- **Timers** – in the tooltip and menu: how long a session has been working / waiting / idle.
- **Sounds** – when someone is waiting, when the agent finishes a task longer than a threshold (3 min by default),
  and when a session has been idle for N minutes (10 by default). The widget deliberately shows no Windows
  balloons or notifications.
- **Do not disturb** – manually from the menu, automatically for full-screen apps or during set hours.
  The screens still change, but without popping up or sounds.

## Event mapping

Tools of helper agents (subagents, an agent started by a `Stop` hook) carry `agent_id` in their data
and do not change the state. Diagnostics: create an empty file `%LOCALAPPDATA%\ai-traffic-light\debug`,
and the hook will append raw events to `debug.log` in the same folder.

| Event | State |
|---|---|
| `SessionStart`, `Stop` | idle |
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | working |
| `Notification` (`permission_prompt`), `PreToolUse` for `AskUserQuestion` / `ExitPlanMode` | waiting |
| `Stop`, when the last paragraph of the reply contains a question (“Shall I do it?”) | waiting (option in Settings → Behavior) |
| `Stop` (or its absence), when the turn ended with an API error | error |
| `PreCompact` | compacting; the following `SessionStart` (`source: compact`) returns to working (auto) or idle (`/compact`) |
| `Notification` (`idle_prompt` and other informational ones) | no change |
| `SessionEnd` | session disappears |

## Other CLIs

`hook.py` also understands Gemini CLI events (`BeforeAgent`, `BeforeTool`, `AfterAgent`…) and Codex CLI.
Their configuration is not installed automatically – examples:

- **Gemini CLI** (`~/.gemini/settings.json`, `hooks` section): command
  `python -S D:/tools/ai-traffic-light/hook.py --cli gemini` for `SessionStart`, `BeforeAgent`,
  `BeforeTool`, `AfterAgent`, `PreCompress`, `Notification`, `SessionEnd`.
- **Codex CLI** (`~/.codex/config.toml`):
  `notify = ["python", "-S", "D:/tools/ai-traffic-light/hook.py", "--cli", "codex"]`.
  Codex only reports the end of a turn, so it shows idle after the work is done, but not the work itself.

## Settings

Stored in `%LOCALAPPDATA%\ai-traffic-light\config.json`, split into tabs:

- **Behavior** – layer (always on top / below windows), when to pop up, how long to stay on top,
  time until a session counts as stalled, keyboard shortcut, click switches to terminal, Do not disturb.
- **Sounds** – sound when waiting, long task finished (threshold + sound), idle reminder
  (time + sound). Sounds from `C:\Windows\Media` or your own `.wav` file; the ▶ button plays the selected one.
- **Appearance** (live preview) – preset, tile housing (dark / light / glass / automatic based on background
  brightness), rim and glow in the state color (on by default), glow strength, background opacity, size.
- **System** – language (automatic from Windows / Polish / English), start with Windows,
  install/remove hooks, state folder.
