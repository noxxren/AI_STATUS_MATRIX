"""Hook dla AI Status Matrix.

Wywoływany przez CLI agenta (Claude Code, Gemini CLI, Codex CLI) przy każdym zdarzeniu.
Czyta JSON zdarzenia (stdin albo ostatni argument), wylicza stan sesji i zapisuje go do
%LOCALAPPDATA%\\ai-status-matrix\\sessions\\<session_id>.json. Widget tylko czyta te pliki.

Zasady: tylko biblioteka standardowa, zero wypisywania na stdout, zawsze exit 0.
Hook nie może nigdy zablokować ani spowolnić agenta.

Użycie:
    python -S hook.py [--cli claude|gemini|codex] [NazwaZdarzenia] [JSON]
"""
import json
import os
import re
import sys
import time

STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ai-status-matrix", "sessions"
)

IDLE, WORKING, WAITING, ERROR, END = "idle", "working", "waiting", "error", "end"
COMPACTING = "compacting"  # kompaktowanie (streszczanie) kontekstu rozmowy
KEEP = "keep"  # zdarzenie informacyjne: zostaw poprzedni stan

# Nazwy zdarzeń: Claude Code, Gemini CLI (Before*/After*) i Codex CLI (notify: "type").
EVENT_STATE = {
    "SessionStart": IDLE,
    "UserPromptSubmit": WORKING,
    "PreToolUse": WORKING,
    "PostToolUse": WORKING,
    "PostToolUseFailure": WORKING,
    "PermissionRequest": WAITING,
    "Stop": IDLE,
    "PreCompact": COMPACTING,
    "SessionEnd": END,
    # Gemini CLI
    "BeforeAgent": WORKING,
    "BeforeModel": WORKING,
    "BeforeTool": WORKING,
    "AfterTool": WORKING,
    "AfterAgent": IDLE,
    "PreCompress": COMPACTING,
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
    # "Claude is waiting for your input" (po ~60 s) i inne informacje nie zmieniają stanu:
    # jeśli agent zadał pytanie, ma dalej świecić pomarańczowe.
    return KEEP


TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "BeforeTool", "AfterTool"}

# ---------------------------------------------------------------- zadania w tle
# Start: PostToolUse z "run_in_background" w wejściu i identyfikatorem zadania w odpowiedzi narzędzia.
# Koniec: Claude Code budzi agenta wiadomością <task-notification><task-id>…</task-id>…, która przychodzi
# jako UserPromptSubmit. Zatrzymanie ręczne: narzędzie TaskStop / KillShell.
BG_ID_KEYS = ("backgroundTaskId", "taskId", "task_id", "agentId", "shellId", "bash_id")
STOP_TOOLS = {"TaskStop", "KillShell", "KillBash"}
_TASK_ID = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")


def background_tasks(event, data, prev):
    """Lista zadań w tle sesji: [{"id", "label", "started"}]."""
    tasks = [t for t in prev.get("background") or [] if isinstance(t, dict) and t.get("id")]
    if data.get("agent_id"):
        return tasks  # zadania pomocniczych agentów nie są zadaniami tej sesji
    if event == "SessionStart" and data.get("source") in ("startup", "resume"):
        return []  # nowy proces Claude Code – zadania poprzedniego już nie żyją
    if event == "UserPromptSubmit":
        done = set(_TASK_ID.findall(str(data.get("prompt") or "")))
        return [t for t in tasks if t["id"] not in done]
    if event != "PostToolUse":
        return tasks
    inp, resp = data.get("tool_input"), data.get("tool_response")
    if not isinstance(inp, dict):
        return tasks
    if data.get("tool_name") in STOP_TOOLS:
        stop = str(inp.get("task_id") or inp.get("shell_id") or inp.get("bash_id") or inp.get("id") or "")
        return [t for t in tasks if t["id"] != stop]
    # komenda w tle ma "run_in_background" w wejściu; agent uruchamia się w tle domyślnie i mówi o tym
    # dopiero odpowiedź narzędzia ("isAsync": true, "status": "async_launched")
    is_async = isinstance(resp, dict) and (resp.get("isAsync") or resp.get("status") == "async_launched")
    if (inp.get("run_in_background") or is_async) and isinstance(resp, dict):
        tid = next((str(resp[k]) for k in BG_ID_KEYS if resp.get(k)), "")
        if tid and all(t["id"] != tid for t in tasks):
            label = inp.get("description") or inp.get("command") or inp.get("prompt") or data.get("tool_name") or ""
            tasks.append({"id": tid, "label": " ".join(str(label).split())[:60], "started": round(time.time(), 1)})
    return tasks


def resolve_state(event, data):
    # Narzędzia pomocniczych agentów (subagent, agent uruchomiony przez hook Stop) mają "agent_id".
    # Nie zmieniają koloru: po zakończeniu odpowiedzi z pytaniem ma zostać pomarańczowe.
    if event in TOOL_EVENTS and data.get("agent_id"):
        return KEEP
    if event == "Notification":
        return notification_state(data)
    if event in ("PreToolUse", "BeforeTool") and data.get("tool_name") in WAITING_TOOLS:
        return WAITING
    return EVENT_STATE.get(event)


# ---------------------------------------------------------------- pytanie na końcu odpowiedzi
def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def tail_entries(path, size=65536):
    """Wpisy z końca zapisu rozmowy (JSONL), od najnowszego. Pusta lista, gdy pliku nie da się przeczytać."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # pierwsza, ucięta linia albo wpis właśnie dopisywany
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _tail_final_text(path):
    """Tekst ostatniej odpowiedzi agenta z zapisu rozmowy.

    None = końcowa odpowiedź nie jest jeszcze zapisana (ostatni wpis to prompt, wynik narzędzia
    albo wywołanie narzędzia)."""
    for obj in tail_entries(path, 262144):
        kind = obj.get("type")
        if kind == "user":
            return None
        if kind != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_use" for c in content):
            return None
        text = _text_of(content).strip()
        if text:
            return text
    return ""


def api_error(data):
    """Tekst błędu API, jeśli tura skończyła się błędem (wpis z "isApiErrorMessage"), inaczej ""."""
    path = data.get("transcript_path")
    if not path:
        return ""
    for obj in tail_entries(path):
        kind = obj.get("type")
        if kind == "user":
            return ""
        if kind == "assistant":
            return error_text(obj)
    return ""


def error_text(entry):
    """Tekst błędu API z wpisu agenta oznaczonego "isApiErrorMessage", inaczej ""."""
    if not entry.get("isApiErrorMessage"):
        return ""
    return _text_of((entry.get("message") or {}).get("content")).strip() or "API Error"


def final_text(data):
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and msg.strip():
        return msg
    if isinstance(msg, dict):
        return _text_of(msg.get("content"))
    path = data.get("transcript_path")
    if not path:
        return ""
    for _ in range(10):  # zapis rozmowy bywa dopisywany chwilę po zdarzeniu Stop
        text = _tail_final_text(path)
        if text is not None:
            return text
        time.sleep(0.12)
    return ""


_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_URL = re.compile(r"https?://\S+")


def question_of(text):
    """Ostatnie pytanie z końcowego akapitu odpowiedzi albo "" (gdy agent o nic nie pyta)."""
    text = _URL.sub("", _CODE.sub("", text or "")).strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return ""
    last = paras[-1]
    sentences = re.findall(r"[^.!?\n]*\?", last)
    if not sentences:
        return ""
    q = re.sub(r"[*_#>]+", "", sentences[-1]).strip(" -•")
    return q[:120]


def debug_log(cli, event, data):
    """Diagnostyka: gdy istnieje plik ...\\ai-status-matrix\\debug, dopisz surowe zdarzenie do debug.log."""
    base = os.path.dirname(STATE_DIR)
    if not os.path.exists(os.path.join(base, "debug")):
        return
    slim = {}
    for k, v in data.items():
        dumped = json.dumps(v, ensure_ascii=False)
        slim[k] = v if len(dumped) < 200 else dumped[:200] + "…"
    line = json.dumps({"t": time.strftime("%H:%M:%S"), "cli": cli, "event": event, "ppid": os.getppid(), **slim},
                      ensure_ascii=False)
    with open(os.path.join(base, "debug.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


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

    debug_log(cli, event, data)
    state = resolve_state(event, data)
    if state is None:
        return
    error = api_error(data) if event == "Stop" else ""
    if error:
        state = ERROR  # tura przerwana błędem API (serwer, połączenie, limit)

    sid = str(data.get("session_id") or data.get("thread-id") or data.get("thread_id") or "")
    if not sid:
        sid = f"{cli}-{data.get('cwd') or os.getcwd()}"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:120]
    path = os.path.join(STATE_DIR, safe + ".json")

    if state == END:
        try:
            os.remove(path)
        except OSError:
            pass
        return

    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}

    if event == "SessionStart" and data.get("source") == "compact":
        # koniec kompaktowania: automatyczne dzieje się w trakcie pracy, ręczne (/compact) kończy się bezczynnością
        state = WORKING if prev.get("compact_trigger") == "auto" else IDLE

    keep = state == KEEP
    if keep:
        if not prev:
            return
        state = prev.get("state", IDLE)

    now = time.time()
    cwd = str(data.get("cwd") or prev.get("cwd") or os.getcwd())
    record = {
        "session_id": sid,
        "cli": cli,
        "cwd": cwd,
        "project": os.path.basename(cwd.rstrip("\\/")) or cwd,
        "state": state,
        "event": event,
        "detail": prev.get("detail", "") if keep else tool_detail(data) or str(data.get("message") or "")[:120],
        # pytanie, którym agent zakończył odpowiedź (widget pokazuje wtedy pomarańczowe)
        "question": prev.get("question", "") if keep else "",
        "ts": now,
        # kiedy sesja pojawiła się pierwszy raz (kolejność kafelków w układzie Matrix)
        "started": prev.get("started", now),
        # od kiedy trwa obecny stan (do liczników w podpowiedzi i przypomnień)
        "since": prev.get("since", now) if prev.get("state") == state else now,
        # początek pracy od ostatniego "bezczynny" (czekanie na zgodę też się liczy)
        "work_started": None if state == IDLE else prev.get("work_started") or now,
        "last_work_seconds": prev.get("last_work_seconds"),
        "transcript_path": data.get("transcript_path") or prev.get("transcript_path"),
        "pid": prev.get("pid"),
        "pid_created": prev.get("pid_created"),
        "background": background_tasks(event, data, prev),
        "compact_trigger": data.get("trigger") if state == COMPACTING else prev.get("compact_trigger"),
    }
    if state == COMPACTING:
        record["detail"] = "kompaktowanie kontekstu" + (" (auto)" if data.get("trigger") == "auto" else "")
    if state == IDLE and prev.get("work_started"):
        record["last_work_seconds"] = round(now - prev["work_started"], 1)
    if error:
        record["detail"] = error[:120]
    elif event in ("Stop", "AfterAgent"):
        record["question"] = question_of(final_text(data))
        if record["question"]:
            record["detail"] = record["question"]

    # PID agenta liczymy raz na sesję (albo gdy wcześniej się nie udało)
    if not record["pid"] or event == "SessionStart":
        try:
            import winapi
            pid = winapi.find_agent_process()
            if pid:
                record["pid"], record["pid_created"] = pid, winapi.process_created(pid)
        except Exception:
            pass
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    replace_retry(tmp, path)


def replace_retry(tmp, path):
    """os.replace z ponowieniami: na Windows nie da się podmienić pliku, który widget akurat czyta."""
    for _ in range(25):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02)
    try:
        os.remove(tmp)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
