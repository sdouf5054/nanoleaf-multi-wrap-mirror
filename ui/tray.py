"""시스템 트레이 아이콘 + 글로벌 핫키

[변경 사항 v2]
- keyboard.unhook_all() → 개별 remove_hotkey(handle)로 변경
  → 다른 라이브러리/프로세스가 등록한 핫키에 영향 없음
- 등록된 핫키 handle을 _hotkey_handles 리스트로 추적
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
    """시스템 트레이 아이콘 — 우클릭 메뉴 + 글로벌 핫키

    메뉴:
        - 밝기 (25/50/75/100%)
        - 일시정지/재개
        - 설정 열기
        - 종료
    글로벌 핫키 (config["options"]에서 읽음):
        - hotkey_toggle      : 미러링 On/Off  (기본: ctrl+shift+o)
        - hotkey_bright_up   : 밝기 +10%      (기본: ctrl+shift+up)
        - hotkey_bright_down : 밝기 -10%      (기본: ctrl+shift+down)
    """

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

        # ★ 등록된 핫키 handle 추적 리스트
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

        self.onoff_action = QAction("▶ 미러링 시작", menu)
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
        """config에서 핫키 문자열을 읽어 글로벌 핫키 등록."""
        if not HAS_KEYBOARD:
            return

        # ★ 기존에 등록한 핫키만 개별 해제
        self._clear_hotkeys()

        opts = self.main_window.config.get("options", {})
        if not opts.get("hotkey_enabled", True):
            return

        hk_toggle = opts.get("hotkey_toggle", "ctrl+shift+o")
        hk_up     = opts.get("hotkey_bright_up", "ctrl+shift+up")
        hk_down   = opts.get("hotkey_bright_down", "ctrl+shift+down")

        def _safe_add(hotkey_str, callback):
            """잘못된 키 문자열로 인한 ValueError를 흡수."""
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
        """★ 등록된 핫키만 개별 해제 — 다른 핫키에 영향 없음."""
        if not HAS_KEYBOARD:
            return
        for handle in self._hotkey_handles:
            try:
                keyboard.remove_hotkey(handle)
            except (ValueError, KeyError):
                # 이미 해제되었거나 유효하지 않은 handle — 무시
                pass
        self._hotkey_handles.clear()

    # ── 액션 ──────────────────────────────────────────────────────────

    def _toggle_onoff(self):
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
        """★ 앱 종료 시 등록된 핫키만 정리."""
        self._clear_hotkeys()
