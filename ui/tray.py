"""시스템 트레이 아이콘 + 글로벌 핫키"""

import os
from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QAction, QWidgetAction, QSlider, QLabel, QHBoxLayout, QWidget
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import Qt, QTimer

# 글로벌 핫키용
try:
    import keyboard  # pip install keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False


class SystemTray(QSystemTrayIcon):
    """시스템 트레이 아이콘 — 우클릭 메뉴 + 글로벌 핫키

    메뉴:
        - 밝기 (25/50/75/100%)
        - 일시정지/재개
        - 설정 열기
        - 종료
    글로벌 핫키:
        - Ctrl+Shift+P: 일시정지 토글
    """

    def __init__(self, main_window, parent=None):
        # 아이콘 로드
        icon_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "assets", "icon.png"
        )
        if os.path.exists(icon_path):
            icon = QIcon(icon_path)
        else:
            icon = QIcon()

        super().__init__(icon, parent)
        self.main_window = main_window
        self.setToolTip("Nanoleaf Screen Mirror")

        self._build_menu()
        self._setup_hotkey()

        # 트레이 아이콘 더블클릭 → 창 열기
        self.activated.connect(self._on_activated)

    def _build_menu(self):
        menu = QMenu()

        # 상태 표시
        self.status_action = QAction("대기 중", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()

        # 미러링 on/off
        self.onoff_action = QAction("▶ 미러링 시작", menu)
        self.onoff_action.triggered.connect(self._toggle_onoff)
        menu.addAction(self.onoff_action)
        menu.addSeparator()

        # 밝기
        bright_menu = QMenu("💡 밝기", menu)
        for pct in (25, 50, 75, 100):
            action = QAction(f"{pct}%", bright_menu)
            action.triggered.connect(lambda checked, p=pct: self._set_brightness(p))
            bright_menu.addAction(action)
        menu.addMenu(bright_menu)
        menu.addSeparator()

        # 설정 열기
        show_action = QAction("⚙ 설정 열기", menu)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        menu.addSeparator()

        # 종료
        quit_action = QAction("❌ 종료", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _setup_hotkey(self):
        """글로벌 핫키 등록: on/off, 밝기 조절"""
        if HAS_KEYBOARD:
            try:
                keyboard.unhook_all()
                keyboard.add_hotkey("ctrl+shift+o", lambda: QTimer.singleShot(0, self._toggle_onoff))
                keyboard.add_hotkey("ctrl+shift+up", lambda: QTimer.singleShot(0, self._brightness_up))
                keyboard.add_hotkey("ctrl+shift+down", lambda: QTimer.singleShot(0, self._brightness_down))
            except Exception:
                pass

    def _toggle_onoff(self):
        """미러링 시작/중지 토글"""
        mw = self.main_window
        if mw.mirror_thread and mw.mirror_thread.isRunning():
            mw._stop_mirror()
            self.onoff_action.setText("▶ 미러링 시작")
            self.update_status("미러링 중지됨")
        else:
            mw._start_mirror()
            self.onoff_action.setText("⏹ 미러링 중지")
            self.update_status("미러링 실행 중")

    def _brightness_up(self):
        mw = self.main_window
        val = min(100, mw.tab_mirror.brightness_slider.value() + 10)
        mw.tab_mirror.brightness_slider.setValue(val)
        if mw.mirror_thread and mw.mirror_thread.isRunning():
            mw.mirror_thread.brightness = val / 100.0

    def _brightness_down(self):
        mw = self.main_window
        val = max(0, mw.tab_mirror.brightness_slider.value() - 10)
        mw.tab_mirror.brightness_slider.setValue(val)
        if mw.mirror_thread and mw.mirror_thread.isRunning():
            mw.mirror_thread.brightness = val / 100.0

    def _set_brightness(self, pct):
        mw = self.main_window
        mw.tab_mirror.brightness_slider.setValue(pct)
        if mw.mirror_thread and mw.mirror_thread.isRunning():
            mw.mirror_thread.brightness = pct / 100.0

    def _show_window(self):
        self.main_window.show()
        self.main_window.activateWindow()
        self.main_window.raise_()

    def _quit(self):
        self.main_window._force_quit = True
        self.main_window.close()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_window()

    def update_status(self, text):
        self.status_action.setText(text)
        self.setToolTip(f"Nanoleaf Mirror — {text}")

    def cleanup(self):
        """핫키 해제"""
        if HAS_KEYBOARD:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
