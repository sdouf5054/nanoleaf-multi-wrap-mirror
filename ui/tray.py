"""시스템 트레이 아이콘 + 글로벌 핫키

[Step 12 변경]
- mirror_thread → _engine 참조 변경
- _start_mirror → _start_engine
- _stop_mirror → _stop_engine
- tab_mirror → tab_control 참조 변경
"""

import os
import sys
from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QAction
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import QTimer

try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False


class SystemTray(QSystemTrayIcon):
    """시스템 트레이 아이콘 — 우클릭 메뉴 + 글로벌 핫키"""

    def __init__(self, main_window, parent=None):
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
        else:
            base_path = os.path.dirname(os.path.dirname(__file__))

        icon_path = os.path.join(base_path, "assets", "icon.ico")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        super().__init__(icon, parent)
        self.main_window = main_window
        self.setToolTip("Nanoleaf Screen Mirror")

        self._hotkey_handles = []

        self._build_menu()
        self._setup_hotkey()

        self.activated.connect(self._on_activated)

    def _build_menu(self):
        menu = QMenu()

        self.status_action = QAction("대기 중", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()

        self.onoff_action = QAction("▶ 엔진 시작", menu)
        self.onoff_action.triggered.connect(self._toggle_onoff)
        menu.addAction(self.onoff_action)
        menu.addSeparator()

        bright_menu = QMenu("💡 밝기", menu)
        for pct in (25, 50, 75, 100):
            action = QAction(f"{pct}%", bright_menu)
            action.triggered.connect(lambda checked, p=pct: self._set_brightness(p))
            bright_menu.addAction(action)
        menu.addMenu(bright_menu)
        menu.addSeparator()

        show_action = QAction("⚙ 설정 열기", menu)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)
        menu.addSeparator()

        quit_action = QAction("❌ 종료", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _setup_hotkey(self):
        if not HAS_KEYBOARD:
            return

        self._clear_hotkeys()

        opts = self.main_window.config.get("options", {})
        if not opts.get("hotkey_enabled", True):
            return

        hk_toggle = opts.get("hotkey_toggle", "ctrl+shift+o")
        hk_up     = opts.get("hotkey_bright_up", "ctrl+shift+up")
        hk_down   = opts.get("hotkey_bright_down", "ctrl+shift+down")

        def _safe_add(hotkey_str, callback):
            if not hotkey_str.strip():
                return
            try:
                handle = keyboard.add_hotkey(
                    hotkey_str.strip(),
                    lambda: QTimer.singleShot(0, callback)
                )
                self._hotkey_handles.append(handle)
            except Exception as e:
                print(f"[핫키 등록 실패] '{hotkey_str}': {e}")

        _safe_add(hk_toggle, self._toggle_onoff)
        _safe_add(hk_up,     self._brightness_up)
        _safe_add(hk_down,   self._brightness_down)

    def _clear_hotkeys(self):
        if not HAS_KEYBOARD:
            return
        for handle in self._hotkey_handles:
            try:
                keyboard.remove_hotkey(handle)
            except (ValueError, KeyError):
                pass
        self._hotkey_handles.clear()

    # ── 액션 ──────────────────────────────────────────────────────

    def _toggle_onoff(self):
        mw = self.main_window
        if mw._engine and mw._engine.isRunning():
            mw._stop_engine()
            self.onoff_action.setText("▶ 엔진 시작")
            self.update_status("엔진 중지됨")
        else:
            mw._start_engine()
            self.onoff_action.setText("⏹ 엔진 중지")
            self.update_status("실행 중")

    def _brightness_up(self):
        mw = self.main_window
        val = min(100, mw.tab_control.mirror_brightness_slider.value() + 10)
        mw.tab_control.mirror_brightness_slider.setValue(val)
        if mw._engine and mw._engine.isRunning():
            mw._engine.brightness = val / 100.0

    def _brightness_down(self):
        mw = self.main_window
        val = max(0, mw.tab_control.mirror_brightness_slider.value() - 10)
        mw.tab_control.mirror_brightness_slider.setValue(val)
        if mw._engine and mw._engine.isRunning():
            mw._engine.brightness = val / 100.0

    def _set_brightness(self, pct):
        mw = self.main_window
        mw.tab_control.mirror_brightness_slider.setValue(pct)
        if mw._engine and mw._engine.isRunning():
            mw._engine.brightness = pct / 100.0

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
        self._clear_hotkeys()
