"""
Nanoleaf Screen Mirror — GUI 앱 진입점 (PySide6)

사용법:
    python main.py
    python main.py --startup   ← 트레이로 바로 시작 (창 숨김)

[ADR-028] Named Mutex 단일 인스턴스 (KEEP)
[ADR-029] PySide6 빌트인 High DPI 스케일링 (CHANGE → B)
  - SetProcessDpiAwareness + Qt 자동 스케일링을 PySide6에 위임
  - 수동 DPI 재조정 코드 40줄 제거

[Phase 7] auto_start: default_mode → 토글 기본값 기반으로 변경
[Phase 8] auto_start_mirror → auto_start_engine 키 이름 변경
         startup 모드 트레이 안정성 강화 (부팅 시 트레이 지연 대응)
"""

import sys
import os
import ctypes

# === 1) pythonw.exe 스트림 보호 ===
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

# === 2) DPI: PySide6가 자체 처리 (ADR-029) ===
# PySide6는 PER_MONITOR_AWARE_V2를 자동 설정.
# 중복 호출 경고를 억제하기 위해 환경 변수로 Qt 측 호출을 비활성화.
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

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
from PySide6.QtCore import Qt, qInstallMessageHandler, QtMsgType
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

def _qt_message_handler(mode, context, message):
    """무해한 Qt 경고를 필터링."""
    # DPI 중복 설정 경고 무시
    if "SetProcessDpiAwarenessContext" in message:
        return
    # QPainter 스타일 캐시 경고 무시 (Windows Fusion 스타일 + 스타일시트 조합에서
    # Qt가 내부 QPixmap 캐시에 그릴 때 발생하는 cosmetic 경고. 기능 영향 없음.)
    if "QPainter" in message:
        return
    # 나머지 메시지는 stderr로 출력
    import sys as _sys
    if mode == QtMsgType.QtWarningMsg:
        print(f"Qt Warning: {message}", file=_sys.stderr)
    elif mode == QtMsgType.QtCriticalMsg:
        print(f"Qt Critical: {message}", file=_sys.stderr)

qInstallMessageHandler(_qt_message_handler)

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

    # Ctrl+C 터미널 종료 지원
    # Qt 이벤트 루프가 Python SIGINT를 삼키므로, 명시적 핸들러 등록
    import signal
    signal.signal(signal.SIGINT, lambda *_: app.quit())

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

    # === 10) startup 모드: 트레이로 시작, 아니면 창 표시 ===
    if start_to_tray:
        # ★ 부팅 시 시스템 트레이 영역이 아직 준비 안 됐을 수 있으므로
        #    약간의 지연 후 트레이 아이콘이 정상 표시되는지 확인.
        #    창은 숨긴 상태로 유지.
        window.hide()
    else:
        window.show()

    # === 11) 자동 엔진 시작 (Phase 8: auto_start_engine) ===
    #   기본값 토글 설정을 바탕으로 엔진을 자동 시작합니다.
    #   startup 모드(부팅 시 자동 실행)이거나 옵션에서 활성화된 경우.
    auto_start = config.get("options", {}).get(
        "auto_start_engine",
        config.get("options", {}).get("auto_start_mirror", False)  # 구 키 폴백
    )
    if auto_start:
        # startup 모드에서는 OS 부팅 직후이므로 USB 디바이스 등이
        # 아직 준비되지 않았을 수 있어 더 긴 지연을 줌.
        delay = 3000 if start_to_tray else 1000
        from PySide6.QtCore import QTimer
        QTimer.singleShot(delay, lambda: window.start_engine())

    sys.exit(app.exec())


if __name__ == "__main__":
    main()