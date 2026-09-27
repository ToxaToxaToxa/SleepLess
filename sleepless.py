"""
SleepLess — маленька програма в треї, що не дає Windows засинати.

Клік по значку вмикає й вимикає режим. У меню: чи тримати ще й екран,
таймер («на 1 год»), «лише від мережі» і автозапуск із Windows.
Окремий режим «Поки працює Claude Code»: поки запущено claude.exe (CLI)
і ноутбук від мережі, система не спить навіть із вимкненим основним.

ЯК ТРИМАЄМО. Два механізми одразу — так само, як у GS_Starlink
(modules/app_settings/sleep_guard.py), де один механізм уже підвів:
  • SetThreadExecutionState — це стан ПОТОКУ, і його мовчки скидає
    будь-який код у тому ж потоці. Тому ставимо його не раз, а
    перевстановлюємо кожні 5 с;
  • PowerCreateRequest/PowerSetRequest — запит на рівні процесу, його
    видно в «powercfg /requests» (з правами адміністратора).

На цьому ноутбуці лише Modern Standby: погаслий через простій екран
одразу присипляє систему. Тому «Не гасити екран» типово ввімкнено.
Закриту кришку чи кнопку живлення програма не перебиває — і не має.
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QLockFile, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

APP = "SleepLess"
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP
SETTINGS_FILE = DATA_DIR / "settings.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

REASSERT_MS = 5000
# Хвилини для меню «Увімкнути на…»; 0 — без обмеження.
DURATIONS = [(30, "30 хв"), (60, "1 год"), (120, "2 год"), (240, "4 год"),
             (480, "8 год"), (0, "Без обмеження")]

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
PowerRequestDisplayRequired = 0
PowerRequestSystemRequired = 1

kernel32 = ctypes.windll.kernel32
kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]


class _ReasonUnion(ctypes.Union):
    _fields_ = [("SimpleReasonString", ctypes.c_wchar_p)]


class _ReasonContext(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong),
                ("Flags", ctypes.c_ulong),
                ("Reason", _ReasonUnion)]


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD)]


kernel32.PowerCreateRequest.restype = wintypes.HANDLE
kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def on_ac_power() -> bool:
    status = _PowerStatus()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return True          # не знаємо — не заважаємо користувачеві
    return status.ACLineStatus != 0   # 255 «невідомо» теж рахуємо мережею


# --- чи працює Claude Code ---------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# Десктопний застосунок Claude теж зветься Claude.exe, але висить у фоні
# постійно — рахувати його означало б «не спати ніколи». Тримаємо лише
# CLI (Claude Code), який ставиться в ~\.local\bin або через npm.
DESKTOP_APP_MARKERS = ("\\anthropicclaude\\", "\\windowsapps\\")


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]


def _process_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def claude_code_running() -> bool:
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return False
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "claude.exe":
                path = _process_path(entry.th32ProcessID).lower()
                if not any(m in path for m in DESKTOP_APP_MARKERS):
                    return True
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snap)


class Guard:
    """Тримає систему (і, за бажанням, екран) при тямі."""

    def __init__(self):
        self.holding = False
        self.display = False
        self._request = None
        self._context = None      # має жити, поки живе запит

    def hold(self, display: bool) -> None:
        if self.holding and display != self.display:
            self.release()        # змінився набір — пересоздаємо запит
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        if display:
            flags |= ES_DISPLAY_REQUIRED
        kernel32.SetThreadExecutionState(flags)
        if self._request is None:
            context = _ReasonContext()
            context.Version = 0
            context.Flags = POWER_REQUEST_CONTEXT_SIMPLE_STRING
            context.Reason.SimpleReasonString = f"{APP}: увімкнено користувачем"
            handle = kernel32.PowerCreateRequest(ctypes.byref(context))
            if handle and handle != wintypes.HANDLE(-1).value:
                kernel32.PowerSetRequest(handle, PowerRequestSystemRequired)
                if display:
                    kernel32.PowerSetRequest(handle, PowerRequestDisplayRequired)
                self._request, self._context = handle, context
        self.holding, self.display = True, display

    def release(self) -> None:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        if self._request is not None:
            kernel32.PowerClearRequest(self._request, PowerRequestSystemRequired)
            if self.display:
                kernel32.PowerClearRequest(self._request,
                                           PowerRequestDisplayRequired)
            kernel32.CloseHandle(self._request)
            self._request = self._context = None
        self.holding = False


# --- значок ------------------------------------------------------------

AMBER = QColor("#F2A93B")
GREY = QColor("#9AA4AD")


def make_icon(active: bool, size: int = 64) -> QIcon:
    """Увімкнено — бурштинове око, розплющене; вимкнено — сірий місяць."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    if active:
        eye = QPainterPath()
        eye.moveTo(4 * s, 32 * s)
        eye.quadTo(32 * s, 2 * s, 60 * s, 32 * s)
        eye.quadTo(32 * s, 62 * s, 4 * s, 32 * s)
        p.setPen(QPen(AMBER, 5 * s))
        p.setBrush(Qt.NoBrush)
        p.drawPath(eye)
        p.setPen(Qt.NoPen)
        p.setBrush(AMBER)
        p.drawEllipse(QPointF(32 * s, 32 * s), 11 * s, 11 * s)
    else:
        moon = QPainterPath()
        moon.addEllipse(QRectF(8 * s, 8 * s, 48 * s, 48 * s))
        bite = QPainterPath()
        bite.addEllipse(QRectF(22 * s, 2 * s, 44 * s, 44 * s))
        p.setPen(Qt.NoPen)
        p.setBrush(GREY)
        p.drawPath(moon.subtracted(bite))
    p.end()
    return QIcon(pix)


# --- налаштування й автозапуск -----------------------------------------

DEFAULTS = {"enabled": False, "display": True, "ac_only": False, "claude": True}


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return {**DEFAULTS, **{k: data[k] for k in DEFAULTS if k in data}}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(settings: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{Path(__file__).resolve()}"'


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP)
            except FileNotFoundError:
                pass


# --- трей --------------------------------------------------------------

class Tray:
    def __init__(self, app: QApplication):
        self.app = app
        self.settings = load_settings()
        self.guard = Guard()
        self.until = None          # time.time() кінця таймера або None

        self.icon_on, self.icon_off = make_icon(True), make_icon(False)
        self.tray = QSystemTrayIcon(self.icon_off)
        self.tray.activated.connect(self._activated)

        menu = QMenu()
        self.status = menu.addAction("")
        self.status.setEnabled(False)
        menu.addSeparator()

        self.toggle = menu.addAction("Не давати спати")
        self.toggle.setCheckable(True)
        self.toggle.triggered.connect(lambda on: self.set_enabled(on))

        timer_menu = menu.addMenu("Увімкнути на…")
        for minutes, label in DURATIONS:
            act = timer_menu.addAction(label)
            act.triggered.connect(lambda _=False, m=minutes: self.enable_for(m))

        menu.addSeparator()
        self.display = self._option(menu, "Не гасити екран", "display")
        self.ac_only = self._option(menu, "Лише коли від мережі", "ac_only")
        self.claude = self._option(menu, "Поки працює Claude Code (від мережі)",
                                   "claude")
        self.autostart = menu.addAction("Запускати з Windows")
        self.autostart.setCheckable(True)
        self.autostart.setChecked(autostart_enabled())
        self.autostart.triggered.connect(self._autostart)

        menu.addSeparator()
        menu.addAction("Вихід").triggered.connect(self.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)

        self.tick = QTimer()
        self.tick.timeout.connect(self.refresh)
        self.tick.start(REASSERT_MS)

        app.aboutToQuit.connect(self.guard.release)
        self.refresh()
        self.tray.show()

    def _option(self, menu: QMenu, title: str, key: str) -> QAction:
        act = menu.addAction(title)
        act.setCheckable(True)
        act.setChecked(self.settings[key])

        def changed(on: bool) -> None:
            self.settings[key] = on
            self._save()
            self.refresh()

        act.triggered.connect(changed)
        return act

    # --- дії --------------------------------------------------------

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.set_enabled(not self.settings["enabled"])

    def set_enabled(self, on: bool) -> None:
        self.settings["enabled"] = on
        self.until = None
        self._save()
        self.refresh()

    def enable_for(self, minutes: int) -> None:
        self.set_enabled(True)
        if minutes:
            self.until = time.time() + minutes * 60
            self._save()
        self.refresh()

    def _save(self) -> None:
        # Таймер не зберігаємо: після перезапуску «на 1 год» не має
        # перетворитися на «назавжди», тож на диск іде «вимкнено».
        save_settings({**self.settings,
                       "enabled": self.settings["enabled"] and not self.until})

    def _autostart(self, on: bool) -> None:
        try:
            set_autostart(on)
        except OSError as exc:
            self.tray.showMessage(APP, f"Не вдалося змінити автозапуск: {exc}",
                                  QSystemTrayIcon.Warning)
        self.autostart.setChecked(autostart_enabled())

    def quit(self) -> None:
        self.guard.release()
        self.tray.hide()
        self.app.quit()

    # --- стан -------------------------------------------------------

    def refresh(self) -> None:
        if self.until and time.time() >= self.until:
            self.until = None
            self.settings["enabled"] = False
            self._save()
            self.tray.showMessage(APP, "Час вийшов, комп'ютер знову може спати.",
                                  self.icon_off, 4000)

        enabled = self.settings["enabled"]
        ac = on_ac_power()
        waiting_ac = enabled and self.settings["ac_only"] and not ac
        # Режим Claude Code — лише від мережі: на батареї довга сесія
        # агента з'їла б акумулятор, поки людина відійшла.
        by_claude = (not (enabled and not waiting_ac) and self.settings["claude"]
                     and ac and claude_code_running())
        holding = (enabled and not waiting_ac) or by_claude

        if holding:
            self.guard.hold(self.settings["display"])
        elif self.guard.holding:
            self.guard.release()

        if holding:
            what = "Не спить" if not self.settings["display"] else "Не спить, екран не гасне"
            if by_claude:
                what += ": працює Claude Code"
            elif self.until:
                what += " до " + time.strftime("%H:%M", time.localtime(self.until))
        elif waiting_ac:
            what = "Увімкнено, чекає живлення від мережі"
        else:
            what = "Вимкнено, комп'ютер може спати"

        self.status.setText(what)
        self.toggle.setChecked(enabled)
        self.tray.setIcon(self.icon_on if holding else self.icon_off)
        self.tray.setToolTip(f"{APP}: {what}")


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(DATA_DIR / "sleepless.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        return 0              # уже запущено — друга копія не потрібна

    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return 1
    tray = Tray(app)          # noqa: F841 — має жити до виходу
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
