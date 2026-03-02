"""메인 윈도우 — 탭 구조 + 미러링 스레드 + 트레이 + 잠금 감지"""

import ctypes
import ctypes.wintypes
from PyQt5.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QSystemTrayIcon, QApplication
)
from PyQt5.QtCore import Qt, QAbstractNativeEventFilter, QTimer
from PyQt5.QtGui import QIcon

from ui.tab_setup import SetupTab
from ui.tab_color import ColorTab
from ui.tab_mirror import MirrorTab
from ui.tab_options import OptionsTab
from ui.tray import SystemTray
from core.mirror import MirrorThread
from core.config import save_config

# Windows 메시지 상수
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8


class SessionEventFilter(QAbstractNativeEventFilter):
    """Windows 잠금/해제 이벤트 감지"""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_WTSSESSION_CHANGE:
                if msg.wParam == WTS_SESSION_LOCK:
                    self._callback("lock")
                elif msg.wParam == WTS_SESSION_UNLOCK:
                    self._callback("unlock")
        return False, 0


class MainWindow(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.mirror_thread = None
        self._force_quit = False
        self._has_shown_tray_message = False

        self.setWindowTitle("Nanoleaf Screen Mirror")
        self.setMinimumSize(700, 780)
        self.resize(740, 840)

        # --- 탭 ---
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tab_setup = SetupTab(config)
        self.tab_color = ColorTab(config)
        self.tab_mirror = MirrorTab(config)
        self.tab_options = OptionsTab(config, main_window=self)

        self.tabs.addTab(self.tab_mirror, "🖥 미러링")
        self.tabs.addTab(self.tab_color, "🎨 색상 보정")
        self.tabs.addTab(self.tab_setup, "⚙ LED 설정")
        self.tabs.addTab(self.tab_options, "🔧 옵션")

        # --- 상태바 ---
        self.statusBar().showMessage("준비")

        # --- 시스템 트레이 ---
        opts = config.get("options", {})
        self.tray = SystemTray(self)
        if QSystemTrayIcon.isSystemTrayAvailable() and opts.get("tray_enabled", True):
            self.tray.show()
        if not opts.get("hotkey_enabled", True):
            self.tray.cleanup()

        # --- 시그널 연결 ---
        self.tab_mirror.btn_start.clicked.connect(self._start_mirror)
        self.tab_mirror.btn_pause.clicked.connect(self._toggle_pause)
        self.tab_mirror.btn_stop.clicked.connect(self._stop_mirror)
        self.tab_mirror.brightness_slider.valueChanged.connect(
            self._on_brightness_changed
        )
        self.tab_mirror.chk_smoothing.stateChanged.connect(
            self._on_smoothing_changed
        )

        self.tab_setup.request_mirror_stop.connect(self._stop_mirror_sync)
        self.tab_color.request_mirror_stop.connect(self._stop_mirror_sync)

        # --- 잠금 감지 ---
        self._was_mirroring_before_lock = False
        self._session_filter = SessionEventFilter(self._on_session_event)
        QApplication.instance().installNativeEventFilter(self._session_filter)
        try:
            ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
                int(self.winId()), 0
            )
        except Exception:
            pass

    def nativeEvent(self, eventType, message):
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == 0x8001:
                self.showNormal()
                self.activateWindow()
                self.raise_()
                return True, 0
        except Exception:
            pass
        return super().nativeEvent(eventType, message)

    def _start_mirror(self):
        self.tab_setup.force_disconnect()
        self.tab_color.force_disconnect()

        self.tab_mirror.apply_to_config()
        save_config(self.config)

        self.mirror_thread = MirrorThread(self.config)
        self.mirror_thread.fps_updated.connect(self.tab_mirror.update_fps)
        self.mirror_thread.status_changed.connect(self._on_status_changed)
        self.mirror_thread.error.connect(self._on_error)
        self.mirror_thread.finished.connect(self._on_mirror_finished)

        self.tab_mirror.set_running_state(True)
        self.mirror_thread.start()

    def _toggle_pause(self):
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.toggle_pause()
            is_paused = self.mirror_thread._paused
            self.tab_mirror.btn_pause.setText(
                "▶ 재개" if is_paused else "⏸ 일시정지"
            )

    def _stop_mirror(self):
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.stop_mirror()
            self.tab_mirror.update_status("중지 중...")

    def _stop_mirror_sync(self):
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.stop_mirror()
            self.mirror_thread.wait(2000)
            self.tab_mirror.update_status("설정 모드 진입으로 중지됨")

    def _on_mirror_finished(self):
        self.tab_mirror.set_running_state(False)
        self.tab_mirror.btn_pause.setText("⏸ 일시정지")
        self.tab_mirror.fps_label.setText("— fps")
        self.tray.update_status("대기 중")
        self.tray.onoff_action.setText("▶ 미러링 시작")
        self.mirror_thread = None

    def _on_status_changed(self, text):
        self.tab_mirror.update_status(text)
        self.statusBar().showMessage(text)
        self.tray.update_status(text)

    def _on_error(self, msg):
        QMessageBox.warning(self, "오류", msg)

    def _on_brightness_changed(self, value):
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.brightness = value / 100.0

    def _on_smoothing_changed(self, state):
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.smoothing_enabled = bool(state)

    def _on_session_event(self, event):
        """잠금/해제 이벤트 처리 — turn_off_on_lock 옵션 반영"""
        opts = self.config.get("options", {})
        turn_off_enabled = opts.get("turn_off_on_lock", True)  # ★ 옵션값 확인

        if event == "lock":
            # ★ 옵션이 켜져 있을 때만 미러링 중지
            if turn_off_enabled and self.mirror_thread and self.mirror_thread.isRunning():
                self._was_mirroring_before_lock = True
                self._stop_mirror()
                self.statusBar().showMessage("잠금 감지 — 미러링 중지")
            # 옵션 꺼진 경우: 아무 동작 안 함 (미러링 계속 실행)

        elif event == "unlock":
            # ★ 옵션에 의해 중지된 경우에만 재시작
            if self._was_mirroring_before_lock:
                self._was_mirroring_before_lock = False
                QTimer.singleShot(3000, self._start_mirror)
                self.statusBar().showMessage("잠금 해제 — 3초 후 미러링 재시작")

    def closeEvent(self, event):
        if self._force_quit:
            self._shutdown()
            event.accept()
            return

        opts = self.config.get("options", {})
        minimize = opts.get("minimize_to_tray", True) and opts.get("tray_enabled", True)

        if (self.mirror_thread and self.mirror_thread.isRunning()
                and minimize and QSystemTrayIcon.isSystemTrayAvailable()):
            event.ignore()
            self.hide()

            if not self._has_shown_tray_message:
                self.tray.showMessage(
                    "Nanoleaf Mirror",
                    "트레이에서 실행 중입니다. 우클릭으로 제어하세요.",
                    self.tray.icon(),
                    2000
                )
                self._has_shown_tray_message = True
        else:
            self._shutdown()
            event.accept()

    def _shutdown(self):
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.stop_mirror()
            self.mirror_thread.wait(3000)
        self.tab_color.cleanup()
        self.tab_setup.cleanup()
        self.tray.cleanup()
        self.tray.hide()
        try:
            ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(
                int(self.winId())
            )
        except Exception:
            pass
        self.tab_mirror.apply_to_config()
        save_config(self.config)
        QApplication.instance().quit()
