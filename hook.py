"""Hook dla Sygnalizatora AI.

Wywoływany przez CLI agenta (Claude Code, Gemini CLI, Codex CLI) przy każdym zdarzeniu.
Czyta JSON zdarzenia (stdin albo ostatni argument), wylicza stan sesji i zapisuje go do
%LOCALAPPDATA%\\ai-traffic-light\\sessions\\<session_id>.json. Widget tylko czyta te pliki.

Zasady: tylko biblioteka standardowa, zero wypisywania na stdout, zawsze exit 0.
Hook nie może nigdy zablokować ani spowolnić agenta.

Użycie:
    python -S hook.py [--cli claude|gemini|codex] [NazwaZdarzenia] [JSON]
"""
import json
import os
import sys
import time

STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ai-traffic-light", "sessions"
)

IDLE, WORKING, WAITING, END = "idle", "working", "waiting", "end"

# Nazwy zdarzeń: Claude Code, Gemini CLI (Before*/After*) i Codex CLI (notify: "type").
EVENT_STATE = {
    "SessionStart": IDLE,
    "UserPromptSubmit": WORKING,
    "PreToolUse": WORKING,
    "PostToolUse": WORKING,
    "PostToolUseFailure": WORKING,
    "PermissionRequest": WAITING,
    "Stop": IDLE,
    "SessionEnd": END,
    # Gemini CLI
    "BeforeAgent": WORKING,
    "BeforeModel": WORKING,
    "BeforeTool": WORKING,
    "AfterTool": WORKING,
    "AfterAgent": IDLE,
    # Codex CLI (notify)
    "agent-turn-complete": IDLE,
}

WAITING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


def parse_args(argv):
    cli, event, raw = "claude", None, None
    rest = list(argv)
    while rest:
        a = rest.pop(0)
        if a == "--cli" and rest:
            cli = rest.pop(0)
        elif a.lstrip().startswith("{"):
            raw = a
        else:
            event = a
    return cli, event, raw


def read_payload(raw):
    if raw is None:
        try:
            if sys.stdin is not None and not sys.stdin.isatty():
                raw = sys.stdin.read()
        except Exception:
            raw = None
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def notification_state(data):
    kind = str(data.get("notification_type") or data.get("type") or "").lower()
    msg = str(data.get("message") or "").lower()
    if kind in ("permission_prompt", "elicitation_dialog") or "permission" in msg or "zgod" in msg:
        return WAITING
    if kind == "idle_prompt" or "waiting for your input" in msg:
        return IDLE
    return WAITING


def resolve_state(event, data):
    if event == "Notification":
        return notification_state(data)
    if event in ("PreToolUse", "BeforeTool") and data.get("tool_name") in WAITING_TOOLS:
        return WAITING
    return EVENT_STATE.get(event)


def tool_detail(data):
    name = data.get("tool_name")
    if not name:
        return ""
    inp = data.get("tool_input") or {}
    arg = ""
    if isinstance(inp, dict):
        arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") or ""
    arg = str(arg).replace("\n", " ")
    return f"{name}({arg[:60]})" if arg else str(name)


def main():
    cli, event, raw = parse_args(sys.argv[1:])
    data = read_payload(raw)
    event = event or data.get("hook_event_name") or data.get("type") or ""

    state = resolve_state(event, data)
    if state is None:
        return

    sid = str(data.get("session_id") or data.get("thread-id") or data.get("thread_id") or "")
    cwd = str(data.get("cwd") or os.getcwd())
    if not sid:
        sid = f"{cli}-{cwd}"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:120]
    path = os.path.join(STATE_DIR, safe + ".json")

    if state == END:
        try:
            os.remove(path)
        except OSError:
            pass
        return

    record = {
        "session_id": sid,
        "cli": cli,
        "cwd": cwd,
        "project": os.path.basename(cwd.rstrip("\\/")) or cwd,
        "state": state,
        "event": event,
        "detail": tool_detail(data) or str(data.get("message") or "")[:120],
        "ts": time.time(),
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
