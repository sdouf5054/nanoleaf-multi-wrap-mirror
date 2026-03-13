"""메인 윈도우 — 탭 구조 + EngineController + 트레이 + 잠금 감지 (PySide6)

[ADR-019] EngineController를 통해 엔진 관리 — MainWindow는 릴레이 슬롯 최소화
[ADR-029] DPI 수동 재조정 코드 제거 — PySide6 빌트인 스케일링에 위임
[ADR-030] WTSRegisterSessionNotification + NativeEventFilter (KEEP)
[ADR-031] Display change debouncing 1500ms (KEEP)
[ADR-039] 트레이 밝기를 시그널로 분리 — 위젯 직접 접근 제거

Phase 3에서는 탭 내부 위젯은 placeholder로 둡니다.
Phase 4/5에서 실제 패널을 구현합니다.
"""

import ctypes
import ctypes.wintypes

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QLabel,
    QMessageBox, QSystemTrayIcon, QApplication,
)
from PySide6.QtCore import Qt, QTimer, QAbstractNativeEventFilter
from PySide6.QtGui import QIcon

from core.config import save_config
from core.device_manager import DeviceManager
from core.engine_controller import EngineController
from core.engine_params import MirrorParams
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


class SessionEventFilter(QAbstractNativeEventFilter):
    """Windows 잠금/해제 + 디스플레이 변경 이벤트 감지."""

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
        self.tabs.addTab(self.tab_control, "🎛 컨트롤")

        # Phase 5: 실제 탭들
        self.tab_color = ColorTab(config, device_manager=self.device_manager)
        self.tab_setup = SetupTab(config, device_manager=self.device_manager)
        self.tab_options = OptionsTab(config, main_window=self)
        self.tabs.addTab(self.tab_color, "🎨 색상 보정")
        self.tabs.addTab(self.tab_setup, "⚙ LED 설정")
        self.tabs.addTab(self.tab_options, "🔧 옵션")

        # setup/color 탭의 미러링 중지 요청
        self.tab_color.request_mirror_stop.connect(self._stop_engine_for_tab)
        self.tab_setup.request_mirror_stop.connect(self._stop_engine_for_tab)

        # ── ControlTab 시그널 연결 ──
        self.tab_control.request_engine_start.connect(self.start_engine)
        self.tab_control.request_engine_stop.connect(self.stop_engine)
        self.tab_control.request_engine_pause.connect(self._toggle_pause)
        self.tab_control.request_mode_switch.connect(self._switch_mode)
        self.tab_control.config_applied.connect(self._save_config)

        # ── 상태바 ──
        self.statusBar().showMessage("준비")

        # ── 시스템 트레이 (ADR-033, ADR-039) ──
        opts = config.get("options", {})
        self.tray = SystemTray(config, parent=None)
        self._connect_tray_signals()

        if (QSystemTrayIcon.isSystemTrayAvailable()
                and opts.get("tray_enabled", True)):
            self.tray.show()

        # ── 잠금 감지 + 디스플레이 변경 (ADR-030, ADR-031) ──
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

    # ══════════════════════════════════════════════════════════════
    #  시그널 연결
    # ══════════════════════════════════════════════════════════════

    def _connect_engine_signals(self):
        ctrl = self.engine_ctrl
        ctrl.status_changed.connect(self._on_status_changed)
        ctrl.error.connect(self._on_error)
        ctrl.running_changed.connect(self._on_running_changed)
        ctrl.engine_stopped.connect(self._on_engine_stopped)

        # EngineController → ControlTab (데이터 시그널)
        ctrl.fps_updated.connect(lambda fps: self.tab_control.update_fps(fps)
                                 if hasattr(self, 'tab_control') else None)
        ctrl.energy_updated.connect(lambda b, m, h: self.tab_control.update_energy(b, m, h)
                                    if hasattr(self, 'tab_control') else None)
        ctrl.spectrum_updated.connect(lambda s: self.tab_control.update_spectrum(s)
                                      if hasattr(self, 'tab_control') else None)
        ctrl.screen_colors_updated.connect(lambda c: self.tab_control.update_preview_colors(c)
                                           if hasattr(self, 'tab_control') else None)

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
        self.engine_ctrl.start_engine(mode)

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
        """트레이 밝기 +/- 시그널 → EngineController로 전달."""
        if not self.engine_ctrl.is_running:
            return
        engine = self.engine_ctrl.engine
        if engine is None:
            return
        current = engine._current_mirror_params.brightness
        new_val = max(0.0, min(1.0, current + delta / 100.0))
        self.engine_ctrl.set_mirror_params(
            MirrorParams(brightness=new_val)
        )

    def _on_tray_brightness_set(self, pct):
        """트레이 밝기 절대값 시그널."""
        if not self.engine_ctrl.is_running:
            return
        self.engine_ctrl.set_mirror_params(
            MirrorParams(brightness=pct / 100.0)
        )

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
            self.engine_ctrl.switch_mode(new_mode)
        finally:
            if hasattr(self, 'tab_control') and hasattr(self.tab_control, 'set_switching'):
                self.tab_control.set_switching(False)

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
    #  ADR-030: 잠금 감지 + ADR-031: 디스플레이 변경
    # ══════════════════════════════════════════════════════════════

    def _on_session_event(self, event):
        if event == "display_change":
            self._display_change_timer.start()
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

        save_config(self.config)
        QApplication.instance().quit()
