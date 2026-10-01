"""Dopisuje (albo usuwa) hooki Sygnalizatora AI w ~/.claude/settings.json.

    python install_hooks.py              # instalacja (z kopią zapasową settings.json)
    python install_hooks.py --uninstall  # usunięcie tylko naszych wpisów
    python install_hooks.py --dry-run    # pokaż wynik bez zapisu

Nasze wpisy rozpoznajemy po "ai-traffic-light" w komendzie, więc skrypt jest idempotentny
i nie rusza innych hooków.
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "hook.py")
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
MARK = "ai-traffic-light"

# (zdarzenie, matcher albo None)
EVENTS = [
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "*"),
    ("PostToolUse", "*"),
    ("Notification", None),
    ("Stop", None),
    ("PreCompact", None),
    ("SessionEnd", None),
]


def base_python():
    # Hook ma działać bez venv: tylko stdlib, więc wystarczy systemowy python.
    exe = getattr(sys, "_base_executable", None) or sys.executable
    return exe.replace("pythonw.exe", "python.exe")


def command():
    py = base_python().replace("\\", "/")
    hook = HOOK.replace("\\", "/")
    return f'"{py}" -S "{hook}" --cli claude'


def is_ours(group):
    return any(MARK in str(h.get("command", "")) for h in group.get("hooks", []))


def strip(settings):
    hooks = settings.get("hooks", {})
    for ev in list(hooks):
        groups = [g for g in hooks[ev] if not is_ours(g)]
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    if not hooks:
        settings.pop("hooks", None)


def install(settings):
    strip(settings)
    hooks = settings.setdefault("hooks", {})
    for ev, matcher in EVENTS:
        group = {"hooks": [{"type": "command", "command": command(), "timeout": 5}]}
        if matcher is not None:
            group = {"matcher": matcher, **group}
        hooks.setdefault(ev, []).append(group)


def main():
    uninstall = "--uninstall" in sys.argv
    dry = "--dry-run" in sys.argv

    settings = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)

    strip(settings) if uninstall else install(settings)
    out = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"

    if dry:
        print(out)
        return

    if os.path.exists(SETTINGS):
        backup = f"{SETTINGS}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(SETTINGS, backup)
        print(f"Kopia zapasowa: {backup}")
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        f.write(out)
    print(("Usunięto" if uninstall else "Zainstalowano") + f" hooki w {SETTINGS}")
    if not uninstall:
        print(f"Komenda: {command()}")
        print("Nowe hooki działają w nowo uruchomionych sesjach Claude Code.")


if __name__ == "__main__":
    main()
