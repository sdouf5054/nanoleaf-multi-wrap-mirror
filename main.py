"""
Nanoleaf Screen Mirror — GUI 앱 진입점

사용법:
    python main.py
"""

import sys
import os

# 작업 디렉토리를 main.py 위치로 강제 설정
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Windows 콘솔 창 숨기기 (python.exe로 실행해도 콘솔 안 보임)
try:
    import ctypes
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass

from PyQt5.QtWidgets import QApplication
from core.config import load_config
from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 기본 폰트 크기 키우기
    font = app.font()
    font.setPointSize(11)
    app.setFont(font)

    config = load_config()
    window = MainWindow(config)
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
