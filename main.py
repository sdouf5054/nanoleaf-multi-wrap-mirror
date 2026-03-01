"""
Nanoleaf Screen Mirror — GUI 앱 진입점

사용법:
    python main.py
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
# System DPI Aware(1)로 고정:
#   - 주 모니터(125%) 기준 물리 해상도를 dxcam이 정확히 읽도록 OS 가상화 차단
#   - 보조 모니터(175%)로 창 이동 시 Windows가 비트맵 확대(~1.4배)로 처리
#     → 레이아웃 붕괴/멈춤 원천 차단 (다소 흐릿하게 보일 수 있음)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI Aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# === 3) 작업 디렉토리를 main.py 위치로 강제 설정 ===
# PyInstaller exe 실행 시 sys.executable 기준, 스크립트 실행 시 __file__ 기준
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
# PyQt5 자동 스케일링 비활성화:
#   - System DPI(1) 고정 상태에서 PyQt5까지 배율 적용 시 이중 계산 충돌 발생
#   - Windows가 비트맵 확대로 처리하므로 PyQt5는 개입하지 않도록 False 설정
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

    # GetLastError() == 183(ERROR_ALREADY_EXISTS): 이미 실행 중인 인스턴스 존재
    if ctypes.windll.kernel32.GetLastError() == 183:
        hwnd = ctypes.windll.user32.FindWindowW(None, "Nanoleaf Screen Mirror")
        if hwnd:
            # 사용자 정의 메시지(0x8001)로 기존 창에 복원 신호 전송
            ctypes.windll.user32.PostMessageW(hwnd, 0x8001, 0, 0)
        sys.exit(0)  # 새로 실행된 프로세스는 즉시 종료

    # === 6) Windows에 독립된 앱으로 인식시키기 (알림창 이름 & 아이콘 캐시 우회) ===
    try:
        myappid = 'NanoleafMirror'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)

    # 마지막 창이 닫혀도 프로그램이 종료되지 않도록 설정 (트레이 대기용)
    app.setQuitOnLastWindowClosed(False)

    app.setStyle("Fusion")

    # === 7) 프로그램 전체 아이콘 설정 (작업 표시줄, 알림창 등) ===
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))

    icon_path = os.path.join(base_path, "assets", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # === 8) 폰트 기본 크기 설정 ===
    # 수동 DPI 계산 제거 — PyQt5가 배율에 맞게 자동 조정
    font = app.font()
    font.setPointSize(10)
    app.setFont(font)

    config = load_config()
    window = MainWindow(config)

    # === 9) 윈도우 크기 — 논리 픽셀 기준으로만 지정 ===
    # PyQt5가 현재 모니터 배율(125%, 175% 등)에 따라 물리 픽셀로 자동 변환
    # (예: 740x840 @ 125% → 실제 925x1050, @ 175% → 실제 1295x1470)
    window.resize(740, 840)
    window.setMinimumSize(600, 700)

    # 화면 중앙에 배치
    screen = app.primaryScreen()
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
