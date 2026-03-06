"""메인 윈도우 — 탭 구조 + 미러링 스레드 + 트레이 + 잠금 감지

[변경 사항 — 오디오 비주얼라이저 통합]
★ AudioTab import 추가
★ tab_audio 인스턴스 생성 + 탭 추가
★ tab_audio 시그널 연결
★ _start_mirror()에서 비주얼라이저 중지 추가
★ _shutdown()에서 tab_audio.cleanup() 추가
"""

import ctypes
import ctypes.wintypes
from PyQt5.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QSystemTrayIcon, QApplication
)
from PyQt5.QtCore import Qt, QAbstractNativeEventFilter, QTimer
from PyQt5.QtGui import QIcon

from core.device_manager import DeviceManager
from ui.tab_setup import SetupTab
from ui.tab_color import ColorTab
from ui.tab_mirror import MirrorTab
from ui.tab_options import OptionsTab
from ui.tab_audio import AudioTab       # ★ 추가
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

        # ★ DeviceManager 생성 — 앱 전체에서 단일 인스턴스
        self.device_manager = DeviceManager(config, parent=self)

        # --- 탭 ---
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # ★ DeviceManager를 각 탭에 주입
        self.tab_setup = SetupTab(config, device_manager=self.device_manager)
        self.tab_color = ColorTab(config, device_manager=self.device_manager)
        self.tab_mirror = MirrorTab(config)
        self.tab_options = OptionsTab(config, main_window=self)
        self.tab_audio = AudioTab(config)   # ★ 오디오 비주얼라이저 탭

        self.tabs.addTab(self.tab_mirror, "🖥 미러링")
        self.tabs.addTab(self.tab_audio, "🎵 오디오")  # ★ 미러링 탭 바로 다음
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
        # ★ 실시간 레이아웃 파라미터 변경
        self.tab_mirror.layout_params_changed.connect(
            self._on_layout_params_changed
        )
        # ★ 실시간 스무딩 계수 변경
        self.tab_mirror.smoothing_factor_changed.connect(
            self._on_smoothing_factor_changed
        )

        self.tab_setup.request_mirror_stop.connect(self._stop_mirror_sync)
        self.tab_color.request_mirror_stop.connect(self._stop_mirror_sync)
        self.tab_audio.request_mirror_stop.connect(self._stop_mirror_sync)  # ★ 추가

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
        # ★ DeviceManager로 강제 해제 — 모든 탭의 연결을 한 번에 정리
        self.device_manager.force_release()

        # ★ 오디오 비주얼라이저가 실행 중이면 먼저 중지
        self.tab_audio.stop_visualizer_sync()

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

    def _on_error(self, msg, severity="critical"):
        """★ 심각도별 에러 표시 분리

        - critical: 팝업(QMessageBox) — 미러링 시작 실패 등 사용자 개입 필요
        - warning: 상태바 + 탭 라벨에 표시 → 5초 후 자동 소멸
        """
        if severity == "critical":
            QMessageBox.warning(self, "오류", msg)
        else:
            # 상태바 + 탭에 경고 표시
            warning_text = f"⚠ {msg}"
            self.statusBar().showMessage(warning_text)
            self.tab_mirror.update_status(warning_text)
            self.tray.update_status(warning_text)

            # 5초 후 상태 메시지를 기본값으로 복원
            QTimer.singleShot(5000, self._restore_status_after_warning)

    def _restore_status_after_warning(self):
        """경고 메시지 자동 소멸 후 현재 상태에 맞는 메시지로 복원"""
        if self.mirror_thread and self.mirror_thread.isRunning():
            if self.mirror_thread._paused:
                text = "일시정지"
            else:
                text = "미러링 실행 중"
        else:
            text = "준비"
        self.statusBar().showMessage(text)
        self.tab_mirror.update_status(text)
        self.tray.update_status(text)

    def _on_brightness_changed(self, value):
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.brightness = value / 100.0

    def _on_smoothing_changed(self, state):
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.smoothing_enabled = bool(state)

    def _on_layout_params_changed(self, params):
        """★ 미러링 중 감쇠/페널티 변경 → MirrorThread에 전달"""
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.update_layout_params(
                decay_radius=params.get("decay_radius"),
                parallel_penalty=params.get("parallel_penalty"),
                decay_per_side=params.get("decay_per_side"),
                penalty_per_side=params.get("penalty_per_side"),
            )

    def _on_smoothing_factor_changed(self, value):
        """★ 미러링 중 스무딩 계수 변경 → MirrorThread에 직접 반영"""
        if self.mirror_thread and self.mirror_thread.isRunning():
            self.mirror_thread.smoothing_factor = value

    def _on_session_event(self, event):
        """잠금/해제 이벤트 처리 — turn_off_on_lock 옵션 반영"""
        opts = self.config.get("options", {})
        turn_off_enabled = opts.get("turn_off_on_lock", True)

        if event == "lock":
            if turn_off_enabled and self.mirror_thread and self.mirror_thread.isRunning():
                self._was_mirroring_before_lock = True
                self._stop_mirror()
                self.statusBar().showMessage("잠금 감지 — 미러링 중지")

        elif event == "unlock":
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
        self.tab_audio.cleanup()    # ★ 오디오 비주얼라이저 정리
        self.tab_color.cleanup()
        self.tab_setup.cleanup()
        # ★ DeviceManager 최종 정리
        self.device_manager.cleanup()
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
