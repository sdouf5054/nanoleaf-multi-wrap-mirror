"""
Nanoleaf Screen Mirror — GUI 앱 진입점 (PySide6)

사용법:
    python main.py
    python main.py --startup   ← 트레이로 바로 시작 (창 숨김)

[ADR-028] Named Mutex 단일 인스턴스 (KEEP)
[ADR-029] PySide6 빌트인 High DPI 스케일링 (CHANGE → B)
  - SetProcessDpiAwareness + Qt 자동 스케일링을 PySide6에 위임
  - 수동 DPI 재조정 코드 40줄 제거
"""

import sys
import os
import ctypes

# === 1) pythonw.exe 스트림 보호 ===
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

# === 2) Windows Per-Monitor DPI Aware ===
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# === 3) 작업 디렉토리 설정 ===
if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))
else:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

# === 4) 콘솔 창 숨기기 ===
try:
    ctypes.windll.user32.ShowWindow(
        ctypes.windll.kernel32.GetConsoleWindow(), 0
    )
except Exception:
    pass

# === 5) PySide6 High DPI (ADR-029: 빌트인 스케일링 사용) ===
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

# PySide6는 기본적으로 High DPI를 잘 지원함.
# PassThrough로 설정하면 OS DPI 설정을 그대로 반영.
QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)

from core.config import load_config
from ui.main_window import MainWindow


def main():
    # === 0) ADR-028: 단일 인스턴스 — Named Mutex ===
    try:
        mutex_name = "nanoleaf_mirror_singleton_mutex"
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        if ctypes.windll.kernel32.GetLastError() == 183:
            hwnd = ctypes.windll.user32.FindWindowW(
                None, "Nanoleaf Screen Mirror"
            )
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, 0x8001, 0, 0)
            sys.exit(0)
    except Exception:
        pass  # Non-Windows: 단일 인스턴스 미적용

    start_to_tray = "--startup" in sys.argv

    # === 6) Windows 앱 ID ===
    try:
        myappid = 'NanoleafMirror'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    # === 7) 아이콘 ===
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))

    icon_path = os.path.join(base_path, "assets", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # === 8) 기본 폰트 ===
    font = app.font()
    font.setPointSize(10)
    app.setFont(font)

    config = load_config()
    window = MainWindow(config)

    # === 9) 윈도우 크기 + 화면 중앙 배치 ===
    window.resize(740, 840)
    window.setMinimumSize(600, 700)

    screen = app.primaryScreen()
    if screen:
        screen_geo = screen.availableGeometry()
        x = (screen_geo.width() - window.width()) // 2
        y = (screen_geo.height() - window.height()) // 2
        window.move(max(0, x), max(0, y))

    if not start_to_tray:
        window.show()

    # === 10) 자동 시작 ===
    if config.get("options", {}).get("auto_start_mirror", False):
        default_mode = config.get("options", {}).get("default_mode", "mirror")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1000, lambda: window.start_engine(default_mode))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
