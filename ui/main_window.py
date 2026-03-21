"""메인 윈도우 — 새 토글 기반 ControlTab + EngineController + 트레이 + ★ 컴팩트 뷰

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

[★ 프리셋 기능 추가]
- _shutdown: auto-save 방식 (스냅샷 불필요, config 직접 저장)
- _save_config: sync + save
- 트레이 프리셋 시그널 연결
- _refresh_tray_presets: 프리셋 목록 변경 시 트레이 메뉴 갱신
- config_applied → _on_config_applied (프리셋 저장/삭제 시 트레이 갱신 포함)

[Hotfix] startup 모드에서 창이 뜨는 문제 수정
  - ★ start_hidden 플래그 추가

[★ 컴팩트 뷰 추가]
- CompactWindow + CompactBridge 인스턴스 관리
- _connect_reverse_sync: ControlTab → CompactWindow 역방향 동기화
- toggle_compact_view / _position_compact_window: 열기/닫기 + 위치 배치
- 트레이 compact_view_requested 시그널 연결
- _switch_mode / _refresh_tray_presets에 컴팩트 동기화 추가
- _shutdown에 bridge cleanup 추가
"""

import ctypes
import ctypes.wintypes
import copy

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QSystemTrayIcon, QApplication,
    QPushButton,
)
from PySide6.QtCore import Qt, QTimer, QAbstractNativeEventFilter
from PySide6.QtGui import QIcon

from core.config import save_config
from core.device_manager import DeviceManager
from core.engine_controller import EngineController
from core.engine_params import EngineParams
from core.preset import list_presets
from ui.tray import SystemTray
from ui.tab_control import ControlTab
from ui.tab_color import ColorTab
from ui.tab_setup import SetupTab
from ui.tab_options import OptionsTab

# ★ 컴팩트 뷰
from ui.compact_window import CompactWindow
from ui.compact_bridge import CompactBridge

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

    def __init__(self, config, start_hidden=False):
        super().__init__()
        self.config = config
        self._force_quit = False

        self._start_hidden = start_hidden

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

        # ★ 컴팩트 뷰 버튼 — 탭바 우측 코너
        self.btn_compact_view = QPushButton("적게 보기")
        self.btn_compact_view.setObjectName("btnCompactView")
        self.btn_compact_view.setCheckable(True)
        self.btn_compact_view.setToolTip("컴팩트 뷰 열기/닫기")
        self.btn_compact_view.clicked.connect(self.toggle_compact_view)
        self.tabs.setCornerWidget(self.btn_compact_view, Qt.Corner.TopRightCorner)

        self.tab_color.request_mirror_stop.connect(self._stop_engine_for_tab)
        self.tab_setup.request_mirror_stop.connect(self._stop_engine_for_tab)

        # ── ControlTab 시그널 연결 ──
        self.tab_control.request_engine_start.connect(self.start_engine)
        self.tab_control.request_engine_stop.connect(self.stop_engine)
        self.tab_control.request_engine_pause.connect(self._toggle_pause)
        self.tab_control.request_mode_switch.connect(self._switch_mode)
        self.tab_control.config_applied.connect(self._on_config_applied)

        # ── EngineController → ControlTab 데이터 시그널 ──
        self.engine_ctrl.fps_updated.connect(self.tab_control.update_fps)
        self.engine_ctrl.energy_updated.connect(self.tab_control.update_energy)
        self.engine_ctrl.spectrum_updated.connect(self.tab_control.update_spectrum)
        self.engine_ctrl.screen_colors_updated.connect(self.tab_control.update_preview_colors)
        self.engine_ctrl.status_changed.connect(self.tab_control.update_status)

        # ── 상태바 ──
        if not start_hidden:
            self.statusBar().showMessage("준비")

        # ── 시스템 트레이 ──
        opts = config.get("options", {})
        self.tray = SystemTray(config, parent=self)
        self._connect_tray_signals()
        if (QSystemTrayIcon.isSystemTrayAvailable()
                and opts.get("tray_enabled", True)):
            self.tray.show()

        # ★ 트레이 프리셋 메뉴 초기화
        self._refresh_tray_presets()

        # ── ★ 컴팩트 뷰 ──
        self.compact_window = CompactWindow()
        self._compact_bridge = CompactBridge(
            compact=self.compact_window,
            tab=self.tab_control,
            engine_ctrl=self.engine_ctrl,
            config=self.config,
            parent=self,
        )
        self._compact_bridge.connect_all()
        self._connect_reverse_sync()
        # ★ 컴팩트 창 닫힘 → 코너 버튼 체크 해제
        self.compact_window.close_requested.connect(
            lambda: self.btn_compact_view.setChecked(False)
        )
        # ★ 컴팩트 더블클릭 → 메인 GUI 열기
        self.compact_window.expand_requested.connect(self._expand_from_compact)

        # ── 잠금 감지 + 디스플레이 변경 + 절전 복귀 ──
        self._was_running_before_lock = False
        self._lock_restart_mode = None
        self._session_filter = SessionEventFilter(self._on_session_event)
        QApplication.instance().installNativeEventFilter(self._session_filter)
        try:
            hwnd = int(self.winId())
            ctypes.windll.wtsapi32.WTSRegisterSessionNotification(hwnd, 0)
            if self._start_hidden:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
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
        self.tray.audio_cycle_requested.connect(self._on_audio_cycle)
        self.tray.preset_selected.connect(self._on_tray_preset_selected)  # ★
        self.tray.show_window_requested.connect(self._show_window)
        self.tray.quit_requested.connect(self._quit)
        # ★ 컴팩트 뷰
        if hasattr(self.tray, 'compact_view_requested'):
            self.tray.compact_view_requested.connect(self.toggle_compact_view)

    # ══════════════════════════════════════════════════════════════
    #  ★ 컴팩트 뷰 — 역방향 동기화 + 토글
    # ══════════════════════════════════════════════════════════════

    def _connect_reverse_sync(self):
        """ControlTab → CompactWindow 역방향 동기화.

        [설계 변경] 개별 시그널 연결 대신 **주기적 폴링** 방식.

        이유:
        - ControlTab의 토글/슬라이더/프리셋 변경 경로가 매우 다양
          (toggled, blockSignals+setChecked, _apply_preset_to_ui, 되돌리기 등)
        - blockSignals로 바뀌는 경우 시그널이 안 나옴 → 누락
        - 미디어 종속 관계(D=OFF→M=OFF) 등 복잡한 상태 전이

        해결: 200ms 타이머로 ControlTab 상태를 읽어서 CompactWindow에 반영.
        CPU 비용 무시 가능 (단순 비교 + blockSignals setValue).
        """
        self._compact_sync_timer = QTimer(self)
        self._compact_sync_timer.setInterval(200)
        self._compact_sync_timer.timeout.connect(self._poll_sync_to_compact)
        self._compact_sync_timer.start()
        self._compact_poll_count = 0  # ★ 미디어 카드는 5틱(1초)마다

        # 프리셋 목록 변경은 즉시 반영 (목록 자체가 바뀌는 건 폴링으로 감지 어려움)
        bridge = self._compact_bridge
        self.tab_control.config_applied.connect(bridge.notify_preset_changed)

        # ★ ControlTab의 미디어 타이머에 bridge 갱신도 연결 — 즉시 반영
        self.tab_control._media_thumbnail_timer.timeout.connect(
            bridge._update_media_card
        )

    def _poll_sync_to_compact(self):
        """ControlTab 상태를 주기적으로 CompactWindow에 동기화."""
        if not self.compact_window.isVisible():
            return

        tab = self.tab_control
        compact = self.compact_window

        # ── 토글 동기화 ──
        for toggle_c, state_tab in [
            (compact.toggle_display, tab._display_on),
            (compact.toggle_audio, tab._audio_on),
            (compact.toggle_media, tab._media_on),
        ]:
            if toggle_c.isChecked() != state_tab:
                toggle_c.blockSignals(True)
                toggle_c.setChecked(state_tab)
                toggle_c.blockSignals(False)

        # 내부 상태도 갱신
        needs_section_update = (
            compact._display_on != tab._display_on
            or compact._audio_on != tab._audio_on
            or compact._media_on != tab._media_on
        )
        compact._display_on = tab._display_on
        compact._audio_on = tab._audio_on
        compact._media_on = tab._media_on
        if needs_section_update:
            compact._update_conditional_sections()

        # ── 밝기 동기화 ──
        tab_bright = tab.slider_master_brightness.value()
        if compact.slider_brightness.value() != tab_bright:
            compact.slider_brightness.blockSignals(True)
            compact.slider_brightness.setValue(tab_bright)
            compact.slider_brightness.blockSignals(False)
            compact.lbl_brightness.setText(f"{tab_bright}%")

        # ── 실행 상태 ──
        if compact._is_running != tab._is_running:
            compact.sync_running_state(tab._is_running)

        # ── 오디오 모드 ──
        tab_mode = tab.section_audio._mode_key
        current_mode = compact.combo_audio_mode.currentData()
        if current_mode != tab_mode:
            compact.combo_audio_mode.blockSignals(True)
            for i in range(compact.combo_audio_mode.count()):
                if compact.combo_audio_mode.itemData(i) == tab_mode:
                    compact.combo_audio_mode.setCurrentIndex(i)
                    break
            compact.combo_audio_mode.blockSignals(False)

        # ── 색상 상태 (D=OFF) ──
        self._compact_bridge._sync_color_state()
  
        # ── ★ 미디어 카드 (1초마다 = 5틱마다) ──
        self._compact_poll_count += 1
        if self._compact_poll_count >= 5:
            self._compact_poll_count = 0
            self._compact_bridge._update_media_card()

    def toggle_compact_view(self):
        """컴팩트 뷰 열기/닫기 토글."""
        if self.compact_window.isVisible():
            self.compact_window.hide()
            self._compact_bridge.on_compact_hidden()
        else:
            self._position_compact_window()
            self.compact_window.show()
            self._compact_bridge.on_compact_shown()
        # ★ 버튼 체크 상태 동기화
        self.btn_compact_view.setChecked(self.compact_window.isVisible())

    def _expand_from_compact(self):
        """컴팩트 뷰에서 메인 GUI로 전환."""
        self.show()
        self.raise_()
        self.activateWindow()

    def _position_compact_window(self):
        """컴팩트 윈도우를 적절한 위치에 배치.

        메인 윈도우가 보이면 → 우측에 나란히.
        메인 윈도우가 숨겨져 있으면 → 화면 우측 하단 (트레이 근처).
        """
        compact = self.compact_window
        compact.adjustSize()

        if self.isVisible():
            # 메인 윈도우 우측에 배치
            main_geo = self.geometry()
            x = main_geo.right() + 10
            y = main_geo.top() - 30
        else:
            # 화면 우측 하단
            screen = QApplication.primaryScreen()
            if screen:
                sg = screen.availableGeometry()
                x = sg.right() - compact.width() - 20
                y = sg.bottom() - compact.height() - 60
            else:
                x, y = 100, 100

        # 화면 밖으로 나가지 않게 보정
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            x = min(x, sg.right() - compact.width())
            y = min(y, sg.bottom() - compact.height())
            x = max(x, sg.left())
            y = max(y, sg.top())

        compact.move(x, y)

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

        audio_dev = self.tab_control.get_audio_device_index()
        self.engine_ctrl.set_audio_device_index(audio_dev)

        self.engine_ctrl.start_engine(
            mode=mode_str,
            initial_params=engine_params,
        )

        # ★ 직전 미디어 판별값 복원
        last = getattr(self.tab_control, '_last_media_confirmed', None)
        if last and self.engine_ctrl.engine:
            self.engine_ctrl.engine._media_detect_last_confirmed = last

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
        if new_mode:
            mode_str = new_mode

        audio_dev = self.tab_control.get_audio_device_index()
        self.engine_ctrl.set_audio_device_index(audio_dev)

        self.engine_ctrl.start_engine(
            mode=mode_str,
            initial_params=engine_params,
        )

        # ★ 직전 미디어 판별값 복원
        last = getattr(self.tab_control, '_last_media_confirmed', None)
        if last and self.engine_ctrl.engine:
            self.engine_ctrl.engine._media_detect_last_confirmed = last

        # ★ 컴팩트 동기화
        if hasattr(self, '_compact_bridge'):
            self._compact_bridge.notify_tab_changed()

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
    #  오디오 모드 순환 핫키
    # ══════════════════════════════════════════════════════════════

    def _on_audio_cycle(self):
        """★ 오디오 모드 순환 핫키 처리."""
        self.tab_control.cycle_audio_mode()

        if not self.engine_ctrl.is_running:
            self.start_engine()

    # ══════════════════════════════════════════════════════════════
    #  ★ 트레이 프리셋
    # ══════════════════════════════════════════════════════════════

    def _on_tray_preset_selected(self, name):
        """트레이에서 프리셋 선택 → UI에 적용 + 엔진 자동 시작."""
        if self.tab_control.select_preset_by_name(name):
            # 엔진이 정지 상태면 자동 시작
            if not self.engine_ctrl.is_running:
                self.start_engine()
            # 트레이 체크 갱신
            self._refresh_tray_presets()

    def _refresh_tray_presets(self):
        """트레이 + 컴팩트 프리셋 메뉴 갱신."""
        names = list_presets()
        current = self.tab_control.current_preset_name
        self.tray.update_preset_menu(names, current)
        # ★ 컴팩트도 갱신
        if hasattr(self, '_compact_bridge'):
            self._compact_bridge.notify_preset_changed()

    # ══════════════════════════════════════════════════════════════
    #  엔진 상태 콜백
    # ══════════════════════════════════════════════════════════════

    def _on_status_changed(self, text):
        if not self._start_hidden:
            self.statusBar().showMessage(text)
        self.tray.update_status(text)

    def _on_error(self, msg, severity="critical"):
        if severity == "critical":
            if self.isVisible() and not self._start_hidden:
                QMessageBox.warning(self, "오류", msg)
            else:
                if self.tray.isVisible():
                    self.tray.showMessage(
                        "Nanoleaf Mirror — 오류",
                        msg,
                        QSystemTrayIcon.MessageIcon.Warning,
                        5000,
                    )
                if not self._start_hidden:
                    self.statusBar().showMessage(f"⚠ {msg}")
                self.tray.update_status(f"⚠ {msg}")
        else:
            if not self._start_hidden:
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

    def _on_config_applied(self):
        """★ config 저장 + 트레이/컴팩트 프리셋 메뉴 갱신."""
        self.tab_control._sync_config_from_ui()
        save_config(self.config)
        self._refresh_tray_presets()

    def _restore_status(self):
        text = "실행 중" if self.engine_ctrl.is_running else "준비"
        if not self._start_hidden:
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
                if not self._start_hidden:
                    self.statusBar().showMessage("잠금 감지 — 엔진 중지")

        elif event == "unlock":
            if self._was_running_before_lock:
                self._was_running_before_lock = False
                mode = self._lock_restart_mode or "unified"
                QTimer.singleShot(3000, lambda: self.start_engine(mode))
                if not self._start_hidden:
                    self.statusBar().showMessage("잠금 해제 — 3초 후 재시작")

    def _on_display_change_settled(self):
        self.engine_ctrl.on_display_changed()
        if not self._start_hidden:
            self.statusBar().showMessage("디스플레이 변경 감지 — 캡처 재초기화 중...")

    def _on_session_resume_settled(self):
        if self.engine_ctrl.is_running:
            self.engine_ctrl.on_session_resume()
            if not self._start_hidden:
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
        self._start_hidden = False
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

        # ★ 컴팩트 뷰 정리
        if hasattr(self, '_compact_sync_timer'):
            self._compact_sync_timer.stop()
        if hasattr(self, '_compact_bridge'):
            self._compact_bridge.cleanup()
        if hasattr(self, 'compact_window'):
            self.compact_window.close()

        try:
            ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(
                int(self.winId())
            )
        except Exception:
            pass

        # ── ★ 종료 시 config auto-save ──
        self.tab_control._sync_config_from_ui()

        # master 밝기
        self.config.setdefault("mirror", {})["master_brightness"] = (
            self.tab_control.slider_master_brightness.value() / 100.0
        )

        # ★ 마지막 프리셋 이름 저장
        self.config.setdefault("options", {})["last_preset"] = (
            self.tab_control.current_preset_name
        )

        # ★ 구 키 정리
        opts = self.config.get("options", {})
        opts.pop("default_mode", None)
        opts.pop("auto_start_mirror", None)

        save_config(self.config)
        QApplication.instance().quit()