"""메인 윈도우 — 탭 구조 + UnifiedEngine + 트레이 + 잠금 감지

[변경] 하이브리드 캡처 통합
- ScreenSampler 관련 코드 제거
- 하이브리드 파라미터 전달 단순화
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
from ui.tab_control import ControlTab
from ui.tab_options import OptionsTab
from ui.tray import SystemTray
from core.engine import (
    UnifiedEngine, MODE_MIRROR, MODE_AUDIO, MODE_HYBRID,
)
from core.config import save_config

# Windows 메시지 상수
WM_WTSSESSION_CHANGE = 0x02B1
WM_DISPLAYCHANGE = 0x007E
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8


class SessionEventFilter(QAbstractNativeEventFilter):
    """Windows 잠금/해제 + 디스플레이 변경 이벤트 감지"""

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
            elif msg.message == WM_DISPLAYCHANGE:
                self._callback("display_change")
        return False, 0


class MainWindow(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self._engine = None
        self._force_quit = False
        self._has_shown_tray_message = False

        self.setWindowTitle("Nanoleaf Screen Mirror")
        self.setMinimumSize(700, 780)
        self.resize(740, 840)

        # DeviceManager
        self.device_manager = DeviceManager(config, parent=self)

        # --- 탭 ---
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tab_control = ControlTab(config)
        self.tab_color = ColorTab(config, device_manager=self.device_manager)
        self.tab_setup = SetupTab(config, device_manager=self.device_manager)
        self.tab_options = OptionsTab(config, main_window=self)

        self.tabs.addTab(self.tab_control, "🎛 컨트롤")
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

        # --- tab_control 시그널 연결 ---
        self.tab_control.request_engine_start.connect(self._start_engine)
        self.tab_control.request_engine_stop.connect(self._stop_engine)
        self.tab_control.request_engine_pause.connect(self._toggle_pause)
        self.tab_control.request_mode_switch.connect(self._switch_mode)
        self.tab_control.config_applied.connect(self._save_config)

        # 미러링 실시간 반영 시그널
        self.tab_control.mirror_brightness_changed.connect(
            self._on_mirror_brightness_changed
        )
        self.tab_control.mirror_smoothing_changed.connect(
            self._on_mirror_smoothing_changed
        )
        self.tab_control.mirror_smoothing_factor_changed.connect(
            self._on_mirror_smoothing_factor_changed
        )
        self.tab_control.mirror_layout_params_changed.connect(
            self._on_mirror_layout_params_changed
        )
        self.tab_control.mirror_zone_count_changed.connect(
            self._on_mirror_zone_count_changed
        )

        # 오디오/하이브리드 실시간 반영 시그널
        self.tab_control.audio_params_changed.connect(
            self._on_audio_params_changed
        )
        self.tab_control.audio_min_brightness_changed.connect(
            self._on_audio_min_brightness_changed
        )
        self.tab_control.hybrid_params_changed.connect(
            self._on_hybrid_params_changed
        )

        # setup/color 탭의 미러링 중지 요청
        self.tab_setup.request_mirror_stop.connect(self._stop_engine_sync)
        self.tab_color.request_mirror_stop.connect(self._stop_engine_sync)

        # --- 잠금 감지 + 디스플레이 변경 감지 ---
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

        # display change 디바운스 타이머
        self._display_change_timer = QTimer(self)
        self._display_change_timer.setSingleShot(True)
        self._display_change_timer.setInterval(1500)
        self._display_change_timer.timeout.connect(self._on_display_change_settled)

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

    # ══════════════════════════════════════════════════════════════
    #  엔진 관리
    # ══════════════════════════════════════════════════════════════

    def _cleanup_engine(self):
        if self._engine is None:
            return

        old = self._engine
        self._engine = None

        for sig_name in ("fps_updated", "status_changed", "error",
                         "finished", "energy_updated",
                         "spectrum_updated", "screen_colors_updated"):
            try:
                getattr(old, sig_name).disconnect()
            except (TypeError, RuntimeError, AttributeError):
                pass

        if old.isRunning():
            old.stop_engine()
            old.wait(3000)

    def _start_engine(self, mode=None):
        self._cleanup_engine()
        self.device_manager.force_release()

        if mode is None:
            mode = self.tab_control.current_mode

        engine = UnifiedEngine(
            self.config,
            audio_device_index=self.tab_control.get_audio_device_index(),
        )
        engine.mode = mode

        self._engine = engine
        params = self.tab_control.collect_engine_init_params()
        self._apply_params_to_engine(params)

        engine.fps_updated.connect(self.tab_control.update_fps)
        engine.status_changed.connect(self._on_status_changed)
        engine.error.connect(self._on_error)
        engine.finished.connect(self._on_engine_finished)

        engine.energy_updated.connect(self.tab_control.update_energy)
        engine.spectrum_updated.connect(self.tab_control.update_spectrum)
        engine.screen_colors_updated.connect(
            self.tab_control.update_preview_colors
        )

        self.tab_control.set_running_state(True)
        engine.start()

        self.tray.onoff_action.setText("⏹ 엔진 중지")
        self.tray.update_status(f"{mode} 실행 중")

    def _switch_mode(self, new_mode):
        self.tab_control.set_switching(True)
        try:
            self._start_engine(new_mode)
        finally:
            self.tab_control.set_switching(False)
            if self._engine and self._engine.isRunning():
                self.tab_control.set_running_state(True)
            else:
                self.tab_control.set_running_state(False)

    def _stop_engine(self):
        if self._engine and self._engine.isRunning():
            self._engine.stop_engine()
            self.tab_control.update_status("중지 중...")

    def _stop_engine_sync(self):
        self._cleanup_engine()
        self.tab_control.set_running_state(False)
        self.tab_control.update_status("설정 모드 진입으로 중지됨")

    def _toggle_pause(self):
        if self._engine and self._engine.isRunning():
            self._engine.toggle_pause()
            is_paused = self._engine._paused
            self.tab_control.update_pause_button(is_paused)

    def _on_engine_finished(self):
        if self._engine is None:
            return

        self.tab_control.set_running_state(False)
        self.tab_control.update_pause_button(False)
        self.tab_control.update_fps(0)
        self.tab_control.fps_label.setText("— fps")
        self.tray.update_status("대기 중")
        self.tray.onoff_action.setText("▶ 엔진 시작")
        self._engine = None

    # ══════════════════════════════════════════════════════════════
    #  엔진 파라미터 전달
    # ══════════════════════════════════════════════════════════════

    def _apply_params_to_engine(self, params):
        if not self._engine:
            return

        eng = self._engine
        mode = params.get("mode", eng.mode)

        if mode == MODE_MIRROR:
            eng.brightness = params.get("brightness", eng.brightness)
            eng.smoothing_enabled = params.get(
                "smoothing_enabled", eng.smoothing_enabled
            )
            eng.smoothing_factor = params.get(
                "smoothing_factor", eng.smoothing_factor
            )
            if "mirror_n_zones" in params:
                eng.mirror_n_zones = params["mirror_n_zones"]

        elif mode in (MODE_AUDIO, MODE_HYBRID):
            eng.audio_brightness = params.get("brightness", eng.audio_brightness)
            eng.bass_sensitivity = params.get(
                "bass_sensitivity", eng.bass_sensitivity
            )
            eng.mid_sensitivity = params.get(
                "mid_sensitivity", eng.mid_sensitivity
            )
            eng.high_sensitivity = params.get(
                "high_sensitivity", eng.high_sensitivity
            )
            eng.attack = params.get("attack", eng.attack)
            eng.release = params.get("release", eng.release)

            if "audio_min_brightness" in params:
                eng.audio_min_brightness = params["audio_min_brightness"]

            if "audio_mode" in params:
                eng.set_audio_mode(params["audio_mode"])
            if "zone_weights" in params:
                zw = params["zone_weights"]
                eng.set_zone_weights(*zw)
            if params.get("rainbow"):
                eng.set_rainbow(True)
            elif "base_color" in params:
                r, g, b = params["base_color"]
                eng.set_color(r, g, b)

            if mode == MODE_HYBRID:
                if "color_source" in params:
                    eng.set_color_source(
                        params["color_source"],
                        n_zones=params.get("n_zones"),
                    )
                if "min_brightness" in params:
                    eng.min_brightness = params["min_brightness"]
                    eng.audio_min_brightness = params["min_brightness"]

    # ══════════════════════════════════════════════════════════════
    #  미러링 실시간 반영 시그널 핸들러
    # ══════════════════════════════════════════════════════════════

    def _on_mirror_brightness_changed(self, value):
        if self._engine and self._engine.isRunning():
            self._engine.brightness = value / 100.0

    def _on_mirror_smoothing_changed(self, enabled):
        if self._engine and self._engine.isRunning():
            self._engine.smoothing_enabled = enabled

    def _on_mirror_smoothing_factor_changed(self, value):
        if self._engine and self._engine.isRunning():
            self._engine.smoothing_factor = value

    def _on_mirror_layout_params_changed(self, params):
        if self._engine and self._engine.isRunning():
            self._engine.update_layout_params(
                decay_radius=params.get("decay_radius"),
                parallel_penalty=params.get("parallel_penalty"),
                decay_per_side=params.get("decay_per_side"),
                penalty_per_side=params.get("penalty_per_side"),
            )

    def _on_mirror_zone_count_changed(self, n_zones):
        if self._engine and self._engine.isRunning():
            self._engine.mirror_n_zones = n_zones
            self._switch_mode(self.tab_control.current_mode)

    # ══════════════════════════════════════════════════════════════
    #  오디오/하이브리드 실시간 반영 시그널 핸들러
    # ══════════════════════════════════════════════════════════════

    def _on_audio_params_changed(self, params):
        if self._engine and self._engine.isRunning():
            params["mode"] = MODE_AUDIO
            self._apply_params_to_engine(params)

    def _on_audio_min_brightness_changed(self, value):
        if self._engine and self._engine.isRunning():
            self._engine.audio_min_brightness = value

    def _on_hybrid_params_changed(self, params):
        if self._engine and self._engine.isRunning():
            params["mode"] = MODE_HYBRID
            self._apply_params_to_engine(params)

    # ══════════════════════════════════════════════════════════════
    #  상태/에러 처리
    # ══════════════════════════════════════════════════════════════

    def _on_status_changed(self, text):
        self.tab_control.update_status(text)
        self.statusBar().showMessage(text)
        self.tray.update_status(text)

    def _on_error(self, msg, severity="critical"):
        if severity == "critical":
            QMessageBox.warning(self, "오류", msg)
        else:
            warning_text = f"⚠ {msg}"
            self.statusBar().showMessage(warning_text)
            self.tab_control.update_status(warning_text)
            self.tray.update_status(warning_text)
            QTimer.singleShot(5000, self._restore_status_after_warning)

    def _restore_status_after_warning(self):
        if self._engine and self._engine.isRunning():
            if self._engine._paused:
                text = "일시정지"
            else:
                text = "실행 중"
        else:
            text = "준비"
        self.statusBar().showMessage(text)
        self.tab_control.update_status(text)
        self.tray.update_status(text)

    def _save_config(self):
        save_config(self.config)

    # ══════════════════════════════════════════════════════════════
    #  잠금 감지 + 디스플레이 변경 감지
    # ══════════════════════════════════════════════════════════════

    def _on_session_event(self, event):
        if event == "display_change":
            self._display_change_timer.start()
            return

        opts = self.config.get("options", {})
        turn_off_enabled = opts.get("turn_off_on_lock", True)

        if event == "lock":
            if turn_off_enabled and self._engine and self._engine.isRunning():
                self._was_running_before_lock = True
                self._lock_restart_mode = self.tab_control.current_mode
                self._stop_engine()
                self.statusBar().showMessage("잠금 감지 — 엔진 중지")

        elif event == "unlock":
            if self._was_running_before_lock:
                self._was_running_before_lock = False
                mode = self._lock_restart_mode or MODE_MIRROR
                QTimer.singleShot(
                    3000, lambda: self._start_engine(mode)
                )
                self.statusBar().showMessage("잠금 해제 — 3초 후 재시작")

    def _on_display_change_settled(self):
        if self._engine and self._engine.isRunning():
            self._engine.on_display_changed()
            self.statusBar().showMessage("디스플레이 변경 감지 — 캡처 재초기화 중...")

    # ══════════════════════════════════════════════════════════════
    #  창 닫기 / 종료
    # ══════════════════════════════════════════════════════════════

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
        self._cleanup_engine()

        self.tab_control.cleanup()
        self.tab_color.cleanup()
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

        self.tab_control._apply_all_settings()
        save_config(self.config)
        QApplication.instance().quit()
