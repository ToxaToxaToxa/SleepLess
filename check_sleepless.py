"""Перевірка SleepLess без справжнього профілю: APPDATA — тимчасова тека,
автозапуск не чіпаємо. Друкує ПРОЙДЕНО / НЕ ПРОЙДЕНО і код виходу 1."""

import ctypes
import json
import os
import sys
import tempfile
import time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["APPDATA"] = str(tmp)
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

import sleepless as sl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

results = []


def check(name, ok):
    results.append(ok)
    print(("ПРОЙДЕНО     " if ok else "НЕ ПРОЙДЕНО  ") + name)


def thread_flags():
    """Поточний стан потоку: ставимо той самий і читаємо попередній."""
    k = ctypes.windll.kernel32
    prev = k.SetThreadExecutionState(sl.ES_CONTINUOUS)
    k.SetThreadExecutionState(prev)
    return prev


# Справжній Claude Code, що запускає цю перевірку, сам тримав би
# систему — для основних пунктів його «немає».
real_claude = sl.claude_code_running
check("Claude Code, що запускає перевірку, видно", real_claude())
sl.claude_code_running = lambda: False

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
tray = sl.Tray(app)

check("старт: вимкнено", not tray.guard.holding and "Вимкнено" in tray.status.text())

tray.set_enabled(True)
f = thread_flags()
check("увімкнено: система й екран у стані потоку",
      f & (sl.ES_SYSTEM_REQUIRED | sl.ES_DISPLAY_REQUIRED) == 3)
check("увімкнено: є запит живлення процесу", tray.guard._request is not None)
check("значок увімкнено", tray.tray.icon().cacheKey() == tray.icon_on.cacheKey())

# чужий код скинув стан потоку — наступний тік має відновити
ctypes.windll.kernel32.SetThreadExecutionState(sl.ES_CONTINUOUS)
tray.refresh()
check("скинутий стан відновлено за тік", thread_flags() & 3 == 3)

tray.display.trigger()          # вимкнути «Не гасити екран»
f = thread_flags()
check("без екрана: лише система", f & 3 == sl.ES_SYSTEM_REQUIRED)
tray.display.trigger()

saved = json.loads(sl.SETTINGS_FILE.read_text(encoding="utf-8"))
check("збережено enabled=true", saved["enabled"] is True)

tray.enable_for(60)
saved = json.loads(sl.SETTINGS_FILE.read_text(encoding="utf-8"))
check("таймер: на диск іде «вимкнено»", saved["enabled"] is False)
check("таймер: у статусі час кінця", " до " in tray.status.text())
tray.until = time.time() - 1
tray.refresh()
check("таймер вийшов: вимкнено", not tray.guard.holding and not tray.settings["enabled"])
check("вимкнено: стан потоку скинуто", thread_flags() & 3 == 0)

tray.set_enabled(True)
orig = sl.on_ac_power
sl.on_ac_power = lambda: False
tray.ac_only.trigger()
check("лише від мережі, на батареї: не тримає",
      not tray.guard.holding and "чекає" in tray.status.text())
sl.on_ac_power = lambda: True
tray.refresh()
check("лише від мережі, від мережі: тримає", tray.guard.holding)
sl.on_ac_power = orig
tray.ac_only.trigger()

sl.claude_code_running = lambda: True
sl.on_ac_power = lambda: True
tray.set_enabled(False)
check("Claude Code, від мережі: тримає",
      tray.guard.holding and "Claude Code" in tray.status.text())
sl.on_ac_power = lambda: False
tray.refresh()
check("Claude Code, на батареї: не тримає", not tray.guard.holding)
sl.on_ac_power = lambda: True
tray.claude.trigger()
check("режим Claude Code вимкнено: не тримає", not tray.guard.holding)
tray.claude.trigger()
sl.claude_code_running = lambda: False
tray.refresh()
check("Claude Code закрився: відпускає", not tray.guard.holding)
sl.on_ac_power = orig

tray.guard.release()
check("вихід: запит знято", tray.guard._request is None and thread_flags() & 3 == 0)

for active in (True, False):
    sl.make_icon(active, 128).pixmap(128, 128).save(
        str(Path(__file__).with_name(f"s_icon_{'on' if active else 'off'}.png")))

print(f"Підсумок: {sum(results)} з {len(results)}")
sys.exit(0 if all(results) else 1)
