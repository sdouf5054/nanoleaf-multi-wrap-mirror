"""메인 윈도우 — 새 토글 기반 ControlTab + EngineController + 트레이 (Phase 4)

[변경 이력 — Phase 4]
- ControlTab의 request_mode_switch 시그널 연결
- start_engine: ControlTab.build_init_params_for_start() 사용
- _switch_mode: ControlTab에서 mode와 params를 한꺼번에 받음
- _on_tray_brightness_delta: master_brightness 슬라이더 동기화
- 기존 panel_mirror/panel_audio/panel_hybrid 직접 참조 제거
- _shutdown: 새 _CONTROL_TAB_KEYS에 맞게 스냅샷 저장

[Phase 8 변경]
- "미러링" 표현 → 모드 중립적 표현으로 교체
- auto_start_mirror → auto_start_engine 참조 변경
- 잠금 복귀 폴백 모드: "audio" → "unified"
- ★ _on_error: 창이 숨겨진 상태(트레이 모드)에서 QMessageBox 대신
  트레이 알림 사용 — QMessageBox가 부모 창을 자동 show하는 문제 방지

[미디어 연동 추가]
- _CONTROL_OPTION_KEYS에 "default_media_enabled" 추가
"""

import ctypes
import ctypes.wintypes
import copy

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QSystemTrayIcon, QApplication,
)
from PySide6.QtCore import Qt, QTimer, QAbstractNativeEventFilter
from PySide6.QtGui import QIcon

from core.config import save_config
from core.device_manager import DeviceManager
from core.engine_controller import EngineController
from core.engine_params import EngineParams
from ui.tray import SystemTray
from ui.tab_control import ControlTab
from ui.tab_color import ColorTab
from ui.tab_setup import SetupTab
from ui.tab_options import OptionsTab

# Windows 메시지 상수
WM_WTSSESSION_CHANGE = 0x02B1
WM_DISPLAYCHANGE = 0x007E
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007


class SessionEventFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == WM_WTSSESSION_CHANGE:
                    if msg.wParam == WTS_SESSION_LOCK:
                        self._callback("lock")
                    elif msg.wParam == WTS_SESSION_UNLOCK:
                        self._callback("unlock")
                elif msg.message == WM_DISPLAYCHANGE:
                    self._callback("display_change")
                elif msg.message == WM_POWERBROADCAST:
                    if msg.wParam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                        self._callback("session_resume")
            except Exception:
                pass
        return False, 0


class MainWindow(QMainWindow):
    """메인 윈도우 — 새 토글 기반 UI."""

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._force_quit = False

        self.setWindowTitle("Nanoleaf Screen Mirror")
        self.setMinimumSize(600, 700)

        # ── DeviceManager ──
        self.device_manager = DeviceManager(config, parent=self)

        # ── EngineController ──
        self.engine_ctrl = EngineController(config, parent=self)
        self._connect_engine_signals()

        # ── 탭 구조 ──
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tab_control = ControlTab(config, engine_ctrl=self.engine_ctrl)
        self.tab_control.set_engine_ctrl(self.engine_ctrl)
        self.tabs.addTab(self.tab_control, "컨트롤")

        self.tab_color = ColorTab(config, device_manager=self.device_manager)
        self.tab_setup = SetupTab(config, device_manager=self.device_manager)
        self.tab_options = OptionsTab(config, main_window=self)
        self.tabs.addTab(self.tab_color, "색상 보정")
        self.tabs.addTab(self.tab_setup, "LED 설정")
        self.tabs.addTab(self.tab_options, "옵션")

        self.tab_color.request_mirror_stop.connect(self._stop_engine_for_tab)
        self.tab_setup.request_mirror_stop.connect(self._stop_engine_for_tab)

        # ── ControlTab 시그널 연결 ──
        self.tab_control.request_engine_start.connect(self.start_engine)
        self.tab_control.request_engine_stop.connect(self.stop_engine)
        self.tab_control.request_engine_pause.connect(self._toggle_pause)
        self.tab_control.request_mode_switch.connect(self._switch_mode)
        self.tab_control.config_applied.connect(self._save_config)

        # ── EngineController → ControlTab 데이터 시그널 ──
        self.engine_ctrl.fps_updated.connect(self.tab_control.update_fps)
        self.engine_ctrl.energy_updated.connect(self.tab_control.update_energy)
        self.engine_ctrl.spectrum_updated.connect(self.tab_control.update_spectrum)
        self.engine_ctrl.screen_colors_updated.connect(self.tab_control.update_preview_colors)
        self.engine_ctrl.status_changed.connect(self.tab_control.update_status)

        # ── 상태바 ──
        self.statusBar().showMessage("준비")

        # ── 시스템 트레이 ──
        opts = config.get("options", {})
        self.tray = SystemTray(config, parent=self)
        self._connect_tray_signals()
        if (QSystemTrayIcon.isSystemTrayAvailable()
                and opts.get("tray_enabled", True)):
            self.tray.show()

        # ── 잠금 감지 + 디스플레이 변경 + 절전 복귀 ──
        self._was_running_before_lock = False
        self._lock_restart_mode = None
        self._session_filter = SessionEventFilter(self._on_session_event)
        QApplication.instance().installNativeEventFilter(self._session_filter)
        try:
            ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
                int(self.winId()), 0
            )
        except Exception:
            pass

        self._display_change_timer = QTimer(self)
        self._display_change_timer.setSingleShot(True)
        self._display_change_timer.setInterval(1500)
        self._display_change_timer.timeout.connect(self._on_display_change_settled)

        self._session_resume_timer = QTimer(self)
        self._session_resume_timer.setSingleShot(True)
        self._session_resume_timer.setInterval(3000)
        self._session_resume_timer.timeout.connect(self._on_session_resume_settled)

    # ══════════════════════════════════════════════════════════════
    #  시그널 연결
    # ══════════════════════════════════════════════════════════════

    def _connect_engine_signals(self):
        ctrl = self.engine_ctrl
        ctrl.status_changed.connect(self._on_status_changed)
        ctrl.error.connect(self._on_error)
        ctrl.running_changed.connect(self._on_running_changed)
        ctrl.engine_stopped.connect(self._on_engine_stopped)

    def _connect_tray_signals(self):
        self.tray.toggle_requested.connect(self._toggle_engine)
        self.tray.brightness_delta.connect(self._on_tray_brightness_delta)
        self.tray.brightness_set.connect(self._on_tray_brightness_set)
        self.tray.show_window_requested.connect(self._show_window)
        self.tray.quit_requested.connect(self._quit)

    # ══════════════════════════════════════════════════════════════
    #  엔진 제어
    # ══════════════════════════════════════════════════════════════

    def start_engine(self, mode=None):
        """엔진 시작."""
        self.device_manager.force_release()

        mode_str, engine_params = (
            self.tab_control.build_init_params_for_start()
        )
        if mode is not None:
            mode_str = mode

        # 오디오 디바이스 설정
        audio_dev = self.tab_control.get_audio_device_index()
        self.engine_ctrl.set_audio_device_index(audio_dev)

        self.engine_ctrl.start_engine(
            mode=mode_str,
            initial_params=engine_params,
        )

    def stop_engine(self):
        self.engine_ctrl.stop_engine()

    def _stop_engine_for_tab(self):
        """setup/color 탭이 USB를 사용하기 위해 엔진 중지."""
        self.engine_ctrl.stop_engine_sync()
        self.tab_control.set_running_state(False)
        self.tab_control.update_status("설정 모드 진입으로 중지됨")

    def _toggle_engine(self):
        if self.engine_ctrl.is_running:
            self.engine_ctrl.stop_engine()
        else:
            self.start_engine()

    def _switch_mode(self, new_mode):
        """실행 중 모드 전환 — 토글 변경/구역 수 변경 시."""
        self.device_manager.force_release()

        mode_str, engine_params = (
            self.tab_control.build_init_params_for_start()
        )
        # new_mode 우선 (토글에서 결정된 모드)
        if new_mode:
            mode_str = new_mode

        audio_dev = self.tab_control.get_audio_device_index()
        self.engine_ctrl.set_audio_device_index(audio_dev)

        self.engine_ctrl.start_engine(
            mode=mode_str,
            initial_params=engine_params,
        )

    def _toggle_pause(self):
        is_paused = self.engine_ctrl.toggle_pause()
        self.tab_control.update_pause_button(is_paused)

    # ══════════════════════════════════════════════════════════════
    #  트레이 밝기 — master_brightness 통합
    # ══════════════════════════════════════════════════════════════

    def _on_tray_brightness_delta(self, delta):
        """트레이 밝기 +/- → master_brightness 슬라이더 조절."""
        slider = self.tab_control.slider_master_brightness
        current = slider.value()
        new_val = max(0, min(100, current + delta))
        slider.setValue(new_val)

    def _on_tray_brightness_set(self, pct):
        """트레이 밝기 절대값."""
        self.tab_control.slider_master_brightness.setValue(pct)

    # ══════════════════════════════════════════════════════════════
    #  엔진 상태 콜백
    # ══════════════════════════════════════════════════════════════

    def _on_status_changed(self, text):
        self.statusBar().showMessage(text)
        self.tray.update_status(text)

    def _on_error(self, msg, severity="critical"):
        """엔진 에러 처리.

        ★ Phase 8: 창이 숨겨진 상태(트레이 모드)에서는 QMessageBox 대신
        트레이 알림을 사용. QMessageBox(parent=self)가 숨겨진 부모 창을
        자동으로 show하는 Qt 동작 때문에, 시작프로그램(--startup)으로
        트레이 실행 시 의도치 않게 창이 나타나는 문제를 방지.
        """
        if severity == "critical":
            if self.isVisible():
                # 창이 보이는 상태: 기존대로 QMessageBox 사용
                QMessageBox.warning(self, "오류", msg)
            else:
                # 창이 숨겨진 상태(트레이 모드): 트레이 알림으로 대체
                if self.tray.isVisible():
                    self.tray.showMessage(
                        "Nanoleaf Mirror — 오류",
                        msg,
                        QSystemTrayIcon.MessageIcon.Warning,
                        5000,
                    )
                # 상태바 + 트레이 상태 텍스트도 갱신
                self.statusBar().showMessage(f"⚠ {msg}")
                self.tray.update_status(f"⚠ {msg}")
        else:
            self.statusBar().showMessage(f"⚠ {msg}")
            self.tray.update_status(f"⚠ {msg}")
            QTimer.singleShot(5000, self._restore_status)

    def _on_running_changed(self, running):
        self.tray.set_engine_running(running)
        self.tab_control.set_running_state(running)

    def _on_engine_stopped(self):
        self.tray.update_status("대기 중")
        self.tab_control.update_fps(0)
        self.tab_control.update_pause_button(False)

    def _save_config(self):
        save_config(self.config)

    def _restore_status(self):
        text = "실행 중" if self.engine_ctrl.is_running else "준비"
        self.statusBar().showMessage(text)
        self.tray.update_status(text)

    # ══════════════════════════════════════════════════════════════
    #  잠금 감지 + 디스플레이 변경 + 절전 복귀
    # ══════════════════════════════════════════════════════════════

    def _on_session_event(self, event):
        if event == "display_change":
            self._display_change_timer.start()
            return

        if event == "session_resume":
            if not self._session_resume_timer.isActive():
                self._session_resume_timer.start()
            return

        opts = self.config.get("options", {})
        turn_off_enabled = opts.get("turn_off_on_lock", True)

        if event == "lock":
            if turn_off_enabled and self.engine_ctrl.is_running:
                self._was_running_before_lock = True
                self._lock_restart_mode = self.tab_control._get_engine_mode_string()
                self.engine_ctrl.stop_engine()
                self.statusBar().showMessage("잠금 감지 — 엔진 중지")

        elif event == "unlock":
            if self._was_running_before_lock:
                self._was_running_before_lock = False
                mode = self._lock_restart_mode or "unified"
                QTimer.singleShot(3000, lambda: self.start_engine(mode))
                self.statusBar().showMessage("잠금 해제 — 3초 후 재시작")

    def _on_display_change_settled(self):
        self.engine_ctrl.on_display_changed()
        self.statusBar().showMessage("디스플레이 변경 감지 — 캡처 재초기화 중...")

    def _on_session_resume_settled(self):
        if self.engine_ctrl.is_running:
            self.engine_ctrl.on_session_resume()
            self.statusBar().showMessage("절전 복귀 — USB 재연결 중...")

    # ══════════════════════════════════════════════════════════════
    #  단일 인스턴스
    # ══════════════════════════════════════════════════════════════

    def nativeEvent(self, eventType, message):
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == 0x8001:
                self._show_window()
                return True, 0
        except Exception:
            pass
        return super().nativeEvent(eventType, message)

    # ══════════════════════════════════════════════════════════════
    #  창 표시 / 닫기 / 종료
    # ══════════════════════════════════════════════════════════════

    def _show_window(self):
        self.show()
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _quit(self):
        self._force_quit = True
        self.close()

    def closeEvent(self, event):
        if self._force_quit:
            self._shutdown()
            event.accept()
            return

        opts = self.config.get("options", {})
        minimize = (opts.get("minimize_to_tray", True)
                    and opts.get("tray_enabled", True))

        if minimize and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
        else:
            self._shutdown()
            event.accept()

    def _shutdown(self):
        self.engine_ctrl.cleanup()
        if hasattr(self.tab_control, 'cleanup'):
            self.tab_control.cleanup()
        if hasattr(self.tab_color, 'cleanup'):
            self.tab_color.cleanup()
        if hasattr(self.tab_setup, 'cleanup'):
            self.tab_setup.cleanup()
        self.device_manager.cleanup()
        self.tray.cleanup()
        self.tray.hide()

        try:
            ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(
                int(self.winId())
            )
        except Exception:
            pass

        # ── 종료 시 config 저장 ──
        saved = self.tab_control.saved_config
        final = copy.deepcopy(self.config)

        _CONTROL_TAB_KEYS = (
            "mirror", "audio_pulse", "audio_spectrum", "audio_bass_detail",
            "audio_wave", "audio_dynamic", "audio_flowing",
        )
        for key in _CONTROL_TAB_KEYS:
            if key in saved:
                final[key] = copy.deepcopy(saved[key])

        # ★ default_media_enabled 추가
        _CONTROL_OPTION_KEYS = (
            "audio_state", "audio_device_index",
            "default_display_enabled", "default_audio_enabled",
            "default_media_enabled",
        )
        saved_opts = saved.get("options", {})
        final_opts = final.setdefault("options", {})
        for key in _CONTROL_OPTION_KEYS:
            if key in saved_opts:
                final_opts[key] = copy.deepcopy(saved_opts[key])

        final.setdefault("mirror", {})["master_brightness"] = (
            self.tab_control.slider_master_brightness.value() / 100.0
        )

        # ★ 구 키 정리
        final_opts.pop("default_mode", None)
        final_opts.pop("auto_start_mirror", None)

        save_config(final)
        QApplication.instance().quit()