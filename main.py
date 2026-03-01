"""
Nanoleaf Screen Mirror — GUI 앱 진입점

사용법:
    python main.py
"""

import sys
import os

# === 1) pythonw.exe 스트림 보호 (stdout/stderr 없을 때 크래시 방지) ===
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

# === 2) Windows DPI Awareness 설정 (QApplication 생성 전, 최우선) ===
# .lnk 실행 시 DPI 가상화로 2560x1440이 2048x1152로 보이는 문제 해결
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI Aware (모든 모니터 동일 스케일)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# === 3) 작업 디렉토리를 main.py 위치로 강제 설정 ===
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# === 4) Windows 콘솔 창 숨기기 (python.exe로 실행해도 콘솔 안 보임) ===
try:
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass

# === 5) Qt High DPI 설정 (QApplication 생성 전) ===
from PyQt5.QtCore import Qt, QCoreApplication
QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

from PyQt5.QtWidgets import QApplication
from core.config import load_config
from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # DPI 스케일 팩터 계산
    screen = app.primaryScreen()
    dpi = screen.logicalDotsPerInch()
    scale = dpi / 96.0  # 96 DPI = 100%

    # 폰트 크기를 DPI에 맞게 조정
    font = app.font()
    font.setPointSize(max(9, int(11 * scale)))
    app.setFont(font)

    config = load_config()
    window = MainWindow(config)

    # 윈도우 크기를 DPI에 맞게 조정
    base_w, base_h = 740, 840
    window.resize(int(base_w * scale), int(base_h * scale))
    window.setMinimumSize(int(600 * scale), int(700 * scale))

    # 화면 중앙에 배치
    screen_geo = screen.availableGeometry()
    x = (screen_geo.width() - window.width()) // 2
    y = (screen_geo.height() - window.height()) // 2
    window.move(max(0, x), max(0, y))

    window.show()

    # 실행 시 미러링 자동 시작
    if config.get("options", {}).get("auto_start_mirror", False):
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(1000, window._start_mirror)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
