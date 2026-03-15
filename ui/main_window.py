"""메인 윈도우 — 탭 구조 + EngineController + 트레이 + 잠금 감지 (PySide6)

[ADR-019] EngineController를 통해 엔진 관리 — MainWindow는 릴레이 슬롯 최소화
[ADR-029] DPI 수동 재조정 코드 제거 — PySide6 빌트인 스케일링에 위임
[ADR-030] WTSRegisterSessionNotification + NativeEventFilter (KEEP)
[ADR-031] Display change debouncing 1500ms (KEEP)
[ADR-039] 트레이 밝기를 시그널로 분리 — 위젯 직접 접근 제거
[ADR-042] 종료 시 saved_config(💾 스냅샷)을 디스크에 기록 — 미저장 변경 유실

[변경] 절전모드 복귀 대응:
- WM_POWERBROADCAST + PBT_APMRESUMEAUTOMATIC 감지
- SessionEventFilter에서 "session_resume" 이벤트 발생
- _on_session_event에서 engine_ctrl.on_session_resume() 호출
- 3초 디바운스로 중복 복귀 이벤트 방지
"""

import ctypes
import ctypes.wintypes

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QSystemTrayIcon, QApplication,
)
from PySide6.QtCore import Qt, QTimer, QAbstractNativeEventFilter
from PySide6.QtGui import QIcon

from core.config import save_config
from core.device_manager import DeviceManager
from core.engine_controller import EngineController
from core.engine_params import MirrorParams, AudioParams
from core.engine_utils import MODE_MIRROR
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

# ★ 절전모드 복귀 상수
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007


class SessionEventFilter(QAbstractNativeEventFilter):
    """Windows 잠금/해제 + 디스플레이 변경 + 절전 복귀 이벤트 감지."""

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
                # ★ 절전모드 복귀 감지
                elif msg.message == WM_POWERBROADCAST:
                    if msg.wParam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                        self._callback("session_resume")
            except Exception:
                pass
        return False, 0


class MainWindow(QMainWindow):
    """메인 윈도우 — UI Shell.

    EngineController가 엔진 수명주기를 관리하므로,
    MainWindow는 OS 이벤트 처리와 탭/트레이 연결만 담당합니다.
    """

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._force_quit = False

        self.setWindowTitle("Nanoleaf Screen Mirror")
        self.setMinimumSize(600, 700)

        # ── DeviceManager ──
        self.device_manager = DeviceManager(config, parent=self)

        # ── EngineController (ADR-019) ──
        self.engine_ctrl = EngineController(config, parent=self)
        self._connect_engine_signals()

        # ── 탭 구조 ──
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Phase 4: 실제 ControlTab
        self.tab_control = ControlTab(config, engine_ctrl=self.engine_ctrl)
        self.tab_control.set_engine_ctrl(self.engine_ctrl)
        self.tabs.addTab(self.tab_control, "컨트롤")

        # Phase 5: 실제 탭들
        self.tab_color = ColorTab(config, device_manager=self.device_manager)
        self.tab_setup = SetupTab(config, device_manager=self.device_manager)
        self.tab_options = OptionsTab(config, main_window=self)
        self.tabs.addTab(self.tab_color, "색상 보정")
        self.tabs.addTab(self.tab_setup, "LED 설정")
        self.tabs.addTab(self.tab_options, "옵션")

        # setup/color 탭의 미러링 중지 요청
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

        # ── 시스템 트레이 (ADR-033, ADR-039) ──
        opts = config.get("options", {})
        self.tray = SystemTray(config, parent=self)
        self._connect_tray_signals()

        if (QSystemTrayIcon.isSystemTrayAvailable()
                and opts.get("tray_enabled", True)):
            self.tray.show()

        # ── 잠금 감지 + 디스플레이 변경 + 절전 복귀 (ADR-030, ADR-031) ──
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

        # ADR-031: display change 디바운스 1500ms
        self._display_change_timer = QTimer(self)
        self._display_change_timer.setSingleShot(True)
        self._display_change_timer.setInterval(1500)
        self._display_change_timer.timeout.connect(self._on_display_change_settled)

        # ★ 절전 복귀 디바운스 3000ms — 중복 이벤트 방지
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
        """[ADR-039] 트레이 시그널 → MainWindow 슬롯."""
        self.tray.toggle_requested.connect(self._toggle_engine)
        self.tray.brightness_delta.connect(self._on_tray_brightness_delta)
        self.tray.brightness_set.connect(self._on_tray_brightness_set)
        self.tray.show_window_requested.connect(self._show_window)
        self.tray.quit_requested.connect(self._quit)

    # ══════════════════════════════════════════════════════════════
    #  엔진 제어 (EngineController에 위임)
    # ══════════════════════════════════════════════════════════════

    def start_engine(self, mode=None):
        """엔진 시작 — 외부(main.py 자동시작, 트레이)에서 호출 가능."""
        self.device_manager.force_release()

        # UI에서 현재 파라미터 수집
        initial_mirror = None
        initial_audio = None
        if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'collect_engine_init_params'):
            params = self.tab_control.collect_engine_init_params()
            effective_mode = mode or self.tab_control.current_mode

            if effective_mode == MODE_MIRROR:
                initial_mirror = MirrorParams(
                    brightness=params.get("brightness", 1.0),
                    smoothing_enabled=params.get("smoothing_enabled", True),
                    smoothing_factor=params.get("smoothing_factor", 0.5),
                    mirror_n_zones=params.get("mirror_n_zones", -1),
                )
            else:
                # audio/hybrid 공통
                filtered = {k: v for k, v in params.items()
                            if k in AudioParams.__dataclass_fields__}
                if filtered:
                    initial_audio = AudioParams(**filtered)

        self.engine_ctrl.start_engine(
            mode=mode,
            initial_mirror_params=initial_mirror,
            initial_audio_params=initial_audio,
        )

    def stop_engine(self):
        self.engine_ctrl.stop_engine()

    def _stop_engine_for_tab(self):
        """setup/color 탭이 USB 디바이스를 사용하기 위해 엔진 중지 요청."""
        self.engine_ctrl.stop_engine_sync()
        if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'set_running_state'):
            self.tab_control.set_running_state(False)
            self.tab_control.update_status("설정 모드 진입으로 중지됨")

    def _toggle_engine(self):
        """트레이 on/off 토글."""
        if self.engine_ctrl.is_running:
            self.engine_ctrl.stop_engine()
        else:
            self.start_engine()

    # ══════════════════════════════════════════════════════════════
    #  ADR-039: 트레이 밝기 시그널 처리
    # ══════════════════════════════════════════════════════════════

    def _on_tray_brightness_delta(self, delta):
        """트레이 밝기 +/- → 모드별 적절한 밝기 조절.

        미러링: brightness 조절
        오디오/하이브리드: min_brightness 조절
        """
        if not self.engine_ctrl.is_running:
            return
        engine = self.engine_ctrl.engine
        if engine is None:
            return

        mode = self.engine_ctrl.current_mode
        if mode == MODE_MIRROR:
            current = engine._current_mirror_params.brightness
            new_val = max(0.0, min(1.0, current + delta / 100.0))
            # 엔진 파라미터 갱신
            mp = engine._current_mirror_params
            self.engine_ctrl.set_mirror_params(MirrorParams(
                brightness=new_val,
                smoothing_enabled=mp.smoothing_enabled,
                smoothing_factor=mp.smoothing_factor,
                mirror_n_zones=mp.mirror_n_zones,
            ))
            # UI 슬라이더 동기화
            self.tab_control.panel_mirror.brightness_slider.blockSignals(True)
            self.tab_control.panel_mirror.brightness_slider.setValue(int(new_val * 100))
            self.tab_control.panel_mirror.brightness_slider.blockSignals(False)
            self.tab_control.panel_mirror.brightness_label.setText(f"{int(new_val * 100)}%")
        else:
            # 오디오/하이브리드: min_brightness 조절
            current = engine._current_audio_params.min_brightness
            new_val = max(0.0, min(1.0, current + delta / 100.0))
            ap = engine._current_audio_params
            self.engine_ctrl.set_audio_params(AudioParams(
                audio_mode=ap.audio_mode, brightness=ap.brightness,
                min_brightness=new_val,
                bass_sensitivity=ap.bass_sensitivity,
                mid_sensitivity=ap.mid_sensitivity,
                high_sensitivity=ap.high_sensitivity,
                attack=ap.attack, release=ap.release,
                input_smoothing=ap.input_smoothing,
                zone_weights=ap.zone_weights, rainbow=ap.rainbow,
                base_color=ap.base_color, color_source=ap.color_source,
                n_zones=ap.n_zones,
            ))
            # UI 슬라이더 + 라벨 동기화
            pct = int(new_val * 100)
            if mode == "hybrid":
                panel = self.tab_control.panel_hybrid
            else:
                panel = self.tab_control.panel_audio
            panel.slider_min_brightness.blockSignals(True)
            panel.slider_min_brightness.setValue(pct)
            panel.slider_min_brightness.blockSignals(False)
            panel.lbl_min_brightness.setText(f"{pct}%")

    def _on_tray_brightness_set(self, pct):
        """트레이 밝기 절대값 시그널."""
        if not self.engine_ctrl.is_running:
            return
        mode = self.engine_ctrl.current_mode
        if mode == MODE_MIRROR:
            self.engine_ctrl.set_mirror_params(
                MirrorParams(brightness=pct / 100.0)
            )
            self.tab_control.panel_mirror.brightness_slider.blockSignals(True)
            self.tab_control.panel_mirror.brightness_slider.setValue(pct)
            self.tab_control.panel_mirror.brightness_slider.blockSignals(False)
            self.tab_control.panel_mirror.brightness_label.setText(f"{pct}%")
        else:
            engine = self.engine_ctrl.engine
            if engine is None:
                return
            ap = engine._current_audio_params
            self.engine_ctrl.set_audio_params(AudioParams(
                audio_mode=ap.audio_mode, brightness=ap.brightness,
                min_brightness=pct / 100.0,
                bass_sensitivity=ap.bass_sensitivity,
                mid_sensitivity=ap.mid_sensitivity,
                high_sensitivity=ap.high_sensitivity,
                attack=ap.attack, release=ap.release,
                input_smoothing=ap.input_smoothing,
                zone_weights=ap.zone_weights, rainbow=ap.rainbow,
                base_color=ap.base_color, color_source=ap.color_source,
                n_zones=ap.n_zones,
            ))
            # UI 슬라이더 + 라벨 동기화
            if mode == "hybrid":
                panel = self.tab_control.panel_hybrid
            else:
                panel = self.tab_control.panel_audio
            panel.slider_min_brightness.blockSignals(True)
            panel.slider_min_brightness.setValue(pct)
            panel.slider_min_brightness.blockSignals(False)
            panel.lbl_min_brightness.setText(f"{pct}%")

    # ══════════════════════════════════════════════════════════════
    #  엔진 상태 콜백
    # ══════════════════════════════════════════════════════════════

    def _on_status_changed(self, text):
        self.statusBar().showMessage(text)
        self.tray.update_status(text)

    def _on_error(self, msg, severity="critical"):
        if severity == "critical":
            QMessageBox.warning(self, "오류", msg)
        else:
            self.statusBar().showMessage(f"⚠ {msg}")
            self.tray.update_status(f"⚠ {msg}")
            QTimer.singleShot(5000, self._restore_status)

    def _on_running_changed(self, running):
        self.tray.set_engine_running(running)
        if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'set_running_state'):
            self.tab_control.set_running_state(running)

    def _on_engine_stopped(self):
        self.tray.update_status("대기 중")
        if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'update_fps'):
            self.tab_control.update_fps(0)
            self.tab_control.update_pause_button(False)

    def _toggle_pause(self):
        is_paused = self.engine_ctrl.toggle_pause()
        if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'update_pause_button'):
            self.tab_control.update_pause_button(is_paused)

    def _switch_mode(self, new_mode):
        if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'set_switching'):
            self.tab_control.set_switching(True)
        try:
            self.device_manager.force_release()
            # 초기 파라미터 수집
            initial_mirror = None
            initial_audio = None
            if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'collect_engine_init_params'):
                params = self.tab_control.collect_engine_init_params()
                if new_mode == MODE_MIRROR:
                    initial_mirror = MirrorParams(
                        brightness=params.get("brightness", 1.0),
                        smoothing_enabled=params.get("smoothing_enabled", True),
                        smoothing_factor=params.get("smoothing_factor", 0.5),
                        mirror_n_zones=params.get("mirror_n_zones", -1),
                    )
                else:
                    filtered = {k: v for k, v in params.items()
                                if k in AudioParams.__dataclass_fields__}
                    if filtered:
                        initial_audio = AudioParams(**filtered)

            self.engine_ctrl.start_engine(
                mode=new_mode,
                initial_mirror_params=initial_mirror,
                initial_audio_params=initial_audio,
            )
        finally:
            if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'set_switching'):
                self.tab_control.set_switching(False)
                if self.engine_ctrl.is_running:
                    self.tab_control.set_running_state(True)

    def _save_config(self):
        save_config(self.config)

    def _restore_status(self):
        if self.engine_ctrl.is_running:
            text = "실행 중"
        else:
            text = "준비"
        self.statusBar().showMessage(text)
        self.tray.update_status(text)

    # ══════════════════════════════════════════════════════════════
    #  ADR-030: 잠금 감지 + ADR-031: 디스플레이 변경 + 절전 복귀
    # ══════════════════════════════════════════════════════════════

    def _on_session_event(self, event):
        if event == "display_change":
            self._display_change_timer.start()
            return

        # ★ 절전모드 복귀 — 디바운스 후 엔진에 전달
        if event == "session_resume":
            if not self._session_resume_timer.isActive():
                self._session_resume_timer.start()
            return

        opts = self.config.get("options", {})
        turn_off_enabled = opts.get("turn_off_on_lock", True)

        if event == "lock":
            if turn_off_enabled and self.engine_ctrl.is_running:
                self._was_running_before_lock = True
                self._lock_restart_mode = self.engine_ctrl.current_mode
                self.engine_ctrl.stop_engine()
                self.statusBar().showMessage("잠금 감지 — 엔진 중지")

        elif event == "unlock":
            if self._was_running_before_lock:
                self._was_running_before_lock = False
                mode = self._lock_restart_mode or MODE_MIRROR
                QTimer.singleShot(
                    3000, lambda: self.start_engine(mode)
                )
                self.statusBar().showMessage("잠금 해제 — 3초 후 재시작")

    def _on_display_change_settled(self):
        """ADR-031: 1500ms 디바운스 후 디스플레이 변경 처리."""
        self.engine_ctrl.on_display_changed()
        self.statusBar().showMessage("디스플레이 변경 감지 — 캡처 재초기화 중...")

    def _on_session_resume_settled(self):
        """★ 3000ms 디바운스 후 절전 복귀 처리.

        엔진이 실행 중이면 세션 복귀 플래그를 전달하여
        USB 강제 재연결 + 캡처 재초기화를 트리거합니다.
        잠금으로 인해 엔진이 중지된 상태라면 unlock 이벤트가
        별도로 재시작을 처리하므로 여기서는 무시합니다.
        """
        if self.engine_ctrl.is_running:
            self.engine_ctrl.on_session_resume()
            self.statusBar().showMessage("절전 복귀 — USB 재연결 중...")

    # ══════════════════════════════════════════════════════════════
    #  ADR-028: 단일 인스턴스 — 기존 창 복원
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

        # [ADR-042] 컨트롤 탭의 💾 스냅샷에서 해당 키만 복원하여 저장.
        # 다른 탭(옵션, 색상 보정, LED 설정)이 자체 저장한 값은 유지.
        import copy
        saved = self.tab_control.saved_config
        final = copy.deepcopy(self.config)

        # 컨트롤 탭이 관리하는 키만 스냅샷으로 덮어쓰기
        _CONTROL_TAB_KEYS = ("mirror", "audio_pulse", "audio_spectrum", "audio_bass_detail", "audio_wave", "audio_dynamic")
        for key in _CONTROL_TAB_KEYS:
            if key in saved:
                final[key] = copy.deepcopy(saved[key])

        # options 내 컨트롤 탭 관련 항목만 스냅샷으로 복원
        _CONTROL_OPTION_KEYS = (
            "audio_state", "hybrid_state", "audio_device_index", "default_mode",
        )
        saved_opts = saved.get("options", {})
        final_opts = final.setdefault("options", {})
        for key in _CONTROL_OPTION_KEYS:
            if key in saved_opts:
                final_opts[key] = copy.deepcopy(saved_opts[key])
            elif key in final_opts:
                del final_opts[key]

        save_config(final)
        QApplication.instance().quit()