"""Sygnalizator AI: minimalistyczny widget pokazujący stan agentów AI.

Zielone   - wszyscy agenci bezczynni
Czerwone  - przynajmniej jeden pracuje
Pomarańcz - ktoś czeka na Twoją odpowiedź (ma pierwszeństwo)

Stan sesji zapisuje hook.py do %LOCALAPPDATA%\\ai-traffic-light\\sessions\\*.json.
Widget tylko czyta te pliki. Uruchamiaj przez pythonw (bez okna konsoli).
"""
import ctypes
import json
import math
import os
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QLockFile, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QMenu, QPushButton, QSlider, QSpinBox, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import install_hooks  # noqa: E402

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ai-traffic-light")
SESSIONS_DIR = os.path.join(APP_DIR, "sessions")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

IDLE, WORKING, WAITING, STALE = "idle", "working", "waiting", "stale"
LABEL = {IDLE: "bezczynny", WORKING: "pracuje", WAITING: "czeka na Ciebie", STALE: "brak sygnału"}
CLI_NAME = {"claude": "Claude Code", "gemini": "Gemini CLI", "codex": "Codex CLI"}

RED, AMBER, GREEN, GREY = QColor(255, 75, 58), QColor(255, 162, 31), QColor(47, 210, 124), QColor(120, 128, 138)
LAMP_COLOR = {WORKING: RED, WAITING: AMBER, IDLE: GREEN}
DOT_COLOR = {WORKING: QColor(217, 58, 43), WAITING: QColor(238, 143, 18), IDLE: QColor(31, 164, 99), STALE: GREY}

DEFAULTS = {
    "mode": "under",          # "top" | "under"
    "pop_rule": "any",        # "any" | "waiting"
    "pop_seconds": 5,         # 0 = zostaje na wierzchu do kliknięcia
    "scale": 1.25,
    "orientation": "vertical",
    "opacity": 0.85,
    "housing": "dark",        # "dark" | "light" | "glass" | "auto"
    "state_rim": True,        # obwódka i poświata w kolorze stanu
    "glow": 0.6,              # siła poświaty 0..1
    "show_dots": True,
    "sound_on_waiting": False,
    "stale_minutes": 10,
    "forget_hours": 12,
    "autostart": False,
    "pos": None,
}

# obudowa: (tło rgb, bazowa alfa tła, obwódka rgba, zgaszona lampa rgba)
HOUSING = {
    "dark":  ((16, 19, 23), 255, (255, 255, 255, 26), (255, 255, 255, 26)),
    "light": ((246, 247, 249), 255, (0, 0, 0, 36), (20, 28, 36, 34)),
    "glass": ((255, 255, 255), 70, (255, 255, 255, 150), (255, 255, 255, 72)),
}

# ---------------------------------------------------------------- Windows z-order
user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
HWND_TOPMOST, HWND_NOTOPMOST, HWND_BOTTOM = -1, -2, 1
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE


def set_z(hwnd, where):
    user32.SetWindowPos(hwnd, where, 0, 0, 0, 0, SWP_FLAGS)


# ---------------------------------------------------------------- config & sessions
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def load_sessions(cfg):
    out, now = [], time.time()
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        age = now - float(s.get("ts", 0))
        if age > cfg["forget_hours"] * 3600:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if s.get("state") in (WORKING, WAITING) and age > cfg["stale_minutes"] * 60:
            s["state"] = STALE
        s["_path"] = path
        out.append(s)
    out.sort(key=lambda s: (s.get("project", ""), s.get("session_id", "")))
    return out


def aggregate(sessions):
    states = {s.get("state") for s in sessions}
    if WAITING in states:
        return WAITING
    if WORKING in states:
        return WORKING
    return IDLE


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


# ---------------------------------------------------------------- the widget
class Light(QWidget):
    def __init__(self, cfg):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_AlwaysShowToolTips)
        self.setWindowTitle("Sygnalizator AI")

        self.cfg = cfg
        self.sessions = []
        self.state = IDLE
        self.popped = False
        self._pop_t0 = -10.0
        self._drag = None
        self._moved = False

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

    # ---- geometry
    def dims(self):
        s = self.cfg["scale"]
        d, gap, pad, margin = 14 * s, 6 * s, 7 * s, 16 * s
        long_side, short_side = 3 * d + 2 * gap + 2 * pad, d + 2 * pad
        cw, ch = (short_side, long_side) if self.cfg["orientation"] == "vertical" else (long_side, short_side)
        dot, dgap, dtop = 4 * s, 3 * s, 5 * s
        n = len(self.sessions) if self.cfg["show_dots"] else 0
        dots_w = n * dot + max(n - 1, 0) * dgap
        return dict(s=s, d=d, gap=gap, pad=pad, m=margin, cw=cw, ch=ch, dot=dot, dgap=dgap, dtop=dtop, n=n, dots_w=dots_w)

    def relayout(self):
        g = self.dims()
        w = max(g["cw"], g["dots_w"]) + 2 * g["m"]
        h = g["ch"] + (g["dtop"] + g["dot"] if g["n"] else 0) + 2 * g["m"]
        self.setFixedSize(math.ceil(w), math.ceil(h))
        self.update()

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
        self.sessions = load_sessions(self.cfg)
        if len(self.sessions) != prev_n and self.cfg["show_dots"]:
            self.relayout()
        new = aggregate(self.sessions)
        changed = new != self.state
        self.state = new
        self.setToolTip(self.tooltip_text())
        self.tray.setToolTip("Sygnalizator AI: " + self.caption())
        self.tray.setIcon(self.tray_icon())
        if changed and not initial:
            self.on_change(new)
        self._sync_anim()
        self.update()

    def caption(self):
        n_wait = sum(s["state"] == WAITING for s in self.sessions)
        n_work = sum(s["state"] == WORKING for s in self.sessions)
        if n_wait:
            return f"{n_wait} czeka na Ciebie"
        if n_work:
            return f"{n_work} pracuje"
        return "wszyscy wolni" if self.sessions else "brak aktywnych sesji"

    def tooltip_text(self):
        if not self.sessions:
            return "Brak aktywnych sesji"
        lines = []
        for s in self.sessions:
            cli = CLI_NAME.get(s.get("cli"), s.get("cli", ""))
            line = f"{s.get('project', '?')} · {cli} — {LABEL[s['state']]}"
            if s["state"] in (WORKING, WAITING) and s.get("detail"):
                line += f"\n    {s['detail']}"
            lines.append(line)
        return "\n".join(lines)

    def on_change(self, new):
        if new == WAITING and self.cfg["sound_on_waiting"]:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        if self.cfg["mode"] == "under" and (self.cfg["pop_rule"] == "any" or new == WAITING):
            self.pop()
        elif self.cfg["mode"] == "top":
            self._pop_t0 = time.monotonic()  # sama animacja, bez zmiany warstwy

    # ---- z-order
    def hwnd(self):
        return int(self.winId())

    def apply_layer(self):
        self.unpop.stop()
        self.popped = False
        if self.cfg["mode"] == "top":
            set_z(self.hwnd(), HWND_TOPMOST)
        else:
            set_z(self.hwnd(), HWND_NOTOPMOST)
            set_z(self.hwnd(), HWND_BOTTOM)

    def pop(self):
        set_z(self.hwnd(), HWND_TOPMOST)
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
        return self.state == WAITING or time.monotonic() - self._pop_t0 < 1.2

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

        cx = self.width() / 2
        body = QRectF(cx - g["cw"] / 2, g["m"], g["cw"], g["ch"])
        radius = min(g["cw"], g["ch"]) / 2

        # animacja "pop": sprężyste powiększenie
        if 0 <= t < 0.5:
            k = t / 0.5 - 1
            sc = 0.8 + 0.2 * (1 + 2.70158 * k ** 3 + 1.70158 * k ** 2)  # easeOutBack
            p.translate(body.center())
            p.scale(sc, sc)
            p.translate(-body.center())

        # cień
        for i, a in enumerate((18, 12, 7)):
            sh = body.adjusted(-i - 1, -i + 2, i + 1, i + 4)
            path = QPainterPath()
            path.addRoundedRect(sh, radius + i, radius + i)
            p.fillPath(path, QColor(0, 0, 0, a))

        breathe = 0.72 + 0.28 * math.cos(now * math.tau / 1.6)
        pulse = breathe if self.state == WAITING else 1.0
        state_col = LAMP_COLOR[self.state]
        rim = self.cfg["state_rim"]

        # poświata dookoła obudowy w kolorze stanu
        if rim and self.cfg["glow"] > 0:
            layers = 7
            for i in range(layers, 0, -1):
                grow = i * 2 * g["s"]
                c = QColor(state_col)
                c.setAlphaF(min(1.0, 0.16 * self.cfg["glow"] * pulse * (1 - (i - 1) / layers)))
                halo = QPainterPath()
                halo.addRoundedRect(body.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow)
                p.fillPath(halo, c)

        # obudowa
        bg_rgb, bg_alpha, border, lamp_off = HOUSING[self.housing()]
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

        # lampy
        order = (WORKING, WAITING, IDLE)
        for i, st in enumerate(order):
            off = g["pad"] + i * (g["d"] + g["gap"]) + g["d"] / 2
            if self.cfg["orientation"] == "vertical":
                c = QPointF(body.center().x(), body.top() + off)
            else:
                c = QPointF(body.left() + off, body.center().y())
            r = g["d"] / 2
            p.setPen(Qt.NoPen)
            if st == self.state:
                col = QColor(LAMP_COLOR[st])
                alpha = breathe if st == WAITING else 1.0
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
        if g["n"]:
            y = body.bottom() + g["dtop"] + g["dot"] / 2
            x = cx - g["dots_w"] / 2 + g["dot"] / 2
            for s in self.sessions:
                p.setBrush(DOT_COLOR.get(s["state"], GREY))
                p.drawEllipse(QPointF(x, y), g["dot"] / 2, g["dot"] / 2)
                x += g["dot"] + g["dgap"]
        p.end()

    def tray_icon(self):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(16, 19, 23))
        p.drawRoundedRect(QRectF(2, 2, 28, 28), 14, 14)
        p.setBrush(LAMP_COLOR[self.state])
        p.drawEllipse(QPointF(16, 16), 8, 8)
        p.end()
        return QIcon(pm)

    # ---- mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False

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
        if self.cfg["mode"] == "under":
            self.apply_layer()  # kliknięcie = "widzę, schowaj"

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
        head = menu.addAction(f"Sygnalizator AI · {self.caption()}")
        head.setEnabled(False)

        sub = menu.addMenu(f"Sesje ({len(self.sessions)})")
        if not self.sessions:
            sub.addAction("Brak aktywnych sesji").setEnabled(False)
        for s in self.sessions:
            cli = CLI_NAME.get(s.get("cli"), s.get("cli", ""))
            a = sub.addAction(f"{s.get('project', '?')} · {cli} — {LABEL[s['state']]}")
            a.setIcon(self._dot_icon(DOT_COLOR.get(s["state"], GREY)))
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

    def clear_sessions(self):
        for s in self.sessions:
            try:
                os.remove(s["_path"])
            except OSError:
                pass
        self.refresh()

    def open_settings(self):
        before = dict(self.cfg)
        dlg = SettingsDialog(self.cfg, self.preview)
        set_z(int(dlg.winId()), HWND_TOPMOST)
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
class SettingsDialog(QDialog):
    def __init__(self, cfg, on_preview=None):
        super().__init__(None, Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("Sygnalizator AI — ustawienia")
        self.setMinimumWidth(520)
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
        self.sound = QCheckBox("Dźwięk, gdy ktoś zaczyna czekać na odpowiedź")

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
        self.show_dots = QCheckBox("Pokazuj kropki sesji pod sygnalizatorem")

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
        self.sound.setChecked(cfg["sound_on_waiting"])
        self.state_rim.setChecked(cfg["state_rim"])
        self.glow.setValue(int(cfg["glow"] * 100))
        self.opacity.setValue(int(cfg["opacity"] * 100))
        self.show_dots.setChecked(cfg["show_dots"])
        self.autostart.setChecked(cfg["autostart"])

        # --- zakładki
        tabs = QTabWidget()
        tabs.addTab(self._page([
            ("Warstwa", self.mode),
            ("Wyskakuj", self.pop_rule),
            ("Na wierzchu przez", self.pop_seconds),
            ("Zawieszona sesja po", self.stale),
            ("", self.sound),
        ], "Sesja, która pracuje albo czeka bez żadnego zdarzenia dłużej niż ustawiony czas "
           "(np. po przerwaniu klawiszem Esc), dostaje szarą kropkę i przestaje wpływać na kolor."), "Zachowanie")
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
        ], "Zmiany widać od razu na sygnalizatorze, a Anuluj przywraca poprzedni wygląd. "
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
        v.setContentsMargins(14, 14, 14, 12)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        for row in rows:
            if row is None:
                line = QWidget()
                line.setFixedHeight(1)
                line.setObjectName("rule")
                line.setAttribute(Qt.WA_StyledBackground)
                form.addRow(line)
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
        return {
            "mode": self.mode.currentData(),
            "pop_rule": self.pop_rule.currentData(),
            "pop_seconds": self.pop_seconds.value(),
            "stale_minutes": self.stale.value(),
            "sound_on_waiting": self.sound.isChecked(),
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
QLabel#section { color: #7F8B97; font-size: 8pt; letter-spacing: 1px; padding-top: 8px; }
QLabel#hint { color: #7F8B97; font-size: 8.5pt; }
QWidget#rule { background: #262D36; }
QTabWidget::pane { border: 1px solid #262D36; border-radius: 8px; top: -1px; background: #171B21; }
QTabBar::tab { background: transparent; color: #8A96A3; padding: 7px 16px; margin-right: 2px; border: 1px solid transparent; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { color: #E6EDF2; background: #171B21; border-color: #262D36; border-bottom-color: #171B21; }
QTabBar::tab:hover:!selected { color: #C9D2DA; }
QSlider::groove:horizontal:disabled { background: #22282F; }
QSlider::handle:horizontal:disabled { background: #4A535D; }
QComboBox, QSpinBox { background: #1C2128; color: #E6EDF2; border: 1px solid #2E3640; border-radius: 6px; padding: 4px 8px; min-height: 20px; }
QComboBox:focus, QSpinBox:focus { border-color: #4C8DFF; }
QComboBox QAbstractItemView { background: #1C2128; color: #E6EDF2; border: 1px solid #2E3640; selection-background-color: #2A3340; }
QComboBox:disabled, QSpinBox:disabled { color: #5F6B77; }
QCheckBox { color: #E6EDF2; spacing: 8px; }
QPushButton { background: #232A33; color: #E6EDF2; border: 1px solid #333C47; border-radius: 6px; padding: 5px 14px; }
QPushButton:hover { background: #2B333E; }
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
    if w.cfg["mode"] == "under":
        w.pop()  # pokaż się po starcie, potem wróć pod okna
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
