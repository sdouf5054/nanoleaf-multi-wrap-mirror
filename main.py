"""
Nanoleaf Screen Mirror — GUI 앱 진입점

사용법:
    python main.py
    python main.py --startup   ← 트레이로 바로 시작 (창 숨김)
"""

import sys
import os
import ctypes

# === 1) pythonw.exe 스트림 보호 (stdout/stderr 없을 때 크래시 방지) ===
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

# === 2) Windows DPI Awareness 설정 (QApplication 생성 전, 최우선) ===
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI Aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# === 3) 작업 디렉토리를 main.py 위치로 강제 설정 ===
if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))
else:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

# === 4) Windows 콘솔 창 숨기기 (python.exe로 실행해도 콘솔 안 보임) ===
try:
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass

# === 5) Qt High DPI 설정 (QApplication 생성 전) ===
from PyQt5.QtCore import Qt, QCoreApplication
QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, False)
QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QIcon
from core.config import load_config
from ui.main_window import MainWindow


def main():
    # === 0) 중복 실행 방지 — Mutex로 기존 인스턴스 감지 후 창 복원 ===
    mutex_name = "nanoleaf_mirror_singleton_mutex"
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)

    if ctypes.windll.kernel32.GetLastError() == 183:
        hwnd = ctypes.windll.user32.FindWindowW(None, "Nanoleaf Screen Mirror")
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, 0x8001, 0, 0)
        sys.exit(0)

    # ★ --startup 인자 감지: 시작프로그램에서 실행 시 트레이로 바로 시작
    start_to_tray = "--startup" in sys.argv

    # === 6) Windows에 독립된 앱으로 인식시키기 ===
    try:
        myappid = 'NanoleafMirror'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)

    # 마지막 창이 닫혀도 프로그램이 종료되지 않도록 설정 (트레이 대기용)
    app.setQuitOnLastWindowClosed(False)

    app.setStyle("Fusion")

    # === 7) 프로그램 전체 아이콘 설정 ===
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))

    icon_path = os.path.join(base_path, "assets", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # === 8) 폰트 기본 크기 설정 ===
    font = app.font()
    font.setPointSize(10)
    app.setFont(font)

    config = load_config()
    window = MainWindow(config)

    # === 9) 윈도우 크기 ===
    window.resize(740, 840)
    window.setMinimumSize(600, 700)

    # 화면 중앙에 배치
    screen = app.primaryScreen()
    screen_geo = screen.availableGeometry()
    x = (screen_geo.width() - window.width()) // 2
    y = (screen_geo.height() - window.height()) // 2
    window.move(max(0, x), max(0, y))

    # ★ --startup 모드: 창을 표시하지 않고 트레이에서만 실행
    if start_to_tray:
        # 창을 숨긴 채로 시작 — 트레이 아이콘만 표시됨
        # (window.show()를 호출하지 않음)
        pass
    else:
        window.show()

    # 실행 시 미러링 자동 시작
    if config.get("options", {}).get("auto_start_mirror", False):
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(1000, window._start_mirror)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
