"""Збірка SleepLess.exe: python build.py → dist\\SleepLess.exe (один файл)."""

import subprocess
import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication

HERE = Path(__file__).parent


def make_ico() -> Path:
    from PIL import Image
    import sleepless
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    png = HERE / "build" / "icon.png"
    png.parent.mkdir(exist_ok=True)
    sleepless.make_icon(True, 256).pixmap(256, 256).save(str(png))
    ico = HERE / "build" / "sleepless.ico"
    Image.open(png).save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                     (64, 64), (128, 128), (256, 256)])
    return ico


if __name__ == "__main__":
    ico = make_ico()
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile",
        "--windowed", "--name", "SleepLess", "--icon", str(ico),
        # Лише потрібне з Qt — інакше exe на сотні МБ.
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtNetwork",
        "--exclude-module", "PySide6.QtPdf",
        str(HERE / "sleepless.py")], cwd=HERE)
    print("Готово:", HERE / "dist" / "SleepLess.exe")
