"""Cienka warstwa WinAPI (ctypes) dla AI Status Matrix.

Tylko biblioteka standardowa: moduł importuje zarówno widget, jak i hook.py
(hook potrzebuje `find_agent_process` i `process_created`).
"""
import ctypes
import os
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.windll.shell32

kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetShellWindow.restype = wintypes.HWND
user32.GetDesktopWindow.restype = wintypes.HWND
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]

HWND_TOPMOST, HWND_NOTOPMOST, HWND_BOTTOM = -1, -2, 1
_SWP = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

AGENT_EXES = {"claude.exe", "node.exe", "codex.exe", "gemini.exe", "bun.exe", "deno.exe"}
SHELL_EXES = {"cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe", "wsl.exe", "conhost.exe",
              "openconsole.exe", "python.exe", "pythonw.exe", "volta.exe", "npx.exe", "uv.exe"}


# ---------------------------------------------------------------- processes
class _PE(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def process_table():
    """{pid: (parent_pid, exe_lower)} dla wszystkich procesów."""
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    table = {}
    if not snap or snap == wintypes.HANDLE(-1).value:
        return table
    try:
        e = _PE()
        e.dwSize = ctypes.sizeof(_PE)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            table[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return table


def ancestors(pid, table=None, limit=16):
    table = table or process_table()
    out, seen = [], set()
    while pid in table and pid not in seen and len(out) < limit:
        seen.add(pid)
        out.append((pid, table[pid][1]))
        pid = table[pid][0]
    return out


def find_agent_process(start_pid=None):
    """PID procesu agenta (claude.exe / node.exe …) nad bieżącym hookiem."""
    chain = ancestors(start_pid or os.getpid())
    for pid, exe in chain[1:]:
        if exe in AGENT_EXES:
            return pid
    for pid, exe in chain[1:]:
        if exe not in SHELL_EXES:
            return pid
    return None


def process_created(pid):
    """Czas utworzenia procesu (FILETIME jako int) albo None. Chroni przed ponownym użyciem PID."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        kernel32.CloseHandle(h)


def process_alive(pid, created=None):
    """True/False, albo None gdy nie da się sprawdzić (brak uprawnień)."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False if ctypes.get_last_error() == 87 else None  # 87 = brak takiego procesu
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return None
        if code.value != STILL_ACTIVE:
            return False
    finally:
        kernel32.CloseHandle(h)
    if created is not None:
        now_created = process_created(pid)
        if now_created is not None and now_created != created:
            return False  # ten PID należy już do innego procesu
    return True


# ---------------------------------------------------------------- windows
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def top_windows_by_pid():
    """{pid: [hwnd, …]} widocznych okien najwyższego poziomu z tytułem."""
    out = {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.setdefault(pid.value, []).append(hwnd)
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return out


def terminal_window(agent_pid):
    """Okno terminala, w którym działa agent: pierwszy przodek z widocznym oknem."""
    if not agent_pid:
        return None
    windows = top_windows_by_pid()
    for pid, _exe in ancestors(agent_pid):
        if pid in windows:
            return windows[pid][0]
    return None


def focus_window(hwnd):
    """Przełącza na okno. Sztuczka z Alt omija blokadę SetForegroundWindow."""
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)       # Alt down
    user32.keybd_event(0x12, 0, 0x0002, 0)  # Alt up
    return bool(user32.SetForegroundWindow(hwnd))


def set_z(hwnd, where):
    user32.SetWindowPos(hwnd, where, 0, 0, 0, 0, _SWP)


# ---------------------------------------------------------------- nie przeszkadzać
class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def fullscreen_app_active():
    """True, gdy na pierwszym planie jest aplikacja pełnoekranowa, prezentacja albo gra."""
    state = ctypes.c_int(0)
    if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (2, 3, 4):
        return True  # BUSY (pełny ekran), RUNNING_D3D_FULL_SCREEN, PRESENTATION_MODE
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in (user32.GetShellWindow(), user32.GetDesktopWindow()):
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


# ---------------------------------------------------------------- hotkey
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312


def register_hotkey(hwnd, hotkey_id, mods, vk):
    return bool(user32.RegisterHotKey(hwnd, hotkey_id, mods | MOD_NOREPEAT, vk))


def unregister_hotkey(hwnd, hotkey_id):
    user32.UnregisterHotKey(hwnd, hotkey_id)
