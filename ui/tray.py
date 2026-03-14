"""시스템 트레이 아이콘 + 글로벌 핫키 (PySide6)

[ADR-033] keyboard 라이브러리 유지 — QTimer.singleShot(0)으로 스레드 마샬링
[ADR-039] 트레이 밝기 조절을 시그널로 분리 — tab 내부 위젯 직접 접근 제거

[FIX] 우클릭 시 간헐적 크래시 수정:
- QMenu를 인스턴스 변수(_menu)로 명시 보관 → GC 방지
- QAction들도 menu의 child로 생성 → 소유권 체인 확보
- cleanup()에서 menu를 명시적으로 정리

Signals:
    toggle_requested(): 엔진 on/off 토글 요청
    brightness_delta(int): 밝기 변경 요청 (+10 또는 -10)
    show_window_requested(): 설정 창 표시 요청
    quit_requested(): 앱 종료 요청
"""

import os
import sys

from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction
from PySide6.QtCore import QTimer, Signal, QObject

try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False


class SystemTray(QSystemTrayIcon):
    """시스템 트레이 아이콘 — 우클릭 메뉴 + 글로벌 핫키.

    [ADR-039] MainWindow의 내부 위젯에 직접 접근하지 않음.
    모든 액션은 시그널로 전달.
    """

    # ── 시그널 (MainWindow가 연결) ──
    toggle_requested = Signal()
    brightness_delta = Signal(int)
    brightness_set = Signal(int)
    show_window_requested = Signal()
    quit_requested = Signal()

    def __init__(self, config, parent=None):
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
        else:
            base_path = os.path.dirname(os.path.dirname(__file__))

        icon_path = os.path.join(base_path, "assets", "icon.ico")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        super().__init__(icon, parent)
        self._config = config
        self.setToolTip("Nanoleaf Screen Mirror")

        self._hotkey_handles = []
        self._menu = None  # ★ 인스턴스 변수로 선언 — GC 방지
        self._build_menu()
        self.setup_hotkeys()
        self.activated.connect(self._on_activated)

    def _build_menu(self):
        # ★ QMenu를 인스턴스 변수로 유지 — setContextMenu()만으로는
        #    Python 측 참조가 사라질 수 있어 GC 대상이 됨
        menu = QMenu()
        self._menu = menu

        self.status_action = QAction("대기 중", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()

        self.onoff_action = QAction("엔진 시작", menu)
        self.onoff_action.triggered.connect(self.toggle_requested.emit)
        menu.addAction(self.onoff_action)
        menu.addSeparator()

        bright_menu = QMenu("밝기", menu)
        for pct in (25, 50, 75, 100):
            action = QAction(f"{pct}%", bright_menu)
            action.triggered.connect(
                lambda checked, p=pct: self.brightness_set.emit(p)
            )
            bright_menu.addAction(action)
        menu.addMenu(bright_menu)
        menu.addSeparator()

        show_action = QAction("설정 열기", menu)
        show_action.triggered.connect(self.show_window_requested.emit)
        menu.addAction(show_action)
        menu.addSeparator()

        quit_action = QAction("종료", menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    # ── 핫키 (ADR-033: keyboard 라이브러리 유지) ─────────────────

    def setup_hotkeys(self):
        """핫키 등록 (옵션에서 변경 후 재호출 가능)."""
        if not HAS_KEYBOARD:
            return

        self._clear_hotkeys()

        opts = self._config.get("options", {})
        if not opts.get("hotkey_enabled", True):
            return

        hk_toggle = opts.get("hotkey_toggle", "ctrl+shift+o")
        hk_up = opts.get("hotkey_bright_up", "ctrl+shift+up")
        hk_down = opts.get("hotkey_bright_down", "ctrl+shift+down")

        def _safe_add(hotkey_str, callback):
            if not hotkey_str.strip():
                return
            try:
                handle = keyboard.add_hotkey(
                    hotkey_str.strip(), callback, suppress=False
                )
                self._hotkey_handles.append(handle)
            except Exception as e:
                print(f"[핫키 등록 실패] '{hotkey_str}': {e}")

        # Qt Signal.emit()은 cross-thread safe (queued connection 자동 적용).
        # keyboard 스레드에서 직접 emit해도 안전.
        _safe_add(hk_toggle, self.toggle_requested.emit)
        _safe_add(hk_up, lambda: self.brightness_delta.emit(10))
        _safe_add(hk_down, lambda: self.brightness_delta.emit(-10))

    def _clear_hotkeys(self):
        if not HAS_KEYBOARD:
            return
        for handle in self._hotkey_handles:
            try:
                keyboard.remove_hotkey(handle)
            except (ValueError, KeyError):
                pass
        self._hotkey_handles.clear()

    # ── UI 업데이트 (MainWindow에서 호출) ────────────────────────

    def update_status(self, text):
        self.status_action.setText(text)
        self.setToolTip(f"Nanoleaf Mirror — {text}")

    def set_engine_running(self, running):
        if running:
            self.onoff_action.setText("엔진 중지")
        else:
            self.onoff_action.setText("엔진 시작")

    # ── 이벤트 ───────────────────────────────────────────────────

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window_requested.emit()

    def cleanup(self):
        self._clear_hotkeys()
        # ★ 메뉴 명시적 정리 — deleteLater()로 안전하게 해제
        if self._menu is not None:
            self._menu.deleteLater()
            self._menu = None