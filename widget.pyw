"""AI Status Widget: minimalistyczny widget pokazujący stan agentów AI.

Zielone   - wszyscy agenci bezczynni
Czerwone  - przynajmniej jeden pracuje
Pomarańcz - ktoś czeka na Twoją odpowiedź (ma pierwszeństwo)

Stan sesji zapisuje hook.py do %LOCALAPPDATA%\\ai-traffic-light\\sessions\\*.json.
Widget czyta te pliki, dodatkowo wykrywa przerwania (Esc) i zamknięte sesje,
przełącza do terminala agenta i gra dźwięki (bez dymków Windows). Uruchamiaj przez pythonw.
"""
import json
import math
import os
import random
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QEvent, QLockFile, QPoint, QPointF, QRectF, Qt, QTime, QTimer
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QKeySequenceEdit, QLabel, QMenu, QPushButton, QSlider, QSpinBox, QSystemTrayIcon, QTabWidget, QTimeEdit,
    QToolButton, QToolTip, QVBoxLayout, QWidget,
)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import install_hooks  # noqa: E402
import winapi  # noqa: E402

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ai-traffic-light")
SESSIONS_DIR = os.path.join(APP_DIR, "sessions")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
MEDIA_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media")

IDLE, WORKING, WAITING, STALE, ERROR = "idle", "working", "waiting", "stale", "error"
COMPACTING = "compacting"  # kompaktowanie (streszczanie) kontekstu rozmowy
OFFLINE = "offline"  # tylko stan zbiorczy: brak jakiejkolwiek sesji
BUSY = (WORKING, WAITING)
LABEL = {IDLE: "bezczynny", WORKING: "pracuje", WAITING: "czeka na Ciebie", STALE: "zawieszona", ERROR: "błąd",
         COMPACTING: "kompaktuje kontekst"}
CLI_NAME = {"claude": "Claude Code", "gemini": "Gemini CLI", "codex": "Codex CLI"}

RED, AMBER, GREEN, GREY = QColor(255, 75, 58), QColor(255, 162, 31), QColor(47, 210, 124), QColor(120, 128, 138)
GLITCH = QColor(220, 70, 255)  # błąd tury / zawieszona sesja
BLUE = QColor(64, 156, 255)  # kompaktowanie
LAMP_COLOR = {WORKING: RED, WAITING: AMBER, IDLE: GREEN, ERROR: GLITCH, OFFLINE: GREY, COMPACTING: BLUE}
DOT_COLOR = {WORKING: QColor(217, 58, 43), WAITING: QColor(238, 143, 18), IDLE: QColor(31, 164, 99), STALE: GREY,
             ERROR: QColor(190, 60, 230), COMPACTING: QColor(46, 128, 230)}
# układ Matrix: praca w przygaszonej, butelkowej zieleni „Matrixa”, bezczynność jako zimny, biało-turkusowy
# terminal; pozostałe stany mają te same kolory co w układzie sygnalizatora
MATRIX_COLOR = {WORKING: QColor(27, 196, 106), IDLE: QColor(159, 232, 224)}

DEFAULTS = {
    # zachowanie
    "mode": "under",          # "top" | "under"
    "pop_rule": "any",        # "any" | "waiting"
    "pop_seconds": 5,         # 0 = zostaje na wierzchu do kliknięcia
    "stale_minutes": 10,
    "forget_hours": 12,
    "hotkey": "",             # przejdź do czekającego agenta; "" = wyłączony
    "click_jumps": True,      # kliknięcie w widget przełącza do czekającego terminala
    "question_waiting": True,  # odpowiedź zakończona pytaniem = czeka na Ciebie (pomarańczowe)
    # nie przeszkadzać
    "dnd_manual": False,
    "dnd_fullscreen": True,
    "dnd_hours": False,
    "dnd_from": "22:00",
    "dnd_to": "07:00",
    # dźwięki ("" = brak, nazwa pliku z C:\Windows\Media albo pełna ścieżka .wav)
    "sound_waiting": "",
    "notify_long_task": True,
    "long_task_minutes": 3,
    "sound_long_task": "Windows Notify System Generic.wav",
    "idle_remind": True,
    "idle_remind_minutes": 10,
    "sound_idle": "Windows Notify Calendar.wav",
    # wygląd
    "scale": 1.25,
    "orientation": "matrix",
    "opacity": 0.85,
    "housing": "dark",        # "dark" | "light" | "glass" | "auto"
    "state_rim": True,        # obwódka i poświata w kolorze stanu
    "glow": 0.6,              # siła poświaty 0..1
    "show_dots": True,
    # system
    "autostart": False,
    "pos": None,
}

# obudowa: (tło rgb, bazowa alfa tła, obwódka rgba, zgaszona lampa rgba)
HOUSING = {
    "dark":  ((16, 19, 23), 255, (255, 255, 255, 26), (255, 255, 255, 26)),
    "light": ((246, 247, 249), 255, (0, 0, 0, 36), (20, 28, 36, 34)),
    "glass": ((255, 255, 255), 70, (255, 255, 255, 150), (255, 255, 255, 72)),
}

HOTKEY_ID = 0xA11

# układy rysowane zamiast lamp; przy pracy animują się bez przerwy
MOVING_LAYOUTS = ("matrix", "eq")


def light_color(color, k):
    """Kolor rozjaśniony w stronę bieli (k = 0..1)."""
    return QColor.fromRgbF(color.redF() + (1 - color.redF()) * k, color.greenF() + (1 - color.greenF()) * k,
                           color.blueF() + (1 - color.blueF()) * k, color.alphaF())


# ---------------------------------------------------------------- config & sessions
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        stored = {}
    # migracja: stare pole "sound_on_waiting" (bool) -> "sound_waiting" (dźwięk)
    if stored.pop("sound_on_waiting", False) and "sound_waiting" not in stored:
        stored["sound_waiting"] = "Windows Exclamation.wav"
    cfg.update(stored)
    if cfg["orientation"] == "ring":  # pierścień zastąpiony ekranem Matrix
        cfg["orientation"] = "matrix"
    return cfg


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def write_session(path, record):
    rec = {k: v for k, v in record.items() if not k.startswith("_")}
    tmp = f"{path}.w.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02)
    _remove(tmp)


def load_sessions(cfg):
    out, now = [], time.time()
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return out
    for name in names:
        path = os.path.join(SESSIONS_DIR, name)
        if name.endswith(".tmp"):
            try:
                if now - os.path.getmtime(path) > 60:
                    os.remove(path)  # osierocony plik po nieudanym zapisie
            except OSError:
                pass
            continue
        if not name.endswith(".json"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        age = now - float(s.get("ts", 0))
        if age > cfg["forget_hours"] * 3600:
            _remove(path)
            continue
        s["_raw_state"] = s.get("state")
        if s.get("state") == IDLE and s.get("question") and cfg["question_waiting"]:
            s["state"] = WAITING  # agent skończył odpowiedź pytaniem i czeka na Ciebie
        # "brak sygnału" tylko dla pracy bez zdarzeń; czekanie na Ciebie może trwać dowolnie długo
        if s.get("state") == WORKING and age > cfg["stale_minutes"] * 60:
            s["state"] = STALE
        s["_path"] = path
        out.append(s)
    out.sort(key=lambda s: (s.get("project", ""), s.get("session_id", "")))
    return out


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def aggregate(sessions):
    """Stan zbiorczy: czekanie > błąd albo zawieszenie > kompaktowanie > praca > bezczynność; bez sesji – offline."""
    if not sessions:
        return OFFLINE
    states = {s.get("state") for s in sessions}
    if WAITING in states:
        return WAITING
    if states & {ERROR, STALE}:
        return ERROR
    if COMPACTING in states:
        return COMPACTING
    if WORKING in states:
        return WORKING
    return IDLE


def _text(content):
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def transcript_status(path, since_ts):
    """("interrupted", "") gdy tura przerwana klawiszem Esc, ("error", tekst) gdy skończyła się błędem API,
    inaczej (None, "")."""
    try:
        st = os.stat(path)
        if st.st_mtime < since_ts - 1:
            return None, ""  # od ostatniego zdarzenia nic nie dopisano
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None, ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        kind = obj.get("type")
        if kind == "assistant":
            if obj.get("isApiErrorMessage"):
                return "error", _text((obj.get("message") or {}).get("content")).strip() or "API Error"
            return None, ""
        if kind != "user":
            continue
        if "[Request interrupted by user" in _text((obj.get("message") or {}).get("content")):
            return "interrupted", ""
        return None, ""
    return None, ""


def fmt_duration(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}:{s:02d}"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------- sounds
def sound_path(spec):
    if not spec:
        return None
    return spec if os.path.isabs(spec) else os.path.join(MEDIA_DIR, spec)


def play_sound(spec):
    path = sound_path(spec)
    if not path or not os.path.exists(path):
        return
    import winsound
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


# ---------------------------------------------------------------- hotkey
def parse_hotkey(text):
    """"Ctrl+Alt+L" -> (mods, vk) dla RegisterHotKey, albo None."""
    seq = QKeySequence(text or "")
    if seq.isEmpty():
        return None
    combo = seq[0]
    mods_q, key = combo.keyboardModifiers(), combo.key()
    mods = 0
    if mods_q & Qt.ControlModifier:
        mods |= winapi.MOD_CONTROL
    if mods_q & Qt.AltModifier:
        mods |= winapi.MOD_ALT
    if mods_q & Qt.ShiftModifier:
        mods |= winapi.MOD_SHIFT
    if mods_q & Qt.MetaModifier:
        mods |= winapi.MOD_WIN
    k = int(key.value) if hasattr(key, "value") else int(key)
    if Qt.Key_A.value <= k <= Qt.Key_Z.value or Qt.Key_0.value <= k <= Qt.Key_9.value:
        vk = k
    elif Qt.Key_F1.value <= k <= Qt.Key_F24.value:
        vk = 0x70 + (k - Qt.Key_F1.value)
    elif k == Qt.Key_Space.value:
        vk = 0x20
    else:
        return None
    return mods, vk


# ---------------------------------------------------------------- system
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AITrafficLight"


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{exe}" "{os.path.abspath(__file__)}"')
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def hooks_installed():
    try:
        with open(install_hooks.SETTINGS, encoding="utf-8") as f:
            return install_hooks.MARK in f.read()
    except OSError:
        return False


def in_quiet_hours(cfg):
    now = QTime.currentTime()
    a, b = QTime.fromString(cfg["dnd_from"], "HH:mm"), QTime.fromString(cfg["dnd_to"], "HH:mm")
    if not a.isValid() or not b.isValid() or a == b:
        return False
    return a <= now < b if a < b else (now >= a or now < b)


# ---------------------------------------------------------------- the widget
class Light(QWidget):
    def __init__(self, cfg):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_AlwaysShowToolTips)
        self.setWindowTitle("AI Status Widget")

        self.cfg = cfg
        self.sessions = load_sessions(cfg)
        self.state = IDLE
        self.popped = False
        self._pop_t0 = -10.0
        self._drag = None
        self._moved = False
        self._prev = None            # {session_id: state} z poprzedniego odczytu
        self._reminded = set()       # (session_id, since) – przypomnienia o bezczynności już wysłane
        self._last_deep_check = 0.0
        self._hotkey_on = False

        self.poll = QTimer(self, interval=300, timeout=self.refresh)
        self.anim = QTimer(self, interval=33, timeout=self._tick)
        self.unpop = QTimer(self, singleShot=True, timeout=self.send_back)
        self.auto_housing = "dark"
        self.bg_probe = QTimer(self, interval=1500, timeout=self.probe_background)

        self.tray = QSystemTrayIcon(self)
        self.tray_menu = QMenu()
        self.tray_menu.aboutToShow.connect(lambda: self.fill_menu(self.tray_menu))
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._tray_click)
        self.tray.show()

        self.relayout()
        self.restore_pos()
        self.refresh(initial=True)
        self.poll.start()
        self.apply_appearance()

    # ---- kolory
    def color(self, state):
        """Kolor stanu dla bieżącego układu (Matrix ma własną paletę dla pracy i bezczynności)."""
        if self.cfg["orientation"] == "matrix" and state in MATRIX_COLOR:
            return MATRIX_COLOR[state]
        return LAMP_COLOR[state]

    def dot_color(self, state):
        if self.cfg["orientation"] == "matrix" and state in MATRIX_COLOR:
            return MATRIX_COLOR[state]
        return DOT_COLOR.get(state, GREY)

    # ---- geometry
    def dims(self):
        s = self.cfg["scale"]
        d, gap, pad, margin = 14 * s, 6 * s, 7 * s, 16 * s
        long_side, short_side = 3 * d + 2 * gap + 2 * pad, d + 2 * pad
        layout = self.cfg["orientation"]
        tile, tgap, cols, rows = 20 * s + 2 * pad, 3 * s, 1, 1
        if layout == "matrix":  # kafelek na sesję, siatka rośnie: 1, 2×1, 2×2, 3×2, 3×3…
            n = max(1, len(self.sessions))
            cols = math.ceil(math.sqrt(n))
            rows = math.ceil(n / cols)
            cw, ch = cols * tile + (cols - 1) * tgap, rows * tile + (rows - 1) * tgap
        elif layout == "single":
            cw = ch = tile  # okrągła obudowa
        elif layout == "eq":
            cw, ch = 29 * s + 2 * pad, 20 * s + 2 * pad
        elif layout == "vertical":
            cw, ch = short_side, long_side
        else:
            cw, ch = long_side, short_side
        dot, dgap, dtop = 4 * s, 3 * s, 5 * s
        # w układzie Matrix każda sesja ma własny kafelek, więc kropki są zbędne
        n = len(self.sessions) if self.cfg["show_dots"] and layout != "matrix" else 0
        dots_w = n * dot + max(n - 1, 0) * dgap
        return dict(s=s, d=d, gap=gap, pad=pad, m=margin, cw=cw, ch=ch, dot=dot, dgap=dgap, dtop=dtop, n=n,
                    dots_w=dots_w, tile=tile, tgap=tgap, cols=cols, rows=rows)

    def matrix_tiles(self, g=None):
        """[(prostokąt, sesja albo None)] kafelków Matrix w kolejności „jak w książce”: od najstarszej sesji,
        od lewej do prawej, potem kolejny rząd – nowy terminal zawsze dostaje następne wolne miejsce."""
        g = g or self.dims()
        body = self.body_rect(g)
        sessions = sorted(self.sessions, key=lambda s: (float(s.get("started") or s.get("ts") or 0),
                                                       s.get("session_id", ""))) or [None]
        step = g["tile"] + g["tgap"]
        out = []
        for i, sess in enumerate(sessions):
            row, col = divmod(i, g["cols"])
            out.append((QRectF(body.left() + col * step, body.top() + row * step, g["tile"], g["tile"]), sess))
        return out

    def relayout(self):
        g = self.dims()
        w = math.ceil(max(g["cw"], g["dots_w"]) + 2 * g["m"])
        h = math.ceil(g["ch"] + (g["dtop"] + g["dot"] if g["n"] else 0) + 2 * g["m"])
        old = self.geometry()
        self.setFixedSize(w, h)
        screen = self.screen()
        if self.isVisible() and screen is not None and (w, h) != (old.width(), old.height()):
            # przy prawej (dolnej) krawędzi ekranu nowe kafelki pojawiają się po lewej (u góry)
            area = screen.availableGeometry()
            x = old.x() + (old.width() - w if old.center().x() > area.center().x() else 0)
            y = old.y() + (old.height() - h if old.center().y() > area.center().y() else 0)
            if (x, y) != (old.x(), old.y()):
                self.move(x, y)
                self.cfg["pos"] = [x, y]
                save_config(self.cfg)
        self.update()

    def body_rect(self, g=None):
        g = g or self.dims()
        return QRectF(self.width() / 2 - g["cw"] / 2, g["m"], g["cw"], g["ch"])

    def dot_centers(self, g=None):
        g = g or self.dims()
        if not g["n"]:
            return []
        y = self.body_rect(g).bottom() + g["dtop"] + g["dot"] / 2
        x0 = self.width() / 2 - g["dots_w"] / 2 + g["dot"] / 2
        return [QPointF(x0 + i * (g["dot"] + g["dgap"]), y) for i in range(g["n"])]

    def restore_pos(self):
        pos = self.cfg.get("pos")
        screen = QApplication.primaryScreen().availableGeometry()
        if pos and any(sc.availableGeometry().contains(QPoint(*pos)) for sc in QApplication.screens()):
            self.move(*pos)
        else:
            self.move(screen.right() - self.width() - 24, screen.top() + 120)

    # ---- state
    def refresh(self, initial=False):
        prev_n = len(self.sessions)
        sessions = load_sessions(self.cfg)
        now = time.time()
        if initial or now - self._last_deep_check > 2:
            self._last_deep_check = now
            sessions = self.deep_check(sessions, now)
        self.sessions = sessions
        if len(sessions) != prev_n and (self.cfg["show_dots"] or self.cfg["orientation"] == "matrix"):
            self.relayout()

        new = aggregate(sessions)
        changed = new != self.state
        self.state = new
        self.setToolTip(self.tooltip_text())
        self.tray.setToolTip("AI Status Widget: " + self.caption())
        self.tray.setIcon(self.tray_icon())

        events = self.session_events(sessions, now, initial)
        if changed and not initial:
            self.on_change(new)
        if events and not initial:
            self.notify(events)
        self._sync_anim()
        self.update()

    def deep_check(self, sessions, now):
        """Co ~2 s: usuń sesje zamkniętych procesów, wykryj przerwanie klawiszem Esc i turę zakończoną błędem API
        (po błędzie Claude Code nie zawsze wywołuje hook Stop, więc sesja zostałaby „pracująca”)."""
        alive = []
        for s in sessions:
            pid = s.get("pid")
            if pid and winapi.process_alive(pid, s.get("pid_created")) is False:
                _remove(s["_path"])
                continue
            if (s.get("_raw_state") in BUSY + (COMPACTING,) and s.get("transcript_path")
                    and now - float(s.get("ts", 0)) > 2):
                status, text = transcript_status(s["transcript_path"], float(s.get("ts", 0)))
                if status == "interrupted":
                    s.update(state=IDLE, _raw_state=IDLE, event="Interrupted", detail="przerwane przez użytkownika",
                             ts=now, since=now, work_started=None)
                elif status == "error":
                    s.update(state=ERROR, _raw_state=ERROR, event="ApiError", detail=text[:120], question="",
                             ts=now, since=now)
                if status:
                    try:
                        write_session(s["_path"], s)
                    except OSError:
                        pass
            alive.append(s)
        return alive

    def session_events(self, sessions, now, initial):
        """Przejścia stanów sesji -> lista zdarzeń do powiadomienia."""
        events = []
        prev = self._prev or {}
        for s in sessions:
            sid, st = s.get("session_id"), s.get("state")
            before = prev.get(sid)
            if self._prev is not None and before != st:
                if st == WAITING:
                    events.append(("waiting", s))
                elif st == IDLE and before in BUSY and s.get("event") != "Interrupted":
                    dur = s.get("last_work_seconds") or 0
                    if self.cfg["notify_long_task"] and dur >= self.cfg["long_task_minutes"] * 60:
                        events.append(("done", s))
            if st == IDLE and self.cfg["idle_remind"] and s.get("since"):
                key = (sid, s["since"])
                if key not in self._reminded and now - float(s["since"]) >= self.cfg["idle_remind_minutes"] * 60:
                    self._reminded.add(key)
                    if not initial:
                        events.append(("idle", s))
        self._prev = {s.get("session_id"): s.get("state") for s in sessions}
        return events

    def caption(self):
        n_wait = sum(s["state"] == WAITING for s in self.sessions)
        n_err = sum(s["state"] in (ERROR, STALE) for s in self.sessions)
        n_work = sum(s["state"] == WORKING for s in self.sessions)
        n_compact = sum(s["state"] == COMPACTING for s in self.sessions)
        if n_wait:
            return f"{n_wait} czeka na Ciebie"
        if n_err:
            return f"{n_err} z błędem lub zawieszona"
        if n_compact:
            return f"{n_compact} kompaktuje kontekst"
        if n_work:
            return f"{n_work} pracuje"
        return "wszyscy wolni" if self.sessions else "brak aktywnych sesji"

    def session_line(self, s):
        cli = CLI_NAME.get(s.get("cli"), s.get("cli", ""))
        line = f"{s.get('project', '?')} · {cli} — {LABEL[s['state']]}"
        if s.get("since") and s["state"] != STALE:
            line += f" {fmt_duration(time.time() - float(s['since']))}"
        return line

    def tooltip_text(self):
        if not self.sessions:
            text = "Brak aktywnych sesji"
        else:
            lines = []
            for s in self.sessions:
                line = self.session_line(s)
                if s["state"] in (WORKING, WAITING, ERROR, COMPACTING) and s.get("detail"):
                    line += f"\n    {s['detail']}"
                lines.append(line)
            text = "\n".join(lines)
        if self.dnd_active():
            text += "\n\nNie przeszkadzać: włączone"
        return text

    # ---- nie przeszkadzać & powiadomienia
    def dnd_active(self):
        c = self.cfg
        if c["dnd_manual"] or (c["dnd_hours"] and in_quiet_hours(c)):
            return True
        return bool(c["dnd_fullscreen"] and winapi.fullscreen_app_active())

    def on_change(self, new):
        if self.dnd_active():
            return
        if self.cfg["mode"] == "under" and (self.cfg["pop_rule"] == "any" or new in (WAITING, ERROR)):
            self.pop()
        elif self.cfg["mode"] == "top":
            self._pop_t0 = time.monotonic()  # sama animacja, bez zmiany warstwy

    def notify(self, events):
        """Tylko dźwięki – bez dymków Windows; resztę mówi sam widget."""
        if self.dnd_active():
            return
        kinds = {k for k, _ in events}
        if "waiting" in kinds:
            play_sound(self.cfg["sound_waiting"])
        elif "done" in kinds:
            play_sound(self.cfg["sound_long_task"])
        elif "idle" in kinds:
            play_sound(self.cfg["sound_idle"])

    # ---- przejście do terminala
    def focus_session(self, s):
        hwnd = winapi.terminal_window(s.get("pid"))
        return bool(hwnd and winapi.focus_window(hwnd))

    def attention_session(self):
        """Sesja, która najbardziej potrzebuje uwagi: najdłużej czekająca, potem ostatnio aktywna."""
        for wanted in ((WAITING,), (ERROR, STALE)):
            found = [s for s in self.sessions if s["state"] in wanted]
            if found:
                return min(found, key=lambda s: float(s.get("since") or 0))
        if self.sessions:
            return max(self.sessions, key=lambda s: float(s.get("ts") or 0))
        return None

    def jump_to_attention(self):
        s = self.attention_session()
        if s:
            self.focus_session(s)
        else:
            self.pop()

    def register_hotkey(self):
        if self._hotkey_on:
            winapi.unregister_hotkey(self.hwnd(), HOTKEY_ID)
            self._hotkey_on = False
        parsed = parse_hotkey(self.cfg["hotkey"])
        if parsed:
            self._hotkey_on = winapi.register_hotkey(self.hwnd(), HOTKEY_ID, *parsed)
        return self._hotkey_on

    def nativeEvent(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == winapi.WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.jump_to_attention()
                return True, 0
        return super().nativeEvent(event_type, message)

    # ---- z-order
    def hwnd(self):
        return int(self.winId())

    def apply_layer(self):
        self.unpop.stop()
        self.popped = False
        if self.cfg["mode"] == "top":
            winapi.set_z(self.hwnd(), winapi.HWND_TOPMOST)
        else:
            winapi.set_z(self.hwnd(), winapi.HWND_NOTOPMOST)
            winapi.set_z(self.hwnd(), winapi.HWND_BOTTOM)

    def pop(self):
        winapi.set_z(self.hwnd(), winapi.HWND_TOPMOST)
        self.popped = True
        self._pop_t0 = time.monotonic()
        self._sync_anim()
        self.unpop.stop()
        if self.cfg["pop_seconds"] > 0:
            self.unpop.start(int(self.cfg["pop_seconds"] * 1000))

    def send_back(self):
        if self.cfg["mode"] == "under":
            self.apply_layer()

    # ---- appearance
    def housing(self):
        h = self.cfg["housing"]
        return self.auto_housing if h == "auto" else h

    def apply_appearance(self):
        if self.cfg["housing"] == "auto":
            self.probe_background()
            self.bg_probe.start()
        else:
            self.bg_probe.stop()
        self.relayout()

    def probe_background(self):
        """Średnia jasność tła tuż wokół widgetu -> jasna albo ciemna obudowa."""
        screen = self.screen()
        if screen is None:
            return
        pad = 10
        geo = self.frameGeometry().adjusted(-pad, -pad, pad, pad)
        sg = screen.geometry()
        img = screen.grabWindow(0, geo.x() - sg.x(), geo.y() - sg.y(), geo.width(), geo.height()).toImage()
        if img.isNull():
            return
        n = 24
        small = img.scaled(n, n, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        total = count = 0
        for y in range(n):
            for x in range(n):
                if 0 < x < n - 1 and 0 < y < n - 1:
                    continue  # tylko pierścień zewnętrzny, czyli tło poza widgetem
                c = small.pixelColor(x, y)
                total += 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
                count += 1
        lum = total / count
        new = self.auto_housing
        if lum < 0.38:
            new = "light"
        elif lum > 0.62:
            new = "dark"
        if new != self.auto_housing:
            self.auto_housing = new
            self.update()

    # ---- animation
    def _animating(self):
        layout = self.cfg["orientation"]
        moving = layout in MOVING_LAYOUTS and self.state == WORKING
        noise = layout == "matrix" and self.state == OFFLINE  # szum na wygaszonym ekranie
        return (moving or noise or self.state in (WAITING, ERROR, COMPACTING)
                or time.monotonic() - self._pop_t0 < 1.2)

    @staticmethod
    def _rng(now, salt=0, fps=14):
        """Generator losowy stały w obrębie jednej klatki glitcha (zmienia się fps razy na sekundę)."""
        return random.Random(int(now * fps) * 7919 + salt)

    def _sync_anim(self):
        if self._animating():
            if not self.anim.isActive():
                self.anim.start()
        else:
            self.anim.stop()

    def _tick(self):
        self.update()
        self._sync_anim()

    # ---- painting
    def paintEvent(self, _):
        g = self.dims()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        now = time.monotonic()
        t = now - self._pop_t0

        body = self.body_rect(g)
        radius = 2 if self.cfg["orientation"] == "matrix" else min(g["cw"], g["ch"]) / 2

        # animacja "pop": sprężyste powiększenie
        if 0 <= t < 0.5:
            k = t / 0.5 - 1
            sc = 0.8 + 0.2 * (1 + 2.70158 * k ** 3 + 1.70158 * k ** 2)  # easeOutBack
            p.translate(body.center())
            p.scale(sc, sc)
            p.translate(-body.center())

        # błąd / zawieszenie: co jakiś czas cały widget drga w bok
        glitch = self.state == ERROR and self._rng(now).random() < 0.3
        if glitch:
            p.translate(self._rng(now, 1).uniform(-2, 2) * g["s"], 0)

        breathe = 0.72 + 0.28 * math.cos(now * math.tau / 1.6)
        pulse = self._pulse(self.state, now, breathe)
        state_col = self.color(self.state)
        rim = self.cfg["state_rim"]
        # bryły z cieniem i poświatą: cała obudowa albo (Matrix) każdy kafelek w kolorze swojej sesji
        if self.cfg["orientation"] == "matrix":
            shapes = [(rect, self.color(self.tile_state(sess)), self._pulse(self.tile_state(sess), now, breathe))
                      for rect, sess in self.matrix_tiles(g)]
        else:
            shapes = [(body, state_col, pulse)]

        # cień
        for shape, _, _ in shapes:
            for i, a in enumerate((18, 12, 7)):
                sh = shape.adjusted(-i - 1, -i + 2, i + 1, i + 4)
                path = QPainterPath()
                path.addRoundedRect(sh, radius + i, radius + i)
                p.fillPath(path, QColor(0, 0, 0, a))

        # poświata dookoła obudowy w kolorze stanu
        if rim and self.cfg["glow"] > 0:
            layers = 7
            for shape, color, shape_pulse in shapes:
                for i in range(layers, 0, -1):
                    grow = i * 2 * g["s"]
                    c = QColor(color)
                    c.setAlphaF(min(1.0, 0.16 * self.cfg["glow"] * shape_pulse * (1 - (i - 1) / layers)))
                    halo = QPainterPath()
                    halo.addRoundedRect(shape.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow)
                    p.fillPath(halo, c)

        # obudowa (Matrix: każdy kafelek ma własną, w kolorze swojej sesji)
        bg_rgb, bg_alpha, border, lamp_off = HOUSING[self.housing()]
        layout = self.cfg["orientation"]
        if layout == "matrix":
            self._paint_matrix_grid(p, g, now, breathe, bg_rgb, bg_alpha, border, rim)
        else:
            path = QPainterPath()
            path.addRoundedRect(body, radius, radius)
            p.fillPath(path, QColor(*bg_rgb, int(bg_alpha * self.cfg["opacity"])))
            if rim:
                c = QColor(state_col)
                c.setAlphaF(0.55 + 0.35 * pulse)
                p.setPen(QPen(c, 1.5 * g["s"]))
            else:
                p.setPen(QPen(QColor(*border), 1))
            p.drawPath(path)

        # poświata przy wyskoczeniu
        if 0 <= t < 1.2:
            k = t / 1.2
            grow = 12 * g["s"] * k
            ring = QPainterPath()
            ring.addRoundedRect(body.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow)
            p.setPen(QPen(QColor(255, 255, 255, int(150 * (1 - k))), 2 * g["s"]))
            p.drawPath(ring)

        if layout == "eq":
            self._paint_eq(p, body, g, now, lamp_off)

        # lampy
        single = layout == "single"
        lamps = () if layout in MOVING_LAYOUTS else (self.state,) if single else (WORKING, WAITING, IDLE)
        for i, st in enumerate(lamps):
            off = g["pad"] + i * (g["d"] + g["gap"]) + g["d"] / 2
            if single:
                c = body.center()
            elif self.cfg["orientation"] == "vertical":
                c = QPointF(body.center().x(), body.top() + off)
            else:
                c = QPointF(body.left() + off, body.center().y())
            r = g["d"] / 2
            p.setPen(Qt.NoPen)
            if self.state == ERROR:  # lampy migają chaotycznie w kolorze glitcha
                lit, col = self._rng(now, 10 + i).random() < 0.45, QColor(GLITCH)
            elif self.state == COMPACTING:  # ładowanie: niebieskie światło przeskakuje po kolei
                lit, col = single or int(now * 8) % len(lamps) == i, QColor(BLUE)
            else:  # offline: wszystkie zgaszone
                lit, col = st == self.state and st != OFFLINE, QColor(LAMP_COLOR[st])
            if lit:
                alpha = breathe if st == WAITING or (single and self.state == COMPACTING) else 1.0
                glow = QRadialGradient(c, r * 2.3)
                gc = QColor(col)
                gc.setAlphaF(0.55 * alpha)
                glow.setColorAt(0.35, gc)
                gc.setAlphaF(0)
                glow.setColorAt(1, gc)
                p.setBrush(glow)
                p.drawEllipse(c, r * 2.3, r * 2.3)
                col.setAlphaF(0.55 + 0.45 * alpha)
                p.setBrush(col)
            else:
                p.setBrush(QColor(*lamp_off))
            p.drawEllipse(c, r, r)

        # kropki sesji
        for s, c in zip(self.sessions, self.dot_centers(g)):
            p.setBrush(self.dot_color(s["state"]))
            p.drawEllipse(c, g["dot"] / 2, g["dot"] / 2)
        p.end()

    def _center_glow(self, p, c, r, color, alpha):
        grad = QRadialGradient(c, r)
        gc = QColor(color)
        gc.setAlphaF(alpha)
        grad.setColorAt(0, gc)
        gc.setAlphaF(0)
        grad.setColorAt(1, gc)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawEllipse(c, r, r)

    def _pulse(self, st, now, breathe):
        """Jasność obwódki i poświaty: pulsuje przy czekaniu, migocze przy błędzie, przygaszona bez sesji."""
        return {WAITING: breathe, OFFLINE: 0.25, ERROR: 0.4 + 0.6 * self._rng(now, 2).random()}.get(st, 1.0)

    @staticmethod
    def tile_state(sess):
        if sess is None:
            return OFFLINE
        return ERROR if sess["state"] == STALE else sess["state"]

    def _paint_matrix_grid(self, p, g, now, breathe, bg_rgb, bg_alpha, border, rim):
        """Kafelek na sesję: obudowa z obwódką w kolorze stanu tej sesji i jej własny ekran Matrix."""
        for rect, sess in self.matrix_tiles(g):
            st = self.tile_state(sess)
            pulse = self._pulse(st, now, breathe)
            path = QPainterPath()
            path.addRoundedRect(rect, 2, 2)
            p.fillPath(path, QColor(*bg_rgb, int(bg_alpha * self.cfg["opacity"])))
            if rim:
                c = QColor(self.color(st))
                c.setAlphaF(0.55 + 0.35 * pulse)
                p.setPen(QPen(c, 1.5 * g["s"]))
            else:
                p.setPen(QPen(QColor(*border), 1))
            p.drawPath(path)
            self._paint_matrix(p, rect, g, now, breathe, st)

    def _paint_matrix(self, p, body, g, now, breathe, st):
        """Ekran w stylu Matrix: kursor `>_` (bezczynny), deszcz znaków (pracuje), glitchujący `?` (czeka),
        wygaszony ekran z szumem (brak sesji), zakłócony obraz z `ERR` (błąd tury albo zawieszona sesja)."""
        s = g["s"]
        col = QColor(self.color(st))
        screen = body.adjusted(2 * s, 2 * s, -2 * s, -2 * s)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(screen, 2, 2)
        p.setClipPath(clip)
        if st != OFFLINE:
            self._center_glow(p, screen.center(), screen.width() * 0.7, col, 0.22 * (breathe if st == WAITING else 1.0))
        if st == OFFLINE:
            self._paint_noise(p, screen, s, now)
        elif st == ERROR:
            self._paint_error(p, screen, s, now)
        elif st == COMPACTING:
            self._paint_compacting(p, screen, s, now, col)
        elif st == WORKING:
            self._paint_rain(p, screen, s, now, col)
        elif st == WAITING:
            glitch = (now * 2.3) % 1 < 0.12  # na moment rozszczepienie na cyjan i magentę
            self._paint_glyph(p, screen, s, "?", col, 11 * s, Qt.AlignCenter, glitch)
        else:  # znak zachęty w lewym dolnym rogu, jak w terminalu
            prompt = screen.adjusted(2.5 * s, 2 * s, -2 * s, -1.5 * s)
            self._paint_glyph(p, prompt, s, ">_" if int(now * 2) % 2 else ">", col, 7 * s, Qt.AlignLeft | Qt.AlignBottom)
        scan = QColor(0, 0, 0, 60)  # linie skanowania jak na monitorze kineskopowym
        y = screen.top()
        while y < screen.bottom():
            p.fillRect(QRectF(screen.left(), y, screen.width(), 0.6 * s), scan)
            y += 1.8 * s
        p.restore()

    def _paint_noise(self, p, rect, s, now):
        """Szum jak na wyłączonym telewizorze: szare ziarno i wolno przesuwający się jaśniejszy pas."""
        rnd = self._rng(now, fps=12)
        cell = 1.5 * s
        band = rect.top() + (now * 0.35 % 1) * rect.height()
        y = rect.top()
        while y < rect.bottom():
            near = max(0.0, 1 - abs(y - band) / (5 * s))  # w pasie szum jest jaśniejszy
            x = rect.left()
            while x < rect.right():
                v = rnd.random()
                if v > 0.5:
                    level = int(60 + 110 * v + 60 * near)
                    p.fillRect(QRectF(x, y, cell, cell), QColor(level, level, level, int(70 + 80 * v)))
                x += cell
            y += cell

    def _paint_error(self, p, rect, s, now):
        """Zakłócony obraz: przesunięte pasy w cyjanie i magencie, drgający napis ERR z rozszczepieniem kolorów."""
        rnd = self._rng(now, 3)
        if rnd.random() < 0.08:
            self._paint_noise(p, rect, s, now)  # na moment sam szum, jakby sygnał zniknął
            return
        for _ in range(rnd.randint(1, 4)):
            q = QColor(*rnd.choice(((0, 240, 255), (255, 40, 190), (220, 70, 255), (255, 255, 255))))
            q.setAlphaF(rnd.uniform(0.25, 0.6))
            w = rect.width() * rnd.uniform(0.3, 1.0)
            p.fillRect(QRectF(rect.left() + rnd.uniform(0, rect.width() - w), rnd.uniform(rect.top(), rect.bottom()),
                              w, rnd.uniform(0.6, 2.5) * s), q)
        shift = rnd.uniform(-2, 2) * s if rnd.random() < 0.5 else 0
        self._paint_glyph(p, rect.translated(shift, 0), s, "ERR", GLITCH, 8 * s, Qt.AlignCenter, glitch=True)

    RAIN_GLYPHS = "01アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモ"

    def _font(self, px):
        font = QFont("MS Gothic")
        font.setPixelSize(max(6, int(px)))
        font.setBold(True)
        return font

    def _paint_compacting(self, p, rect, s, now, col):
        """Kompaktowanie: skaner jak KITT z „Knight Ridera” – światło z gasnącym ogonem jeździ od ściany do ściany."""
        cells, gap = 7, 1.0 * s
        cw = (rect.width() - 4 * s - (cells - 1) * gap) / cells
        h = 5.5 * s
        y = rect.center().y() - h / 2
        period = 0.8  # sekundy na przejazd w jedną stronę

        def head(t):  # pozycja światła 0..cells-1, z hamowaniem przy ścianach
            return (cells - 1) * (0.5 - 0.5 * math.cos(t * math.pi / period))

        levels = [0.0] * cells
        for k in range(8):  # ogon: wcześniejsze położenia, coraz słabsze
            pos = head(now - k * 0.028)
            for i in range(cells):
                levels[i] = max(levels[i], 0.8 ** k * max(0.0, 1 - abs(i - pos)))
        lead = head(now)
        glow = QRadialGradient(QPointF(rect.left() + 2 * s + lead * (cw + gap) + cw / 2, rect.center().y()), 9 * s)
        gc = QColor(col)
        gc.setAlphaF(0.45)
        glow.setColorAt(0, gc)
        gc.setAlphaF(0)
        glow.setColorAt(1, gc)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawRect(rect)
        for i, level in enumerate(levels):
            x = rect.left() + 2 * s + i * (cw + gap)
            q = light_color(QColor(col), 0.35 * level) if level > 0.6 else QColor(col)
            q.setAlphaF(0.12 + 0.88 * level)
            p.fillRect(QRectF(x, y, cw, h), q)

    def _paint_rain(self, p, rect, s, now, col):
        """Deszcz znaków jak w Matriksie: kolumny opadają z różną prędkością, głowa kolumny jest najjaśniejsza."""
        ch = 4.6 * s
        p.setFont(self._font(ch))
        cols = max(3, int(rect.width() / ch))
        x0 = rect.center().x() - cols * ch / 2
        trail = 6
        span = rect.height() + trail * ch
        for k in range(cols):
            speed = 2.2 + (k * 0.61) % 1.4  # znaków na sekundę, różne dla kolumn
            head = rect.top() + (now * speed * ch + span * ((k * 0.618) % 1)) % span
            for j in range(trail):
                y = head - j * ch
                glyph = self.RAIN_GLYPHS[int(now * 9 + k * 7 + j * 3) % len(self.RAIN_GLYPHS)]
                q = light_color(col, 0.75) if j == 0 else QColor(col)
                q.setAlphaF(1.0 if j == 0 else 0.85 - j * 0.13)
                p.setPen(q)
                p.drawText(QRectF(x0 + k * ch, y - ch / 2, ch, ch), Qt.AlignCenter, glyph)

    def _paint_glyph(self, p, rect, s, text, col, size, align, glitch=False):
        """Tekst w prostokącie; przy glitchu z przesuniętymi kopiami w cyjanie i magencie."""
        p.setFont(self._font(size))
        if glitch:
            for q, dx in ((QColor(0, 240, 255, 200), -1.6 * s), (QColor(255, 40, 190, 200), 1.6 * s)):
                p.setPen(q)
                p.drawText(rect.translated(dx, 0), align, text)
        p.setPen(light_color(col, 0.2))
        p.drawText(rect, align, text)

    def _paint_eq(self, p, body, g, now, lamp_off):
        """Korektor: niskie, nieruchome słupki (bezczynny), tańczące (pracuje), podskakujące razem (czeka)."""
        s = g["s"]
        bars, bw, gap = 5, 3.4 * s, 2.5 * s
        top, bottom = body.top() + g["pad"], body.bottom() - g["pad"]
        full = bottom - top
        x0 = body.center().x() - (bars * bw + (bars - 1) * gap) / 2
        col = QColor(self.color(self.state))
        shape = (0.6, 0.8, 1.0, 0.8, 0.6)
        bounce = abs(math.sin(now * math.pi * 1.25))
        p.setPen(Qt.NoPen)
        for i in range(bars):
            x = x0 + i * (bw + gap)
            p.setBrush(QColor(*lamp_off))
            p.drawRoundedRect(QRectF(x, top, bw, full), bw / 2, bw / 2)
            if self.state == ERROR:  # słupki skaczą chaotycznie
                level = self._rng(now, 20 + i).random()
            elif self.state == OFFLINE:  # płaskie, wygaszone
                level = 0
            elif self.state == COMPACTING:  # fala ładowania przesuwa się od lewej do prawej
                level = 0.15 + 0.85 * max(0.0, math.cos((now * 2.6 - i / bars) * math.tau)) ** 3
            elif self.state == WORKING:
                level = 0.55 + 0.25 * math.sin(now * 7.3 + i * 1.9) + 0.2 * math.sin(now * 4.1 * (1 + i * 0.17) + i)
            elif self.state == WAITING:
                level = (0.3 + 0.7 * bounce) * shape[i]
            else:
                level = 0.22 + 0.08 * shape[i]
            h = max(bw, full * min(1.0, max(0.12, level)))
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x, bottom - h, bw, h), bw / 2, bw / 2)

    def tray_icon(self):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(16, 19, 23))
        p.drawRoundedRect(QRectF(2, 2, 28, 28), 14, 14)
        col = QColor(self.color(self.state))
        if self.cfg["dnd_manual"]:
            col.setAlphaF(0.4)
        p.setBrush(col)
        p.drawEllipse(QPointF(16, 16), 8, 8)
        p.end()
        return QIcon(pm)

    # ---- mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False

    def event(self, e):
        if e.type() == QEvent.ToolTip and self.cfg["orientation"] == "matrix" and self.sessions:
            for rect, sess in self.matrix_tiles():
                if sess and rect.contains(QPointF(e.pos())):
                    text = self.session_line(sess)
                    if sess.get("detail") and sess["state"] != IDLE:
                        text += f"\n    {sess['detail']}"
                    QToolTip.showText(e.globalPos(), text, self)
                    return True
        return super().event(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)
            self._moved = True

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._drag = None
        if self._moved:
            self.cfg["pos"] = [self.x(), self.y()]
            save_config(self.cfg)
        else:
            self.handle_click(e.position())
        if self.cfg["mode"] == "under":
            self.apply_layer()  # kliknięcie = "widzę, schowaj"

    def handle_click(self, pos):
        g = self.dims()
        if self.cfg["orientation"] == "matrix":
            for rect, sess in self.matrix_tiles(g):
                if sess and rect.contains(pos):
                    self.focus_session(sess)
                    return
        hit = max(g["dot"] / 2 + g["dgap"] / 2, 5)
        for s, c in zip(self.sessions, self.dot_centers(g)):
            if abs(pos.x() - c.x()) <= hit and abs(pos.y() - c.y()) <= hit + 2:
                self.focus_session(s)
                return
        if self.cfg["click_jumps"]:
            waiting = [s for s in self.sessions if s["state"] == WAITING]
            if waiting:
                self.focus_session(min(waiting, key=lambda s: float(s.get("since") or 0)))

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        self.fill_menu(menu)
        menu.exec(e.globalPos())
        if self.cfg["mode"] == "under" and not self.popped:
            self.apply_layer()

    def _tray_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.pop()

    # ---- menu
    def fill_menu(self, menu):
        menu.clear()
        head = menu.addAction(f"AI Status Widget · {self.caption()}")
        head.setEnabled(False)

        target = self.attention_session()
        if target:
            label = "Przejdź do czekającego agenta" if target["state"] == WAITING else "Przejdź do ostatniej sesji"
            a = menu.addAction(f"{label}: {target.get('project', '?')}", lambda *_, s=target: self.focus_session(s))
            if self._hotkey_on:
                a.setShortcut(QKeySequence(self.cfg["hotkey"]))
                a.setShortcutVisibleInContextMenu(True)

        sub = menu.addMenu(f"Sesje ({len(self.sessions)})")
        if not self.sessions:
            sub.addAction("Brak aktywnych sesji").setEnabled(False)
        for s in self.sessions:
            a = sub.addAction(self.session_line(s), lambda *_, s=s: self.focus_session(s))
            a.setIcon(self._dot_icon(self.dot_color(s["state"])))
            a.setToolTip(s.get("cwd", ""))
        sub.addSeparator()
        sub.addAction("Wyczyść listę sesji", self.clear_sessions)

        menu.addSeparator()
        grp = QActionGroup(menu)
        for key, text in (("top", "Zawsze na wierzchu"), ("under", "Pod oknami, wyskakuj przy zmianie")):
            a = QAction(text, menu, checkable=True, checked=self.cfg["mode"] == key)
            a.triggered.connect(lambda _=False, k=key: self.set_mode(k))
            grp.addAction(a)
            menu.addAction(a)
        dnd = QAction("Nie przeszkadzać", menu, checkable=True, checked=self.cfg["dnd_manual"])
        dnd.triggered.connect(self.toggle_dnd)
        menu.addAction(dnd)

        menu.addSeparator()
        menu.addAction("Ustawienia…", self.open_settings)
        menu.addSeparator()
        menu.addAction("Zamknij", QApplication.quit)

    @staticmethod
    def _dot_icon(color):
        pm = QPixmap(12, 12)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QRectF(2, 2, 8, 8))
        p.end()
        return QIcon(pm)

    def set_mode(self, mode):
        self.cfg["mode"] = mode
        save_config(self.cfg)
        self.apply_layer()

    def toggle_dnd(self, on):
        self.cfg["dnd_manual"] = bool(on)
        save_config(self.cfg)
        self.refresh()

    def clear_sessions(self):
        for s in self.sessions:
            _remove(s["_path"])
        self.refresh()

    def open_settings(self):
        before = dict(self.cfg)
        dlg = SettingsDialog(self.cfg, self.preview)
        winapi.set_z(int(dlg.winId()), winapi.HWND_TOPMOST)
        if dlg.exec() == QDialog.Accepted:
            self.cfg.update(dlg.values())
            save_config(self.cfg)
            try:
                set_autostart(self.cfg["autostart"])
            except OSError:
                pass
        else:
            self.cfg.clear()
            self.cfg.update(before)
        self.register_hotkey()
        self.apply_appearance()
        self.apply_layer()
        self.refresh()

    def preview(self, values):
        """Podgląd na żywo wyglądu z otwartego okna ustawień."""
        self.cfg.update({k: values[k] for k in APPEARANCE_KEYS})
        self.apply_appearance()


APPEARANCE_KEYS = ("housing", "state_rim", "glow", "opacity", "scale", "orientation", "show_dots")

# nazwa -> (obudowa, obwódka w kolorze stanu)
STYLE_PRESETS = [
    ("Kolorowa obwódka (domyślny)", "dark", True),
    ("Ciemna, bez obwódki", "dark", False),
    ("Jasna", "light", False),
    ("Szklana", "glass", False),
    ("Jasna z kolorową obwódką", "light", True),
    ("Automatyczny kontrast", "auto", False),
    ("Automatyczny kontrast z obwódką", "auto", True),
]


# ---------------------------------------------------------------- settings dialog
class SoundPicker(QWidget):
    """Lista dźwięków (Brak / dźwięki Windows / własny plik) z przyciskiem odtwarzania."""
    BROWSE = "__browse__"

    def __init__(self, value):
        super().__init__()
        self.combo = QComboBox()
        self.combo.setMinimumWidth(220)
        self.combo.addItem("Brak", "")
        try:
            names = sorted(n for n in os.listdir(MEDIA_DIR) if n.lower().endswith(".wav"))
        except OSError:
            names = []
        for n in names:
            self.combo.addItem(n[:-4], n)
        if value and self.combo.findData(value) < 0:
            self.combo.addItem(os.path.basename(value), value)
        self.combo.addItem("Wybierz plik .wav…", self.BROWSE)
        self.combo.setCurrentIndex(max(self.combo.findData(value), 0))
        self._last = self.combo.currentIndex()
        self.combo.currentIndexChanged.connect(self._picked)

        play = QToolButton(text="▶")
        play.setToolTip("Odtwórz")
        play.clicked.connect(lambda: play_sound(self.value()))

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.combo, 1)
        h.addWidget(play)

    def _picked(self, idx):
        if self.combo.itemData(idx) != self.BROWSE:
            self._last = idx
            return
        path, _ = QFileDialog.getOpenFileName(self, "Wybierz dźwięk", MEDIA_DIR, "Dźwięki (*.wav)")
        self.combo.blockSignals(True)
        if path:
            path = os.path.normpath(path)
            at = self.combo.count() - 1
            self.combo.insertItem(at, os.path.basename(path), path)
            self.combo.setCurrentIndex(at)
            self._last = at
            play_sound(path)
        else:
            self.combo.setCurrentIndex(self._last)
        self.combo.blockSignals(False)

    def value(self):
        v = self.combo.currentData()
        return "" if v == self.BROWSE else v


class SettingsDialog(QDialog):
    def __init__(self, cfg, on_preview=None):
        super().__init__(None, Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("AI Status Widget — ustawienia")
        self.setMinimumWidth(540)
        self._on_preview = on_preview
        self._loading = True

        # --- Zachowanie
        self.mode = QComboBox()
        self.mode.addItem("Zawsze na wierzchu", "top")
        self.mode.addItem("Pod oknami, wyskakuje przy zmianie", "under")
        self.pop_rule = QComboBox()
        self.pop_rule.addItem("Przy każdej zmianie koloru", "any")
        self.pop_rule.addItem("Tylko gdy ktoś czeka (pomarańczowe)", "waiting")
        self.pop_seconds = QSpinBox(minimum=0, maximum=600, suffix=" s")
        self.pop_seconds.setSpecialValueText("do kliknięcia")
        self.stale = QSpinBox(minimum=1, maximum=240, suffix=" min")
        self.hotkey = QKeySequenceEdit(QKeySequence(cfg["hotkey"]))
        if hasattr(self.hotkey, "setMaximumSequenceLength"):
            self.hotkey.setMaximumSequenceLength(1)
        if hasattr(self.hotkey, "setClearButtonEnabled"):
            self.hotkey.setClearButtonEnabled(True)
        self.hotkey_lbl = QLabel()
        self.hotkey_lbl.setObjectName("hint")
        self.hotkey.keySequenceChanged.connect(self._check_hotkey)
        self.click_jumps = QCheckBox("Kliknięcie w widget przełącza do czekającego terminala")
        self.question_waiting = QCheckBox("Odpowiedź agenta zakończona pytaniem = czeka na Ciebie (pomarańczowe)")

        # --- Nie przeszkadzać (w zakładce Zachowanie)
        self.dnd_fullscreen = QCheckBox("Gdy aplikacja jest na pełnym ekranie (prezentacja, gra, wideo)")
        self.dnd_hours = QCheckBox("W godzinach")
        self.dnd_from = QTimeEdit(QTime.fromString(cfg["dnd_from"], "HH:mm"), displayFormat="HH:mm")
        self.dnd_to = QTimeEdit(QTime.fromString(cfg["dnd_to"], "HH:mm"), displayFormat="HH:mm")
        self.dnd_hours.toggled.connect(lambda on: (self.dnd_from.setEnabled(on), self.dnd_to.setEnabled(on)))

        # --- Dźwięki
        self.sound_waiting = SoundPicker(cfg["sound_waiting"])
        self.notify_long = QCheckBox("Zagraj dźwięk, gdy agent skończy długie zadanie")
        self.long_minutes = QSpinBox(minimum=1, maximum=240, suffix=" min")
        self.sound_long = SoundPicker(cfg["sound_long_task"])
        self.idle_remind = QCheckBox("Zagraj dźwięk, gdy sesja długo czeka na kolejne polecenie")
        self.idle_minutes = QSpinBox(minimum=1, maximum=480, suffix=" min")
        self.sound_idle = SoundPicker(cfg["sound_idle"])
        self.notify_long.toggled.connect(lambda on: (self.long_minutes.setEnabled(on), self.sound_long.setEnabled(on)))
        self.idle_remind.toggled.connect(lambda on: (self.idle_minutes.setEnabled(on), self.sound_idle.setEnabled(on)))

        # --- Wygląd
        self.preset = QComboBox()
        for name, housing, rim in STYLE_PRESETS:
            self.preset.addItem(name, (housing, rim))
        self.preset.addItem("Własny", None)
        self.housing = QComboBox()
        self.housing.addItem("Ciemna", "dark")
        self.housing.addItem("Jasna", "light")
        self.housing.addItem("Szklana (półprzezroczysta)", "glass")
        self.housing.addItem("Automatyczna (dopasuj do tła)", "auto")
        self.state_rim = QCheckBox("Obwódka i poświata w kolorze aktualnego stanu")
        self.glow = QSlider(Qt.Horizontal, minimum=0, maximum=100)
        self.glow_lbl = QLabel()
        self.opacity = QSlider(Qt.Horizontal, minimum=30, maximum=100)
        self.opacity_lbl = QLabel()
        self.scale = QComboBox()
        for v in (1.0, 1.25, 1.5, 2.0, 2.5):
            self.scale.addItem(f"{int(v * 100)}%", v)
        self.orientation = QComboBox()
        self.orientation.addItem("Pionowy", "vertical")
        self.orientation.addItem("Poziomy", "horizontal")
        self.orientation.addItem("Jedno światło (zmienia kolor)", "single")
        self.orientation.addItem("Matrix (kwadratowy ekran)", "matrix")
        self.orientation.addItem("Korektor (słupki)", "eq")
        self.show_dots = QCheckBox("Pokazuj kropki sesji pod widgetem (poza układem Matrix)")

        # --- System
        self.autostart = QCheckBox("Uruchamiaj razem z Windows")
        self.hooks_lbl = QLabel()
        self.hooks_btn = QPushButton()
        self.hooks_btn.clicked.connect(self.toggle_hooks)
        self._refresh_hooks()
        open_dir = QPushButton("Otwórz folder stanu")
        open_dir.clicked.connect(lambda: (os.makedirs(SESSIONS_DIR, exist_ok=True), os.startfile(APP_DIR)))

        # --- wartości początkowe
        for combo, key in ((self.mode, "mode"), (self.pop_rule, "pop_rule"), (self.scale, "scale"),
                           (self.orientation, "orientation"), (self.housing, "housing")):
            combo.setCurrentIndex(max(combo.findData(cfg[key]), 0))
        self.pop_seconds.setValue(int(cfg["pop_seconds"]))
        self.stale.setValue(int(cfg["stale_minutes"]))
        self.click_jumps.setChecked(cfg["click_jumps"])
        self.question_waiting.setChecked(cfg["question_waiting"])
        self.dnd_fullscreen.setChecked(cfg["dnd_fullscreen"])
        self.dnd_hours.setChecked(cfg["dnd_hours"])
        self.dnd_from.setEnabled(cfg["dnd_hours"])
        self.dnd_to.setEnabled(cfg["dnd_hours"])
        self.notify_long.setChecked(cfg["notify_long_task"])
        self.long_minutes.setValue(int(cfg["long_task_minutes"]))
        self.long_minutes.setEnabled(cfg["notify_long_task"])
        self.sound_long.setEnabled(cfg["notify_long_task"])
        self.idle_remind.setChecked(cfg["idle_remind"])
        self.idle_minutes.setValue(int(cfg["idle_remind_minutes"]))
        self.idle_minutes.setEnabled(cfg["idle_remind"])
        self.sound_idle.setEnabled(cfg["idle_remind"])
        self.state_rim.setChecked(cfg["state_rim"])
        self.glow.setValue(int(cfg["glow"] * 100))
        self.opacity.setValue(int(cfg["opacity"] * 100))
        self.show_dots.setChecked(cfg["show_dots"])
        self.autostart.setChecked(cfg["autostart"])
        self._check_hotkey()

        # --- zakładki
        hours = QWidget()
        hh = QHBoxLayout(hours)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.addWidget(self.dnd_hours)
        hh.addWidget(self.dnd_from)
        hh.addWidget(QLabel("–"))
        hh.addWidget(self.dnd_to)
        hh.addStretch(1)
        hotkey_box = QWidget()
        hk_v = QVBoxLayout(hotkey_box)
        hk_v.setContentsMargins(0, 0, 0, 0)
        hk_v.setSpacing(3)
        hk_v.addWidget(self.hotkey)
        hk_v.addWidget(self.hotkey_lbl)

        tabs = QTabWidget()
        tabs.addTab(self._page([
            ("Warstwa", self.mode),
            ("Wyskakuj", self.pop_rule),
            ("Na wierzchu przez", self.pop_seconds),
            ("Zawieszona sesja po", self.stale),
            ("", self.question_waiting),
            None,
            ("Skrót do agenta", hotkey_box),
            ("", self.click_jumps),
            None,
            "Nie przeszkadzać",
            ("", self.dnd_fullscreen),
            ("", hours),
        ], "W trybie „Nie przeszkadzać” widget nadal zmienia kolor, ale nie wyskakuje na wierzch, "
           "nie gra dźwięków. Można go też włączyć ręcznie w menu pod prawym przyciskiem."),
            "Zachowanie")
        tabs.addTab(self._page([
            "Ktoś czeka na odpowiedź",
            ("Dźwięk", self.sound_waiting),
            None,
            "Koniec długiego zadania",
            ("", self.notify_long),
            ("Zadanie trwające od", self.long_minutes),
            ("Dźwięk", self.sound_long),
            None,
            "Bezczynna sesja",
            ("", self.idle_remind),
            ("Bezczynna od", self.idle_minutes),
            ("Dźwięk", self.sound_idle),
        ], "Tylko dźwięki – widget nie pokazuje dymków Windows. Przycisk ▶ odtwarza wybrany dźwięk. "
           "W trybie „Nie przeszkadzać” dźwięki są wyciszone."), "Dźwięki")
        tabs.addTab(self._page([
            ("Gotowy styl", self.preset),
            None,
            ("Obudowa", self.housing),
            ("", self.state_rim),
            ("Siła poświaty", self._with_label(self.glow, self.glow_lbl)),
            ("Krycie tła", self._with_label(self.opacity, self.opacity_lbl)),
            None,
            ("Rozmiar", self.scale),
            ("Układ", self.orientation),
            ("", self.show_dots),
        ], "Zmiany widać od razu na widgecie, a Anuluj przywraca poprzedni wygląd. "
           "Obudowa automatyczna co 1,5 s sprawdza jasność tła i wybiera jasną albo ciemną kapsułę."), "Wygląd")
        hk_w = QWidget()
        hk = QHBoxLayout(hk_w)
        hk.setContentsMargins(0, 0, 0, 0)
        hk.addWidget(self.hooks_lbl, 1)
        hk.addWidget(self.hooks_btn)
        tabs.addTab(self._page([
            ("", self.autostart),
            ("Hooki Claude Code", hk_w),
            ("", open_dir),
        ], "Hooki działają w sesjach Claude Code uruchomionych po instalacji."), "System")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Zapisz")
        buttons.button(QDialogButtonBox.Cancel).setText("Anuluj")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.addWidget(tabs)
        lay.addSpacing(6)
        lay.addWidget(buttons)

        # --- sygnały
        self.mode.currentIndexChanged.connect(self._sync_enabled)
        self.preset.currentIndexChanged.connect(self._apply_preset)
        for w in (self.housing, self.scale, self.orientation):
            w.currentIndexChanged.connect(self._changed)
        for w in (self.state_rim, self.show_dots):
            w.toggled.connect(self._changed)
        for w in (self.glow, self.opacity):
            w.valueChanged.connect(self._changed)
        self._loading = False
        self._changed()
        self._sync_enabled()

    @staticmethod
    def _with_label(slider, label):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(slider, 1)
        label.setMinimumWidth(36)
        h.addWidget(label)
        return w

    @staticmethod
    def _page(rows, hint_text):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(14, 12, 14, 12)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(9)
        for row in rows:
            if row is None:
                line = QWidget()
                line.setFixedHeight(1)
                line.setObjectName("rule")
                line.setAttribute(Qt.WA_StyledBackground)
                form.addRow(line)
            elif isinstance(row, str):
                lbl = QLabel(row.upper())
                lbl.setObjectName("section")
                form.addRow(lbl)
            else:
                form.addRow(row[0], row[1])
        v.addLayout(form)
        v.addStretch(1)
        hint = QLabel(hint_text)
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        v.addWidget(hint)
        return page

    def _sync_enabled(self):
        under = self.mode.currentData() == "under"
        self.pop_rule.setEnabled(under)
        self.pop_seconds.setEnabled(under)

    def _check_hotkey(self):
        text = self.hotkey.keySequence().toString()
        if not text:
            self.hotkey_lbl.setText("Wyłączony. Kliknij pole i naciśnij kombinację klawiszy.")
        elif parse_hotkey(text) is None:
            self.hotkey_lbl.setText("Nieobsługiwany klawisz: użyj litery, cyfry, F1–F24 albo spacji z Ctrl/Alt/Shift/Win.")
        else:
            self.hotkey_lbl.setText("Przełącza do najdłużej czekającego agenta, a gdy nikt nie czeka, do ostatniej sesji.")

    def _apply_preset(self):
        data = self.preset.currentData()
        if self._loading or data is None:
            return
        housing, rim = data
        self._loading = True
        self.housing.setCurrentIndex(self.housing.findData(housing))
        self.state_rim.setChecked(rim)
        self._loading = False
        self._changed()

    def _changed(self):
        if self._loading:
            return
        self.glow_lbl.setText(f"{self.glow.value()}%")
        self.opacity_lbl.setText(f"{self.opacity.value()}%")
        self.glow.setEnabled(self.state_rim.isChecked())
        # „Gotowy styl” pokazuje preset pasujący do bieżących ustawień albo „Własny”
        current = (self.housing.currentData(), self.state_rim.isChecked())
        idx = next((i for i in range(self.preset.count()) if self.preset.itemData(i) == current), self.preset.count() - 1)
        self._loading = True
        self.preset.setCurrentIndex(idx)
        self._loading = False
        if self._on_preview:
            self._on_preview(self.values())

    def _refresh_hooks(self):
        on = hooks_installed()
        self.hooks_lbl.setText("zainstalowane" if on else "niezainstalowane")
        self.hooks_btn.setText("Usuń" if on else "Zainstaluj")

    def toggle_hooks(self):
        sys_argv = sys.argv
        sys.argv = [sys_argv[0]] + (["--uninstall"] if hooks_installed() else [])
        try:
            install_hooks.main()
        finally:
            sys.argv = sys_argv
        self._refresh_hooks()

    def values(self):
        hotkey = self.hotkey.keySequence().toString()
        return {
            "mode": self.mode.currentData(),
            "pop_rule": self.pop_rule.currentData(),
            "pop_seconds": self.pop_seconds.value(),
            "stale_minutes": self.stale.value(),
            "hotkey": hotkey if parse_hotkey(hotkey) else "",
            "click_jumps": self.click_jumps.isChecked(),
            "question_waiting": self.question_waiting.isChecked(),
            "dnd_fullscreen": self.dnd_fullscreen.isChecked(),
            "dnd_hours": self.dnd_hours.isChecked(),
            "dnd_from": self.dnd_from.time().toString("HH:mm"),
            "dnd_to": self.dnd_to.time().toString("HH:mm"),
            "sound_waiting": self.sound_waiting.value(),
            "notify_long_task": self.notify_long.isChecked(),
            "long_task_minutes": self.long_minutes.value(),
            "sound_long_task": self.sound_long.value(),
            "idle_remind": self.idle_remind.isChecked(),
            "idle_remind_minutes": self.idle_minutes.value(),
            "sound_idle": self.sound_idle.value(),
            "housing": self.housing.currentData(),
            "state_rim": self.state_rim.isChecked(),
            "glow": self.glow.value() / 100,
            "opacity": self.opacity.value() / 100,
            "scale": self.scale.currentData(),
            "orientation": self.orientation.currentData(),
            "show_dots": self.show_dots.isChecked(),
            "autostart": self.autostart.isChecked(),
        }


STYLE = """
* { font-family: "Segoe UI"; font-size: 9.5pt; }
QToolTip { background: #14181D; color: #E6EDF2; border: 1px solid #2E3640; padding: 6px 8px; border-radius: 6px; }
QMenu { background: #171B21; color: #E6EDF2; border: 1px solid #2E3640; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 18px 6px 12px; border-radius: 5px; }
QMenu::item:selected { background: #262D36; }
QMenu::item:disabled { color: #8A96A3; }
QMenu::separator { height: 1px; background: #2E3640; margin: 4px 6px; }
QMenu::indicator { width: 12px; height: 12px; margin-left: 4px; }
QDialog { background: #14181D; color: #E6EDF2; }
QLabel { color: #C9D2DA; }
QLabel#section { color: #7F8B97; font-size: 8pt; letter-spacing: 1px; padding-top: 4px; }
QLabel#hint { color: #7F8B97; font-size: 8.5pt; }
QWidget#rule { background: #262D36; }
QTabWidget::pane { border: 1px solid #262D36; border-radius: 8px; top: -1px; background: #171B21; }
QTabBar::tab { background: transparent; color: #8A96A3; padding: 7px 16px; margin-right: 2px; border: 1px solid transparent; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { color: #E6EDF2; background: #171B21; border-color: #262D36; border-bottom-color: #171B21; }
QTabBar::tab:hover:!selected { color: #C9D2DA; }
QSlider::groove:horizontal:disabled { background: #22282F; }
QSlider::handle:horizontal:disabled { background: #4A535D; }
QComboBox, QSpinBox, QTimeEdit, QLineEdit { background: #1C2128; color: #E6EDF2; border: 1px solid #2E3640; border-radius: 6px; padding: 4px 8px; min-height: 20px; }
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus, QLineEdit:focus { border-color: #4C8DFF; }
QComboBox QAbstractItemView { background: #1C2128; color: #E6EDF2; border: 1px solid #2E3640; selection-background-color: #2A3340; }
QComboBox:disabled, QSpinBox:disabled, QTimeEdit:disabled { color: #5F6B77; }
QCheckBox { color: #E6EDF2; spacing: 8px; }
QPushButton, QToolButton { background: #232A33; color: #E6EDF2; border: 1px solid #333C47; border-radius: 6px; padding: 5px 14px; }
QToolButton { padding: 4px 9px; }
QPushButton:hover, QToolButton:hover { background: #2B333E; }
QToolButton:disabled { color: #5F6B77; }
QPushButton:default { background: #E6EDF2; color: #14181D; border-color: #E6EDF2; }
QSlider::groove:horizontal { height: 4px; background: #2E3640; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; margin: -5px 0; background: #E6EDF2; border-radius: 7px; }
"""


def main():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    lock = QLockFile(os.path.join(APP_DIR, "widget.lock"))
    if not lock.tryLock(100):
        return  # już działa
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)
    w = Light(load_config())
    w.show()
    w.apply_layer()
    w.register_hotkey()
    if w.cfg["mode"] == "under":
        w.pop()  # pokaż się po starcie, potem wróć pod okna
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
