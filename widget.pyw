"""AI Status Matrix: minimalistyczny widget pokazujący stan agentów AI jako ekrany w stylu Matrix.

Każda sesja (terminal) ma własny kwadratowy kafelek:
  bezczynny  - kursor `>_`            pracuje      - deszcz znaków
  czeka      - pomarańczowy `?`       kompaktuje   - niebieski skaner (KITT)
  błąd       - glitch `ERR`           brak sesji   - szary szum

Stan sesji zapisuje hook.py do %LOCALAPPDATA%\\ai-status-matrix\\sessions\\*.json.
Widget czyta te pliki, dodatkowo wykrywa przerwania (Esc) i zamknięte sesje,
przełącza do terminala agenta i gra dźwięki (bez dymków Windows). Uruchamiaj przez pythonw.
"""
import functools
import json
import math
import os
import random
import shutil
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QEvent, QLocale, QLockFile, QPoint, QPointF, QRectF, Qt, QTime, QTimer
from PySide6.QtGui import (
    QAction, QActionGroup, QBrush, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QKeySequenceEdit, QLabel, QMenu, QPushButton, QSlider, QSpinBox, QSystemTrayIcon, QTabWidget, QTimeEdit,
    QToolButton, QToolTip, QVBoxLayout, QWidget,
)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hook  # noqa: E402
import install_hooks  # noqa: E402
import winapi  # noqa: E402

LOCAL = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
APP_DIR = os.path.join(LOCAL, "ai-status-matrix")
LEGACY_APP_DIR = os.path.join(LOCAL, "ai-traffic-light")  # folder stanu sprzed zmiany nazwy projektu
SESSIONS_DIR = os.path.join(APP_DIR, "sessions")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
MEDIA_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media")

IDLE, WORKING, WAITING, STALE, ERROR = "idle", "working", "waiting", "stale", "error"
COMPACTING = "compacting"  # kompaktowanie (streszczanie) kontekstu rozmowy
BACKGROUND = "background"  # agent czeka na Ciebie, ale w tle działa jego zadanie (komenda, pomocniczy agent)
OFFLINE = "offline"  # tylko stan zbiorczy: brak jakiejkolwiek sesji
BUSY = (WORKING, WAITING)
LABEL = {IDLE: "bezczynny", WORKING: "pracuje", WAITING: "czeka na Ciebie", STALE: "zawieszona", ERROR: "błąd",
         COMPACTING: "kompaktuje kontekst", BACKGROUND: "zadanie w tle"}
CLI_NAME = {"claude": "Claude Code", "gemini": "Gemini CLI", "codex": "Codex CLI"}

GLITCH = QColor(220, 70, 255)  # błąd tury / zawieszona sesja
# praca w przygaszonej, butelkowej zieleni „Matrixa”, bezczynność jako zimny, biało-turkusowy terminal
COLOR = {WORKING: QColor(27, 196, 106), IDLE: QColor(159, 232, 224), WAITING: QColor(255, 162, 31),
         COMPACTING: QColor(64, 156, 255), ERROR: GLITCH, STALE: GLITCH, OFFLINE: QColor(120, 128, 138)}
COLOR[BACKGROUND] = COLOR[IDLE]  # jak bezczynny: agent czeka na Ciebie
BACKGROUND_DOTS = QColor(255, 75, 58)  # czerwone kropki „zadanie w tle trwa”
KNOWN_STATES = (IDLE, WORKING, WAITING, ERROR, COMPACTING)  # stany, które może zapisać hook

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
    "size": 1.0,              # rozmiar względem bazowego (100% = BASE_SCALE)
    "opacity": 0.85,
    "housing": "dark",        # "dark" | "light" | "glass" | "auto"
    "state_rim": True,        # obwódka i poświata w kolorze stanu
    "glow": 0.6,              # siła poświaty 0..1
    "show_names": True,       # nazwa projektu na listwie u dołu kafelka
    # system
    "language": "auto",       # "auto" | "pl" | "en"
    "autostart": False,
    "pos": None,
}

# obudowa kafelka: (tło rgb, bazowa alfa tła, obwódka rgba)
HOUSING = {
    "dark":  ((16, 19, 23), 255, (255, 255, 255, 26)),
    "light": ((246, 247, 249), 255, (0, 0, 0, 36)),
    "glass": ((255, 255, 255), 70, (255, 255, 255, 150)),
}
# ustawienia usuniętych układów (sygnalizator, korektor…) i dawna bezwzględna skala zastąpiona przez "size"
OBSOLETE_KEYS = ("orientation", "show_dots", "scale")
BASE_SCALE = 1.75  # rozmiar 100%: kafelek 60 px
SIZES = (0.75, 0.85, 1.0, 1.15, 1.3, 1.5)

HOTKEY_ID = 0xA11
RADIUS = 2  # zaokrąglenie kafelków


def light_color(color, k):
    """Kolor rozjaśniony w stronę bieli (k = 0..1)."""
    return QColor.fromRgbF(color.redF() + (1 - color.redF()) * k, color.greenF() + (1 - color.greenF()) * k,
                           color.blueF() + (1 - color.blueF()) * k, color.alphaF())


# ---------------------------------------------------------------- język
# Teksty interfejsu pisane są po polsku; T() zwraca angielskie tłumaczenie, gdy wybrano angielski.
EN = {
    # stany i podpisy
    "bezczynny": "idle",
    "pracuje": "working",
    "czeka na Ciebie": "waiting for you",
    "zawieszona": "stalled",
    "błąd": "error",
    "kompaktuje kontekst": "compacting context",
    "zadanie w tle": "background task",
    "{n} z zadaniem w tle": "{n} with a background task",
    "w tle: {label} ({time})": "background: {label} ({time})",
    "{n} czeka na Ciebie": "{n} waiting for you",
    "{n} z błędem lub zawieszona": "{n} with error or stalled",
    "{n} kompaktuje kontekst": "{n} compacting context",
    "{n} pracuje": "{n} working",
    "wszyscy wolni": "all idle",
    "brak aktywnych sesji": "no active sessions",
    "Brak aktywnych sesji": "No active sessions",
    "Nie przeszkadzać: włączone": "Do not disturb: on",
    # szczegóły zapisywane przez hook i widget
    "przerwane przez użytkownika": "interrupted by user",
    "kompaktowanie kontekstu": "compacting context",
    "kompaktowanie kontekstu (auto)": "compacting context (auto)",
    # menu
    "Przejdź do czekającego agenta": "Go to waiting agent",
    "Przejdź do ostatniej sesji": "Go to latest session",
    "Sesje ({n})": "Sessions ({n})",
    "Wyczyść listę sesji": "Clear session list",
    "Zawsze na wierzchu": "Always on top",
    "Pod oknami, wyskakuj przy zmianie": "Below windows, pop up on change",
    "Nie przeszkadzać": "Do not disturb",
    "Ustawienia…": "Settings…",
    "Zamknij": "Quit",
    # gotowe style
    "Kolorowa obwódka (domyślny)": "Colored rim (default)",
    "Ciemna, bez obwódki": "Dark, no rim",
    "Jasna": "Light",
    "Szklana": "Glass",
    "Jasna z kolorową obwódką": "Light with colored rim",
    "Automatyczny kontrast": "Automatic contrast",
    "Automatyczny kontrast z obwódką": "Automatic contrast with rim",
    # wybór dźwięku
    "Brak": "None",
    "Wybierz plik .wav…": "Choose .wav file…",
    "Odtwórz": "Play",
    "Wybierz dźwięk": "Choose sound",
    "Dźwięki (*.wav)": "Sounds (*.wav)",
    # ustawienia
    "AI Status Matrix — ustawienia": "AI Status Matrix — settings",
    "Pod oknami, wyskakuje przy zmianie": "Below windows, pops up on change",
    "Przy każdej zmianie stanu": "On every state change",
    "Tylko gdy ktoś czeka (pomarańczowe)": "Only when someone is waiting (orange)",
    "do kliknięcia": "until clicked",
    "Kliknięcie w widget przełącza do czekającego terminala": "Clicking the widget switches to the waiting terminal",
    "Odpowiedź agenta zakończona pytaniem = czeka na Ciebie (pomarańczowe)":
        "Agent reply ending with a question = waiting for you (orange)",
    "Gdy aplikacja jest na pełnym ekranie (prezentacja, gra, wideo)":
        "When an app is full screen (presentation, game, video)",
    "W godzinach": "During hours",
    "Zagraj dźwięk, gdy agent skończy długie zadanie": "Play a sound when the agent finishes a long task",
    "Zagraj dźwięk, gdy sesja długo czeka na kolejne polecenie":
        "Play a sound when a session waits long for the next prompt",
    "Własny": "Custom",
    "Ciemna": "Dark",
    "Szklana (półprzezroczysta)": "Glass (translucent)",
    "Automatyczna (dopasuj do tła)": "Automatic (match background)",
    "Obwódka i poświata w kolorze aktualnego stanu": "Rim and glow in the current state's color",
    "Nazwa projektu (terminala) na dole kafelka": "Project (terminal) name at the bottom of the tile",
    "Automatycznie (język systemu)": "Automatic (system language)",
    "Uruchamiaj razem z Windows": "Start with Windows",
    "Otwórz folder stanu": "Open state folder",
    "Warstwa": "Layer",
    "Wyskakuj": "Pop up",
    "Na wierzchu przez": "Stay on top for",
    "Zawieszona sesja po": "Session stalled after",
    "Skrót do agenta": "Agent shortcut",
    "W trybie „Nie przeszkadzać” widget nadal zmienia kolor, ale nie wyskakuje na wierzch, "
    "nie gra dźwięków. Można go też włączyć ręcznie w menu pod prawym przyciskiem.":
        "In “Do not disturb” mode the widget still changes color, but it does not pop up "
        "or play sounds. You can also turn it on manually from the right-click menu.",
    "Zachowanie": "Behavior",
    "Ktoś czeka na odpowiedź": "Someone is waiting for a reply",
    "Dźwięk": "Sound",
    "Koniec długiego zadania": "Long task finished",
    "Zadanie trwające od": "Task lasting at least",
    "Bezczynna sesja": "Idle session",
    "Bezczynna od": "Idle for",
    "Tylko dźwięki – widget nie pokazuje dymków Windows. Przycisk ▶ odtwarza wybrany dźwięk. "
    "W trybie „Nie przeszkadzać” dźwięki są wyciszone.":
        "Sounds only – the widget shows no Windows notifications. The ▶ button plays the selected sound. "
        "Sounds are muted in “Do not disturb” mode.",
    "Dźwięki": "Sounds",
    "Gotowy styl": "Preset",
    "Obudowa": "Housing",
    "Siła poświaty": "Glow strength",
    "Krycie tła": "Background opacity",
    "Rozmiar": "Size",
    "Zmiany widać od razu na widgecie, a Anuluj przywraca poprzedni wygląd. "
    "Obudowa automatyczna co 1,5 s sprawdza jasność tła i wybiera jasne albo ciemne kafelki.":
        "Changes show on the widget right away; Cancel restores the previous look. "
        "Automatic housing checks the background brightness every 1.5 s and picks light or dark tiles.",
    "Wygląd": "Appearance",
    "Język / Language": "Language / Język",
    "Hooki Claude Code": "Claude Code hooks",
    "Hooki działają w sesjach Claude Code uruchomionych po instalacji. Zmiana języka działa po zapisaniu.":
        "Hooks work in Claude Code sessions started after installation. Language changes apply after saving.",
    "Zapisz": "Save",
    "Anuluj": "Cancel",
    "Wyłączony. Kliknij pole i naciśnij kombinację klawiszy.": "Off. Click the field and press a key combination.",
    "Nieobsługiwany klawisz: użyj litery, cyfry, F1–F24 albo spacji z Ctrl/Alt/Shift/Win.":
        "Unsupported key: use a letter, digit, F1–F24 or Space with Ctrl/Alt/Shift/Win.",
    "Przełącza do najdłużej czekającego agenta, a gdy nikt nie czeka, do ostatniej sesji.":
        "Switches to the agent waiting longest, or to the latest session when nobody is waiting.",
    "zainstalowane": "installed",
    "niezainstalowane": "not installed",
    "Usuń": "Remove",
    "Zainstaluj": "Install",
}
LANG = "pl"


def T(text):
    """Tekst interfejsu w wybranym języku (polski jest językiem źródłowym)."""
    return EN.get(text, text) if LANG == "en" else text


def set_language(cfg):
    """"auto" = język systemu: polski dla polskiego Windows, w pozostałych przypadkach angielski."""
    global LANG
    lang = cfg.get("language", "auto")
    if lang not in ("pl", "en"):
        lang = "pl" if QLocale.system().language() == QLocale.Polish else "en"
    LANG = lang


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
    cfg.update({k: v for k, v in stored.items() if k not in OBSOLETE_KEYS})
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


_parsed = {}  # ścieżka -> ((mtime_ns, rozmiar), rekord): plik czytamy ponownie tylko, gdy się zmienił


def _read_session(entry):
    st = entry.stat()
    key = (st.st_mtime_ns, st.st_size)
    cached = _parsed.get(entry.path)
    if cached and cached[0] == key:
        return dict(cached[1])
    with open(entry.path, encoding="utf-8") as f:
        rec = json.load(f)
    if not isinstance(rec, dict):
        raise ValueError(entry.path)
    _parsed[entry.path] = (key, rec)
    return dict(rec)


def load_sessions(cfg):
    """Sesje posortowane od najstarszej – w tej kolejności („jak w książce”) stoją kafelki."""
    out, now = [], time.time()
    try:
        entries = list(os.scandir(SESSIONS_DIR))
    except OSError:
        return out
    for entry in entries:
        name, path = entry.name, entry.path
        if name.endswith(".tmp"):
            try:
                if now - entry.stat().st_mtime > 60:
                    os.remove(path)  # osierocony plik po nieudanym zapisie
            except OSError:
                pass
            continue
        if not name.endswith(".json"):
            continue
        try:
            s = _read_session(entry)
        except (OSError, ValueError):
            continue
        if s.get("state") not in KNOWN_STATES:
            continue
        age = now - float(s.get("ts", 0))
        if age > cfg["forget_hours"] * 3600:
            _remove(path)
            continue
        s["_raw_state"] = s.get("state")
        if s.get("state") == IDLE and s.get("question") and cfg["question_waiting"]:
            s["state"] = WAITING  # agent skończył odpowiedź pytaniem i czeka na Ciebie
        elif s.get("state") == IDLE and s.get("background"):
            s["state"] = BACKGROUND  # agent skończył turę, ale jego zadanie w tle jeszcze trwa
        # "brak sygnału" tylko dla pracy bez zdarzeń; czekanie na Ciebie może trwać dowolnie długo
        if s.get("state") == WORKING and age > cfg["stale_minutes"] * 60:
            s["state"] = STALE
        s["_path"] = path
        out.append(s)
    for gone in _parsed.keys() - {s["_path"] for s in out}:
        del _parsed[gone]
    out.sort(key=lambda s: (float(s.get("started") or s.get("ts") or 0), s.get("session_id", "")))
    return out


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def aggregate(sessions):
    """Stan zbiorczy: czekanie > błąd albo zawieszenie > kompaktowanie > praca > zadanie w tle > bezczynność;
    bez sesji – offline."""
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
    if BACKGROUND in states:
        return BACKGROUND
    return IDLE


def transcript_status(path, since_ts):
    """("interrupted", "") gdy tura przerwana klawiszem Esc, ("error", tekst) gdy skończyła się błędem API,
    inaczej (None, "")."""
    try:
        if os.stat(path).st_mtime < since_ts - 1:
            return None, ""  # od ostatniego zdarzenia nic nie dopisano
    except OSError:
        return None, ""
    for obj in hook.tail_entries(path):
        kind = obj.get("type")
        if kind == "assistant":
            error = hook.error_text(obj)
            return ("error", error) if error else (None, "")
        if kind == "user":
            interrupted = "[Request interrupted by user" in hook._text_of((obj.get("message") or {}).get("content"))
            return ("interrupted", "") if interrupted else (None, "")
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
RUN_NAME = "AIStatusMatrix"
LEGACY_RUN_NAME = "AITrafficLight"  # sprzed zmiany nazwy


def set_autostart(enabled):
    """Wpis w rejestrze (Run) – przy każdym zapisie z aktualną ścieżką, więc nadąża za przeniesionym folderem."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        for name in (LEGACY_RUN_NAME,) + (() if enabled else (RUN_NAME,)):
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass
        if enabled:
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{exe}" "{os.path.abspath(__file__)}"')


def migrate_app_dir():
    """Przenosi konfigurację i sesje ze starego folderu stanu (ai-traffic-light). Pliki już istniejące w nowym
    folderze wygrywają – mógł je założyć hook nowej wersji, zanim widget wystartował."""
    if not os.path.isdir(LEGACY_APP_DIR):
        return
    for root, _dirs, files in os.walk(LEGACY_APP_DIR):
        target_dir = os.path.join(APP_DIR, os.path.relpath(root, LEGACY_APP_DIR))
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            target = os.path.join(target_dir, name)
            if name != "widget.lock" and not os.path.exists(target):
                try:
                    os.replace(os.path.join(root, name), target)
                except OSError:
                    pass
    shutil.rmtree(LEGACY_APP_DIR, ignore_errors=True)


def hooks_installed():
    return install_hooks.installed()


def in_quiet_hours(cfg):
    now = QTime.currentTime()
    a, b = QTime.fromString(cfg["dnd_from"], "HH:mm"), QTime.fromString(cfg["dnd_to"], "HH:mm")
    if not a.isValid() or not b.isValid() or a == b:
        return False
    return a <= now < b if a < b else (now >= a or now < b)


# ---------------------------------------------------------------- the widget
@functools.lru_cache(maxsize=96)
def halo_pixmap(tile, s, rgb, pulse, glow, dpr):
    """(obrazek, margines): cień i poświata jednego kafelka. Poświata ma warstwy co ~2,5 px niezależnie
    od rozmiaru, więc przy dużym widgecie nie rozpada się na pasy."""
    reach = 14 * s
    pad = math.ceil(reach) + 5
    size = tile + 2 * pad
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    rect = QRectF(pad, pad, tile, tile)
    for i, a in enumerate((18, 12, 7)):
        p.setBrush(QColor(0, 0, 0, a))
        p.drawRoundedRect(rect.adjusted(-i - 1, -i + 2, i + 1, i + 4), RADIUS + i, RADIUS + i)
    if glow > 0:
        layers = max(7, round(reach / 2.5))
        c = QColor.fromRgb(rgb)
        for i in range(layers, 0, -1):
            grow = i * reach / layers
            c.setAlphaF(min(1.0, 0.16 * 7 / layers * glow * pulse * (1 - (i - 1) / layers)))
            p.setBrush(c)
            p.drawRoundedRect(rect.adjusted(-grow, -grow, grow, grow), RADIUS + grow, RADIUS + grow)
    p.end()
    return pm, pad



class Light(QWidget):
    def __init__(self, cfg):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_AlwaysShowToolTips)
        self.setWindowTitle("AI Status Matrix")

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
        self._icon_key = None        # (stan, nie przeszkadzać) narysowane na ikonie w zasobniku
        self._look = None            # [(session_id, stan)] z ostatniego przerysowania

        self.poll = QTimer(self, interval=300, timeout=self.refresh)
        self.anim = QTimer(self, timeout=self._tick)
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

    # ---- geometry
    def dims(self):
        """Siatka kafelków: jeden na sesję, rośnie 1, 2×1, 2×2, 3×2, 3×3…"""
        s = self.cfg["size"] * BASE_SCALE
        # pełne piksele: krawędzie kafelków nie wypadają na połówkach pikseli, więc nie są rozmyte
        tile, gap, margin = round(34 * s), max(2, round(3 * s)), round(16 * s)
        n = max(1, len(self.sessions))
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        return dict(s=s, m=margin, tile=tile, gap=gap, cols=cols,
                    cw=cols * tile + (cols - 1) * gap, ch=rows * tile + (rows - 1) * gap)

    def tiles(self, g=None):
        """[(prostokąt, sesja albo None)] w kolejności „jak w książce”: od najstarszej sesji, od lewej do prawej,
        potem kolejny rząd – nowy terminal zawsze dostaje następne wolne miejsce. Bez sesji jeden pusty kafelek."""
        g = g or self.dims()
        step = g["tile"] + g["gap"]
        out = []
        for i, sess in enumerate(self.sessions or [None]):
            row, col = divmod(i, g["cols"])
            out.append((QRectF(g["m"] + col * step, g["m"] + row * step, g["tile"], g["tile"]), sess))
        return out

    def tile_at(self, pos):
        return next((sess for rect, sess in self.tiles() if sess and rect.contains(pos)), None)

    def relayout(self):
        g = self.dims()
        w, h = math.ceil(g["cw"] + 2 * g["m"]), math.ceil(g["ch"] + 2 * g["m"])
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
        return QRectF(g["m"], g["m"], g["cw"], g["ch"])

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
        if len(sessions) != prev_n:
            self.relayout()

        new = aggregate(sessions)
        changed = new != self.state
        self.state = new
        self.setToolTip(self.tooltip_text())
        self.tray.setToolTip("AI Status Matrix: " + self.caption())
        icon_key = (new, self.cfg["dnd_manual"])
        if icon_key != self._icon_key:  # podmiana ikony w zasobniku jest kosztowna – tylko przy zmianie
            self._icon_key = icon_key
            self.tray.setIcon(self.tray_icon())

        events = self.session_events(sessions, now, initial)
        if changed and not initial:
            self.on_change(new)
        if events and not initial:
            self.notify(events)
        look = [(s.get("session_id"), s["state"]) for s in sessions]
        if look != self._look:  # bez animacji przerysuj tylko, gdy coś się zmieniło
            self._look = look
            self.update()
        self._sync_anim()

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
                elif st in (IDLE, BACKGROUND) and before in BUSY and s.get("event") != "Interrupted":
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
        self._reminded = {key for key in self._reminded if key[0] in self._prev}  # bez zamkniętych sesji
        return events

    def caption(self):
        n_wait = sum(s["state"] == WAITING for s in self.sessions)
        n_err = sum(s["state"] in (ERROR, STALE) for s in self.sessions)
        n_work = sum(s["state"] == WORKING for s in self.sessions)
        n_compact = sum(s["state"] == COMPACTING for s in self.sessions)
        n_bg = sum(s["state"] == BACKGROUND for s in self.sessions)
        if n_wait:
            return T("{n} czeka na Ciebie").format(n=n_wait)
        if n_err:
            return T("{n} z błędem lub zawieszona").format(n=n_err)
        if n_compact:
            return T("{n} kompaktuje kontekst").format(n=n_compact)
        if n_work:
            return T("{n} pracuje").format(n=n_work)
        if n_bg:
            return T("{n} z zadaniem w tle").format(n=n_bg)
        return T("wszyscy wolni") if self.sessions else T("brak aktywnych sesji")

    def session_line(self, s):
        cli = CLI_NAME.get(s.get("cli"), s.get("cli", ""))
        line = f"{s.get('project', '?')} · {cli} — {T(LABEL[s['state']])}"
        if s.get("since") and s["state"] != STALE:
            line += f" {fmt_duration(time.time() - float(s['since']))}"
        return line

    def session_text(self, s):
        """Linia sesji, a pod nią szczegół (narzędzie, pytanie, błąd), gdy sesja nie jest bezczynna."""
        line = self.session_line(s)
        if s["state"] == BACKGROUND:
            for task in s.get("background") or []:
                elapsed = fmt_duration(time.time() - float(task.get("started") or time.time()))
                line += "\n    " + T("w tle: {label} ({time})").format(label=task.get("label") or "?", time=elapsed)
        elif s["state"] != IDLE and s.get("detail"):
            line += f"\n    {T(s['detail'])}"
        return line

    def tooltip_text(self):
        text = "\n".join(map(self.session_text, self.sessions)) or T("Brak aktywnych sesji")
        if self.dnd_active():
            text += "\n\n" + T("Nie przeszkadzać: włączone")
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
    @staticmethod
    def _rng(now, salt=0, fps=14):
        """Generator losowy stały w obrębie jednej klatki glitcha (zmienia się fps razy na sekundę)."""
        return random.Random(int(now * fps) * 7919 + salt)

    def _sync_anim(self):
        """Pełna płynność (30 kl./s), gdy cokolwiek się rusza; same bezczynne kafelki potrzebują tylko
        mrugającego kursora, więc wystarczają 4 klatki na sekundę."""
        fast = self.state != IDLE or time.monotonic() - self._pop_t0 < 1.2
        interval = 33 if fast else 250
        if self.anim.interval() != interval or not self.anim.isActive():
            self.anim.start(interval)

    def _tick(self):
        self.update()
        self._sync_anim()

    # ---- painting
    def paintEvent(self, _):
        g = self.dims()
        tiles = [(rect, self.tile_state(sess)) for rect, sess in self.tiles(g)]
        names = [(sess or {}).get("project", "") if self.cfg["show_names"] else "" for _, sess in self.tiles(g)]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        now = time.monotonic()
        t = now - self._pop_t0
        body = self.body_rect(g)

        # animacja "pop": sprężyste powiększenie
        if 0 <= t < 0.5:
            k = t / 0.5 - 1
            sc = 0.8 + 0.2 * (1 + 2.70158 * k ** 3 + 1.70158 * k ** 2)  # easeOutBack
            p.translate(body.center())
            p.scale(sc, sc)
            p.translate(-body.center())

        # błąd / zawieszenie: co jakiś czas cały widget drga w bok
        if self.state == ERROR and self._rng(now).random() < 0.3:
            p.translate(self._rng(now, 1).uniform(-2, 2) * g["s"], 0)

        breathe = 0.72 + 0.28 * math.cos(now * math.tau / 1.6)
        rim = self.cfg["state_rim"]
        bg_rgb, bg_alpha, border = HOUSING[self.housing()]
        bg = QColor(*bg_rgb, int(bg_alpha * self.cfg["opacity"]))

        # cień i poświata w kolorze stanu: gotowe obrazki z pamięci podręcznej zamiast kilkunastu warstw na klatkę
        pulses = [self._pulse(st, now, breathe) for _, st in tiles]
        glow = self.cfg["glow"] if rim else 0
        dpr = self.devicePixelRatioF()
        for (rect, st), pulse in zip(tiles, pulses):
            pm, pad = halo_pixmap(round(rect.width()), g["s"], COLOR[st].rgb() if glow else 0,
                                  round(pulse * 20) / 20, glow, dpr)
            p.drawPixmap(QPointF(rect.left() - pad, rect.top() - pad), pm)

        # kafelki: obudowa z obwódką w kolorze stanu i ekran Matrix
        for (rect, st), pulse, name in zip(tiles, pulses, names):
            if rim:
                c = QColor(COLOR[st])
                c.setAlphaF(0.55 + 0.35 * pulse)
                p.setPen(QPen(c, 1.5 * g["s"]))
            else:
                p.setPen(QPen(QColor(*border), 1))
            p.setBrush(bg)
            p.drawRoundedRect(rect, RADIUS, RADIUS)
            self._paint_screen(p, rect, g["s"], now, breathe, st, name)

        # poświata przy wyskoczeniu
        if 0 <= t < 1.2:
            k = t / 1.2
            grow = 12 * g["s"] * k
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, int(150 * (1 - k))), 2 * g["s"]))
            p.drawRoundedRect(body.adjusted(-grow, -grow, grow, grow), RADIUS + grow, RADIUS + grow)
        p.end()

    def _pulse(self, st, now, breathe):
        """Jasność obwódki i poświaty: pulsuje przy czekaniu, migocze przy błędzie, przygaszona bez sesji."""
        if st == WAITING:
            return breathe
        if st == OFFLINE:
            return 0.25
        if st == ERROR:
            return 0.4 + 0.6 * self._rng(now, 2).random()
        return 1.0

    @staticmethod
    def tile_state(sess):
        if sess is None:
            return OFFLINE
        return ERROR if sess["state"] == STALE else sess["state"]

    def _paint_screen(self, p, tile, s, now, breathe, st, name=""):
        """Ekran kafelka: kursor `>_` (bezczynny), deszcz znaków (pracuje), glitchujący `?` (czeka),
        skaner KITT (kompaktuje), zakłócony obraz z `ERR` (błąd albo zawieszona sesja), szum (brak sesji).
        Z nazwą projektu ekran jest niższy, a pod nim listwa z nazwą – jak tabliczka na monitorze."""
        col = COLOR[st]
        inset = round(2 * s)
        strip = round(7 * s) if name else 0
        screen = tile.adjusted(inset, inset, -inset, -inset - strip)
        if name:
            p.save()
            p.setFont(self._label_font(4.4 * s))
            c = QColor(col)
            c.setAlphaF(0.9)
            p.setPen(c)
            label = QRectF(tile.left() + inset, screen.bottom(), tile.width() - 2 * inset, strip + inset * 0.5)
            p.drawText(label, Qt.AlignCenter, p.fontMetrics().elidedText(name, Qt.ElideRight, int(label.width())))
            p.restore()
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(screen, RADIUS, RADIUS)
        p.setClipPath(clip)
        if st == OFFLINE:
            self._paint_noise(p, screen, s, now)
        else:
            self._center_glow(p, screen.center(), screen.width() * 0.7, col, 0.22 * (breathe if st == WAITING else 1.0))
        if st == ERROR:
            self._paint_error(p, screen, s, now)
        elif st == COMPACTING:
            self._paint_compacting(p, screen, s, now, col)
        elif st == WORKING:
            self._paint_rain(p, screen, s, now, col)
        elif st == WAITING:
            glitch = (now * 2.3) % 1 < 0.12  # na moment rozszczepienie na cyjan i magentę
            self._paint_glyph(p, screen, s, "?", col, 14 * s, Qt.AlignCenter, glitch)
        elif st in (IDLE, BACKGROUND):  # znak zachęty w lewym dolnym rogu, jak w terminalu
            if st == BACKGROUND:  # „duch” deszczu: przygaszony i wolniejszy – w tle coś się liczy
                p.save()
                p.setOpacity(0.28)
                self._paint_rain(p, screen, s, now * 0.45, COLOR[WORKING])
                p.restore()
            prompt = screen.adjusted(2.5 * s, 2 * s, -2 * s, -1.5 * s)
            self._paint_glyph(p, prompt, s, ">_" if int(now * 2) % 2 else ">", col, 8 * s, Qt.AlignLeft | Qt.AlignBottom)
            if st == BACKGROUND:
                self._paint_busy_dots(p, screen, s, now)
        p.fillRect(screen, self._scanlines(s))  # linie skanowania jak na monitorze kineskopowym
        p.restore()

    @staticmethod
    @functools.lru_cache(maxsize=8)
    def _scanlines(s):
        """Pędzel z wzorem linii skanowania (rysowany raz na skalę zamiast pętli w każdej klatce)."""
        step = max(2, round(1.8 * s))
        img = QImage(1, step, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        for y in range(max(1, round(0.6 * s))):
            img.setPixelColor(0, y, QColor(0, 0, 0, 60))
        return QBrush(img)

    @staticmethod
    def _center_glow(p, c, r, color, alpha):
        grad = QRadialGradient(c, r)
        gc = QColor(color)
        gc.setAlphaF(alpha)
        grad.setColorAt(0, gc)
        gc.setAlphaF(0)
        grad.setColorAt(1, gc)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawEllipse(c, r, r)

    @staticmethod
    def _paint_busy_dots(p, rect, s, now):
        """Trzy czerwone kropki w prawym górnym rogu zapalające się po kolei – zadanie w tle trwa."""
        r = 1.1 * s
        p.setPen(Qt.NoPen)
        for i in range(3):
            c = QColor(BACKGROUND_DOTS)
            c.setAlphaF(1.0 if int(now * 3) % 3 == i else 0.25)
            p.setBrush(c)
            p.drawEllipse(QPointF(rect.right() - 3 * s - (2 - i) * 3.2 * s, rect.top() + 3.5 * s), r, r)

    def _paint_noise(self, p, rect, s, now):
        """Szum jak na wyłączonym telewizorze: szare ziarno i wolno przesuwający się jaśniejszy pas.
        Ziarno to mały obrazek (piksel = ziarno) rozciągnięty na ekran – jedno rysowanie zamiast setek."""
        rnd = self._rng(now, fps=12)
        cell = 1.5 * s
        w, h = max(1, math.ceil(rect.width() / cell)), max(1, math.ceil(rect.height() / cell))
        band = (now * 0.35 % 1) * h
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        for y in range(h):
            near = max(0.0, 1 - abs(y - band) * cell / (5 * s))  # w pasie szum jest jaśniejszy
            for x in range(w):
                v = rnd.random()
                if v > 0.5:
                    level = min(255, int(60 + 110 * v + 60 * near))
                    img.setPixelColor(x, y, QColor(level, level, level, int(70 + 80 * v)))
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        p.drawImage(QRectF(rect.left(), rect.top(), w * cell, h * cell), img)
        p.restore()

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
        self._paint_glyph(p, rect.translated(shift, 0), s, "ERR", GLITCH, 9.5 * s, Qt.AlignCenter, glitch=True)

    RAIN_GLYPHS = "01アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモ"

    @staticmethod
    @functools.lru_cache(maxsize=16)
    def _font(px):
        """Wygładzana czcionka ekranu: łacina z Consolas, katakana z Yu Gothic UI. MS Gothic w małych rozmiarach
        rysuje znaki z gotowych bitmap, więc przy większym widgecie wyglądały jak schodki."""
        font = QFont("Consolas")
        font.setFamilies(["Consolas", "Yu Gothic UI", "MS Gothic"])
        font.setPixelSize(max(6, round(px)))
        font.setBold(True)
        font.setStyleStrategy(QFont.PreferAntialias)
        return font

    @staticmethod
    @functools.lru_cache(maxsize=8)
    def _label_font(px):
        font = QFont("Segoe UI")
        font.setPixelSize(max(7, round(px)))
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
        lead = rect.left() + 2 * s + head(now) * (cw + gap) + cw / 2
        self._center_glow(p, QPointF(lead, rect.center().y()), 9 * s, col, 0.45)
        for i, level in enumerate(levels):
            x = rect.left() + 2 * s + i * (cw + gap)
            q = light_color(col, 0.35 * level) if level > 0.6 else QColor(col)
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
        shades = [light_color(col, 0.75)] + [QColor(col) for _ in range(trail - 1)]
        for j, q in enumerate(shades[1:], 1):
            q.setAlphaF(0.85 - j * 0.13)
        for k in range(cols):
            speed = 2.2 + (k * 0.61) % 1.4  # znaków na sekundę, różne dla kolumn
            head = rect.top() + (now * speed * ch + span * ((k * 0.618) % 1)) % span
            for j in range(trail):
                y = head - j * ch
                if y + ch / 2 < rect.top() or y - ch / 2 > rect.bottom():
                    continue  # znak poza ekranem
                glyph = self.RAIN_GLYPHS[int(now * 9 + k * 7 + j * 3) % len(self.RAIN_GLYPHS)]
                p.setPen(shades[j])
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

    def tray_icon(self):
        """Ikona w zasobniku: mały ekran Matrix w kolorze stanu zbiorczego."""
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        col = QColor(COLOR[self.state])
        if self.cfg["dnd_manual"]:
            col.setAlphaF(0.4)
        p.setPen(QPen(col, 2.5))
        p.setBrush(QColor(16, 19, 23))
        p.drawRoundedRect(QRectF(2.5, 2.5, 27, 27), 3, 3)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(8, 19, 11, 4), 1, 1)  # kursor terminala
        p.end()
        return QIcon(pm)

    # ---- mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False

    def event(self, e):
        if e.type() == QEvent.ToolTip:  # podpowiedź nad kafelkiem: tylko ta sesja
            sess = self.tile_at(QPointF(e.pos()))
            if sess:
                QToolTip.showText(e.globalPos(), self.session_text(sess), self)
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
        sess = self.tile_at(pos)
        if sess:
            self.focus_session(sess)
        elif self.cfg["click_jumps"]:
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
        head = menu.addAction(f"AI Status Matrix · {self.caption()}")
        head.setEnabled(False)

        target = self.attention_session()
        if target:
            label = T("Przejdź do czekającego agenta" if target["state"] == WAITING else "Przejdź do ostatniej sesji")
            a = menu.addAction(f"{label}: {target.get('project', '?')}", lambda *_, s=target: self.focus_session(s))
            if self._hotkey_on:
                a.setShortcut(QKeySequence(self.cfg["hotkey"]))
                a.setShortcutVisibleInContextMenu(True)

        sub = menu.addMenu(T("Sesje ({n})").format(n=len(self.sessions)))
        if not self.sessions:
            sub.addAction(T("Brak aktywnych sesji")).setEnabled(False)
        for s in self.sessions:
            a = sub.addAction(self.session_line(s), lambda *_, s=s: self.focus_session(s))
            a.setIcon(self._dot_icon(COLOR[s["state"]]))
            a.setToolTip(s.get("cwd", ""))
        sub.addSeparator()
        sub.addAction(T("Wyczyść listę sesji"), self.clear_sessions)

        menu.addSeparator()
        grp = QActionGroup(menu)
        for key, text in (("top", "Zawsze na wierzchu"), ("under", "Pod oknami, wyskakuj przy zmianie")):
            a = QAction(T(text), menu, checkable=True, checked=self.cfg["mode"] == key)
            a.triggered.connect(lambda _=False, k=key: self.set_mode(k))
            grp.addAction(a)
            menu.addAction(a)
        dnd = QAction(T("Nie przeszkadzać"), menu, checkable=True, checked=self.cfg["dnd_manual"])
        dnd.triggered.connect(self.toggle_dnd)
        menu.addAction(dnd)

        menu.addSeparator()
        menu.addAction(T("Ustawienia…"), self.open_settings)
        menu.addSeparator()
        menu.addAction(T("Zamknij"), QApplication.quit)

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
            set_language(self.cfg)
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


APPEARANCE_KEYS = ("housing", "state_rim", "glow", "opacity", "size", "show_names")

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
        self.combo.addItem(T("Brak"), "")
        try:
            names = sorted(n for n in os.listdir(MEDIA_DIR) if n.lower().endswith(".wav"))
        except OSError:
            names = []
        for n in names:
            self.combo.addItem(n[:-4], n)
        if value and self.combo.findData(value) < 0:
            self.combo.addItem(os.path.basename(value), value)
        self.combo.addItem(T("Wybierz plik .wav…"), self.BROWSE)
        self.combo.setCurrentIndex(max(self.combo.findData(value), 0))
        self._last = self.combo.currentIndex()
        self.combo.currentIndexChanged.connect(self._picked)

        play = QToolButton(text="▶")
        play.setToolTip(T("Odtwórz"))
        play.clicked.connect(lambda: play_sound(self.value()))

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.combo, 1)
        h.addWidget(play)

    def _picked(self, idx):
        if self.combo.itemData(idx) != self.BROWSE:
            self._last = idx
            return
        path, _ = QFileDialog.getOpenFileName(self, T("Wybierz dźwięk"), MEDIA_DIR, T("Dźwięki (*.wav)"))
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
        self.setWindowTitle(T("AI Status Matrix — ustawienia"))
        self.setMinimumWidth(540)
        self._on_preview = on_preview
        self._loading = True

        # --- Zachowanie
        self.mode = QComboBox()
        self.mode.addItem(T("Zawsze na wierzchu"), "top")
        self.mode.addItem(T("Pod oknami, wyskakuje przy zmianie"), "under")
        self.pop_rule = QComboBox()
        self.pop_rule.addItem(T("Przy każdej zmianie stanu"), "any")
        self.pop_rule.addItem(T("Tylko gdy ktoś czeka (pomarańczowe)"), "waiting")
        self.pop_seconds = QSpinBox(minimum=0, maximum=600, suffix=" s")
        self.pop_seconds.setSpecialValueText(T("do kliknięcia"))
        self.stale = QSpinBox(minimum=1, maximum=240, suffix=" min")
        self.hotkey = QKeySequenceEdit(QKeySequence(cfg["hotkey"]))
        if hasattr(self.hotkey, "setMaximumSequenceLength"):
            self.hotkey.setMaximumSequenceLength(1)
        if hasattr(self.hotkey, "setClearButtonEnabled"):
            self.hotkey.setClearButtonEnabled(True)
        self.hotkey_lbl = QLabel()
        self.hotkey_lbl.setObjectName("hint")
        self.hotkey.keySequenceChanged.connect(self._check_hotkey)
        self.click_jumps = QCheckBox(T("Kliknięcie w widget przełącza do czekającego terminala"))
        self.question_waiting = QCheckBox(T("Odpowiedź agenta zakończona pytaniem = czeka na Ciebie (pomarańczowe)"))

        # --- Nie przeszkadzać (w zakładce Zachowanie)
        self.dnd_fullscreen = QCheckBox(T("Gdy aplikacja jest na pełnym ekranie (prezentacja, gra, wideo)"))
        self.dnd_hours = QCheckBox(T("W godzinach"))
        self.dnd_from = QTimeEdit(QTime.fromString(cfg["dnd_from"], "HH:mm"), displayFormat="HH:mm")
        self.dnd_to = QTimeEdit(QTime.fromString(cfg["dnd_to"], "HH:mm"), displayFormat="HH:mm")
        self.dnd_hours.toggled.connect(lambda on: (self.dnd_from.setEnabled(on), self.dnd_to.setEnabled(on)))

        # --- Dźwięki
        self.sound_waiting = SoundPicker(cfg["sound_waiting"])
        self.notify_long = QCheckBox(T("Zagraj dźwięk, gdy agent skończy długie zadanie"))
        self.long_minutes = QSpinBox(minimum=1, maximum=240, suffix=" min")
        self.sound_long = SoundPicker(cfg["sound_long_task"])
        self.idle_remind = QCheckBox(T("Zagraj dźwięk, gdy sesja długo czeka na kolejne polecenie"))
        self.idle_minutes = QSpinBox(minimum=1, maximum=480, suffix=" min")
        self.sound_idle = SoundPicker(cfg["sound_idle"])
        self.notify_long.toggled.connect(lambda on: (self.long_minutes.setEnabled(on), self.sound_long.setEnabled(on)))
        self.idle_remind.toggled.connect(lambda on: (self.idle_minutes.setEnabled(on), self.sound_idle.setEnabled(on)))

        # --- Wygląd
        self.preset = QComboBox()
        for name, housing, rim in STYLE_PRESETS:
            self.preset.addItem(T(name), (housing, rim))
        self.preset.addItem(T("Własny"), None)
        self.housing = QComboBox()
        self.housing.addItem(T("Ciemna"), "dark")
        self.housing.addItem(T("Jasna"), "light")
        self.housing.addItem(T("Szklana (półprzezroczysta)"), "glass")
        self.housing.addItem(T("Automatyczna (dopasuj do tła)"), "auto")
        self.state_rim = QCheckBox(T("Obwódka i poświata w kolorze aktualnego stanu"))
        self.show_names = QCheckBox(T("Nazwa projektu (terminala) na dole kafelka"))
        self.glow = QSlider(Qt.Horizontal, minimum=0, maximum=100)
        self.glow_lbl = QLabel()
        self.opacity = QSlider(Qt.Horizontal, minimum=30, maximum=100)
        self.opacity_lbl = QLabel()
        self.scale = QComboBox()
        for v in SIZES:
            self.scale.addItem(f"{round(v * 100)}%", v)

        # --- System
        self.autostart = QCheckBox(T("Uruchamiaj razem z Windows"))
        self.language = QComboBox()
        self.language.addItem(T("Automatycznie (język systemu)"), "auto")
        self.language.addItem("Polski", "pl")
        self.language.addItem("English", "en")
        self.hooks_lbl = QLabel()
        self.hooks_btn = QPushButton()
        self.hooks_btn.clicked.connect(self.toggle_hooks)
        self._refresh_hooks()
        open_dir = QPushButton(T("Otwórz folder stanu"))
        open_dir.clicked.connect(lambda: (os.makedirs(SESSIONS_DIR, exist_ok=True), os.startfile(APP_DIR)))

        # --- wartości początkowe
        for combo, key in ((self.mode, "mode"), (self.pop_rule, "pop_rule"), (self.scale, "size"),
                           (self.housing, "housing"), (self.language, "language")):
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
        self.show_names.setChecked(cfg["show_names"])
        self.glow.setValue(int(cfg["glow"] * 100))
        self.opacity.setValue(int(cfg["opacity"] * 100))
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
            T("Zachowanie"))
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
           "W trybie „Nie przeszkadzać” dźwięki są wyciszone."), T("Dźwięki"))
        tabs.addTab(self._page([
            ("Gotowy styl", self.preset),
            None,
            ("Obudowa", self.housing),
            ("", self.state_rim),
            ("Siła poświaty", self._with_label(self.glow, self.glow_lbl)),
            ("Krycie tła", self._with_label(self.opacity, self.opacity_lbl)),
            None,
            ("Rozmiar", self.scale),
            ("", self.show_names),
        ], "Zmiany widać od razu na widgecie, a Anuluj przywraca poprzedni wygląd. "
           "Obudowa automatyczna co 1,5 s sprawdza jasność tła i wybiera jasne albo ciemne kafelki."), T("Wygląd"))
        hk_w = QWidget()
        hk = QHBoxLayout(hk_w)
        hk.setContentsMargins(0, 0, 0, 0)
        hk.addWidget(self.hooks_lbl, 1)
        hk.addWidget(self.hooks_btn)
        tabs.addTab(self._page([
            ("Język / Language", self.language),
            ("", self.autostart),
            ("Hooki Claude Code", hk_w),
            ("", open_dir),
        ], "Hooki działają w sesjach Claude Code uruchomionych po instalacji. Zmiana języka działa po zapisaniu."),
            T("System"))

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText(T("Zapisz"))
        buttons.button(QDialogButtonBox.Cancel).setText(T("Anuluj"))
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
        for w in (self.housing, self.scale):
            w.currentIndexChanged.connect(self._changed)
        self.state_rim.toggled.connect(self._changed)
        self.show_names.toggled.connect(self._changed)
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
                lbl = QLabel(T(row).upper())
                lbl.setObjectName("section")
                form.addRow(lbl)
            else:
                form.addRow(T(row[0]), row[1])
        v.addLayout(form)
        v.addStretch(1)
        hint = QLabel(T(hint_text))
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
            self.hotkey_lbl.setText(T("Wyłączony. Kliknij pole i naciśnij kombinację klawiszy."))
        elif parse_hotkey(text) is None:
            self.hotkey_lbl.setText(T("Nieobsługiwany klawisz: użyj litery, cyfry, F1–F24 albo spacji z Ctrl/Alt/Shift/Win."))
        else:
            self.hotkey_lbl.setText(T("Przełącza do najdłużej czekającego agenta, a gdy nikt nie czeka, do ostatniej sesji."))

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
        self.hooks_lbl.setText(T("zainstalowane" if on else "niezainstalowane"))
        self.hooks_btn.setText(T("Usuń" if on else "Zainstaluj"))

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
            "show_names": self.show_names.isChecked(),
            "glow": self.glow.value() / 100,
            "opacity": self.opacity.value() / 100,
            "size": self.scale.currentData(),
            "autostart": self.autostart.isChecked(),
            "language": self.language.currentData(),
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
    migrate_app_dir()
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    lock = QLockFile(os.path.join(APP_DIR, "widget.lock"))
    if not lock.tryLock(100):
        return  # już działa
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)
    cfg = load_config()
    set_language(cfg)
    if cfg["autostart"]:
        try:
            set_autostart(True)  # odśwież wpis: nowa nazwa projektu, aktualna ścieżka
        except OSError:
            pass
    w = Light(cfg)
    w.show()
    w.apply_layer()
    w.register_hotkey()
    if w.cfg["mode"] == "under":
        w.pop()  # pokaż się po starcie, potem wróć pod okna
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
