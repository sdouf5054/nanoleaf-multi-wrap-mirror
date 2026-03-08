"""통합 컨트롤 탭 — 미러링 + 오디오 + 하이브리드 통합 UI

모드별 설정 패널은 ui/panels/에 분리되어 있고,
이 클래스는 상태바, 제어 버튼, 모드 선택, LED 프리뷰, 공통 설정,
적용/되돌리기 로직을 관리하는 컨테이너입니다.

Signals:
    request_engine_start(str): 모드 문자열과 함께 엔진 시작 요청
    request_engine_stop(): 엔진 중지 요청
    request_engine_pause(): 일시정지/재개 토글
    request_mode_switch(str): 실행 중 모드 전환 요청
    config_applied(): config.json 저장 요청
    mirror_layout_params_changed(dict): 실행 중 감쇠/페널티 변경
    mirror_brightness_changed(int): 실행 중 밝기 변경
    mirror_smoothing_changed(bool): 스무딩 on/off 변경
    mirror_smoothing_factor_changed(float): 스무딩 계수 변경
    mirror_zone_count_changed(int): 미러링 구역 수 변경
    audio_params_changed(dict): 오디오 파라미터 변경
    audio_min_brightness_changed(float): 오디오 최소 밝기 변경
    hybrid_params_changed(dict): 하이브리드 파라미터 변경
"""

import os
import copy
import psutil
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QComboBox, QFrame, QScrollArea, QStackedWidget,
    QButtonGroup, QSizePolicy, QSpinBox, QDoubleSpinBox,
    QCheckBox, QSlider,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QEvent

from core.audio_engine import list_loopback_devices, HAS_PYAUDIO
from core.engine_utils import (
    MODE_MIRROR, MODE_AUDIO, MODE_HYBRID,
    N_ZONES_PER_LED,
)
from ui.widgets.monitor_preview import MonitorPreview
from ui.panels.mirror_panel import MirrorPanel
from ui.panels.audio_panel import AudioPanel
from ui.panels.hybrid_panel import HybridPanel


# ── 모드 인덱스 ──────────────────────────────────────────────────
_MODE_INDEX = {MODE_MIRROR: 0, MODE_HYBRID: 1, MODE_AUDIO: 2}
_INDEX_MODE = {0: MODE_MIRROR, 1: MODE_HYBRID, 2: MODE_AUDIO}


class _NoScrollFilter(QObject):
    """마우스 휠로 위젯 값이 변경되는 것을 방지하는 이벤트 필터."""
    _FILTERED_TYPES = (QComboBox, QSpinBox, QDoubleSpinBox, QSlider)

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Wheel
                and isinstance(obj, self._FILTERED_TYPES)):
            event.ignore()
            return True
        return False


class _ModeButton(QPushButton):
    """모드 선택용 토글 버튼."""
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("""
            QPushButton {
                background: #2b2b2b; color: #aaa;
                border: 1px solid #444; border-radius: 6px;
                font-size: 13px; font-weight: bold; padding: 6px 12px;
            }
            QPushButton:hover { background: #353535; color: #ccc; }
            QPushButton:checked {
                background: #1a5276; color: #eee;
                border: 2px solid #2e86c1;
            }
        """)


class ControlTab(QWidget):
    """통합 컨트롤 탭 — 패널들의 컨테이너."""

    # MainWindow로 전달되는 시그널
    request_engine_start = pyqtSignal(str)
    request_engine_stop = pyqtSignal()
    request_engine_pause = pyqtSignal()
    request_mode_switch = pyqtSignal(str)
    config_applied = pyqtSignal()

    # 미러링 실시간 반영 시그널
    mirror_layout_params_changed = pyqtSignal(dict)
    mirror_brightness_changed = pyqtSignal(int)
    mirror_smoothing_changed = pyqtSignal(bool)
    mirror_smoothing_factor_changed = pyqtSignal(float)
    mirror_zone_count_changed = pyqtSignal(int)

    # 오디오/하이브리드 실시간 반영 시그널
    audio_params_changed = pyqtSignal(dict)
    audio_min_brightness_changed = pyqtSignal(float)
    hybrid_params_changed = pyqtSignal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._is_running = False
        self._current_mode = MODE_MIRROR
        self._applied_snapshot = copy.deepcopy(config)

        # config에 오디오 키 확보
        from ui.widgets.audio_param_widget import AUDIO_DEFAULTS
        for key in ("audio_pulse", "audio_spectrum", "audio_bass_detail"):
            if key not in self.config:
                mode_name = key.replace("audio_", "")
                self.config[key] = dict(AUDIO_DEFAULTS.get(
                    mode_name, AUDIO_DEFAULTS["pulse"]
                ))

        self._build_ui()

        # 미러링 레이아웃 디바운스 타이머
        self._layout_debounce = QTimer(self)
        self._layout_debounce.setSingleShot(True)
        self._layout_debounce.setInterval(300)
        self._layout_debounce.timeout.connect(self._emit_mirror_layout_params)

        # 자원 모니터링 타이머
        self._process = psutil.Process(os.getpid())
        self._process.cpu_percent()
        self._res_timer = QTimer(self)
        self._res_timer.timeout.connect(self._update_resource_usage)
        self._res_timer.start(2000)

    # ══════════════════════════════════════════════════════════════
    #  UI 빌드
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 4, 6, 4)
        container.setStyleSheet(
            "QGroupBox { padding-top: 14px; margin-top: 4px; }"
            "QGroupBox::title { subcontrol-position: top left;"
            " padding: 0 4px; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll.setWidget(container)

        self._build_status_section(layout)
        self._build_control_buttons(layout)
        self._build_mode_selector(layout)
        self._build_preview_section(layout)
        self._build_mode_panels(layout)
        self._build_common_settings(layout)
        self._build_action_buttons(layout)
        layout.addStretch()

        # 스크롤 방지 필터
        self._no_scroll_filter = _NoScrollFilter(self)
        for w in container.findChildren(QWidget):
            if isinstance(w, _NoScrollFilter._FILTERED_TYPES):
                w.setFocusPolicy(Qt.StrongFocus)
                w.installEventFilter(self._no_scroll_filter)

    # ── 1. 상태 ──────────────────────────────────────────────────

    def _build_status_section(self, parent_layout):
        sg = QGroupBox("상태")
        sl = QHBoxLayout(sg)
        sl.setContentsMargins(6, 16, 6, 4)
        self.status_label = QLabel("대기 중")
        self.status_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        sl.addWidget(self.status_label)
        sl.addStretch()
        self.cpu_label = QLabel("CPU: —%")
        self.cpu_label.setStyleSheet("font-size: 12px; color: #d35400; margin-right: 6px;")
        sl.addWidget(self.cpu_label)
        self.ram_label = QLabel("RAM: — MB")
        self.ram_label.setStyleSheet("font-size: 12px; color: #27ae60; margin-right: 10px;")
        sl.addWidget(self.ram_label)
        self.fps_label = QLabel("— fps")
        self.fps_label.setStyleSheet("font-size: 14px; color: #888;")
        sl.addWidget(self.fps_label)
        parent_layout.addWidget(sg)

    # ── 2. 제어 버튼 ─────────────────────────────────────────────

    def _build_control_buttons(self, parent_layout):
        bl = QHBoxLayout()
        self.btn_start = QPushButton("▶ 시작")
        self.btn_start.setMinimumHeight(32)
        self.btn_start.setStyleSheet(
            "QPushButton { background: #2d8c46; color: white; font-size: 14px;"
            " font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #35a352; }"
            "QPushButton:disabled { background: #555; color: #999; }"
        )
        self.btn_start.clicked.connect(self._on_start_clicked)
        bl.addWidget(self.btn_start)

        self.btn_pause = QPushButton("⏸ 일시정지")
        self.btn_pause.setMinimumHeight(32)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setStyleSheet(
            "QPushButton { background: #2c3e50; color: white; font-size: 14px;"
            " font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #34495e; }"
            "QPushButton:disabled { background: #555; color: #999; }"
        )
        self.btn_pause.clicked.connect(lambda: self.request_engine_pause.emit())
        bl.addWidget(self.btn_pause)

        self.btn_stop = QPushButton("⏹ 중지")
        self.btn_stop.setMinimumHeight(32)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton { background: #c0392b; color: white; font-size: 14px;"
            " font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #e74c3c; }"
            "QPushButton:disabled { background: #555; color: #999; }"
        )
        self.btn_stop.clicked.connect(lambda: self.request_engine_stop.emit())
        bl.addWidget(self.btn_stop)
        parent_layout.addLayout(bl)

    # ── 3. 모드 선택 ─────────────────────────────────────────────

    def _build_mode_selector(self, parent_layout):
        mg = QGroupBox("모드")
        ml = QHBoxLayout(mg)
        ml.setSpacing(6)
        self._mode_buttons = QButtonGroup(self)
        self._mode_buttons.setExclusive(True)
        modes = [
            (MODE_MIRROR, "🖥  미러링"),
            (MODE_HYBRID, "🎵+🖥  하이브리드"),
            (MODE_AUDIO, "🎵  오디오"),
        ]
        for mode_key, label in modes:
            btn = _ModeButton(label)
            self._mode_buttons.addButton(btn, _MODE_INDEX[mode_key])
            ml.addWidget(btn)
        self._mode_buttons.button(_MODE_INDEX[MODE_MIRROR]).setChecked(True)
        self._mode_buttons.idClicked.connect(self._on_mode_changed)
        parent_layout.addWidget(mg)

    # ── 4. LED 프리뷰 ────────────────────────────────────────────

    def _build_preview_section(self, parent_layout):
        pg = QGroupBox("LED 프리뷰")
        pl = QVBoxLayout(pg)
        pl.setContentsMargins(6, 16, 6, 4)
        pl.setSpacing(2)
        self.btn_preview_toggle = QPushButton("👁 프리뷰 보기")
        self.btn_preview_toggle.setCheckable(True)
        self.btn_preview_toggle.setChecked(False)
        self.btn_preview_toggle.setFixedWidth(130)
        self.btn_preview_toggle.setStyleSheet(
            "QPushButton { background: #34495e; color: #bdc3c7; border-radius: 4px;"
            " padding: 5px; font-size: 11px; }"
            "QPushButton:checked { background: #2980b9; color: white; }"
        )
        self.btn_preview_toggle.toggled.connect(self._on_preview_toggled)
        pl.addWidget(self.btn_preview_toggle)
        self.monitor_preview = MonitorPreview(self.config)
        self.monitor_preview.setVisible(False)
        pl.addWidget(self.monitor_preview)
        parent_layout.addWidget(pg)

    # ── 5. 모드별 패널 ───────────────────────────────────────────

    def _build_mode_panels(self, parent_layout):
        self.mode_stack = QStackedWidget()
        self.mode_stack.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        # 미러링 패널
        self.panel_mirror = MirrorPanel(self.config)
        self.panel_mirror.brightness_changed.connect(self._on_mirror_brightness)
        self.panel_mirror.smoothing_changed.connect(self.mirror_smoothing_changed.emit)
        self.panel_mirror.smoothing_factor_changed.connect(
            self.mirror_smoothing_factor_changed.emit
        )
        self.panel_mirror.layout_params_changed.connect(self._on_mirror_layout_changed)
        self.panel_mirror.zone_count_changed.connect(self._on_mirror_zone_count)
        self.mode_stack.addWidget(self.panel_mirror)

        # 하이브리드 패널
        self.panel_hybrid = HybridPanel(self.config)
        self.panel_hybrid.hybrid_params_changed.connect(self.hybrid_params_changed.emit)
        self.mode_stack.addWidget(self.panel_hybrid)

        # 오디오 패널
        self.panel_audio = AudioPanel(self.config)
        self.panel_audio.audio_params_changed.connect(self.audio_params_changed.emit)
        self.panel_audio.audio_min_brightness_changed.connect(
            self.audio_min_brightness_changed.emit
        )
        self.mode_stack.addWidget(self.panel_audio)

        self.mode_stack.setCurrentIndex(0)
        self.mode_stack.currentChanged.connect(self._adjust_stack_size)
        self._adjust_stack_size(0)
        parent_layout.addWidget(self.mode_stack)

    # ── 6. 공통 설정 ─────────────────────────────────────────────

    def _build_common_settings(self, parent_layout):
        cg = QGroupBox("공통 설정")
        cl = QVBoxLayout(cg)
        cl.setSpacing(3)
        cl.setContentsMargins(6, 16, 6, 4)

        orient_row = QHBoxLayout()
        orient_row.addWidget(QLabel("화면 방향:"))
        self.combo_orientation = QComboBox()
        self.combo_orientation.addItems(["자동 감지", "가로 (Landscape)", "세로 (Portrait)"])
        orient_val = self.config.get("mirror", {}).get("orientation", "auto")
        idx_map = {"auto": 0, "landscape": 1, "portrait": 2}
        self.combo_orientation.setCurrentIndex(idx_map.get(orient_val, 0))
        orient_row.addWidget(self.combo_orientation)
        orient_row.addWidget(QLabel("세로 회전:"))
        self.combo_rotation = QComboBox()
        self.combo_rotation.addItems(["시계방향 (CW)", "반시계방향 (CCW)"])
        rot_val = self.config.get("mirror", {}).get("portrait_rotation", "cw")
        self.combo_rotation.setCurrentIndex(0 if rot_val == "cw" else 1)
        orient_row.addWidget(self.combo_rotation)
        orient_row.addStretch()
        cl.addLayout(orient_row)

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("Target FPS:"))
        self.spin_target_fps = QSpinBox()
        self.spin_target_fps.setRange(10, 60)
        self.spin_target_fps.setValue(self.config.get("mirror", {}).get("target_fps", 60))
        fps_row.addWidget(self.spin_target_fps)
        fps_row.addStretch()
        cl.addLayout(fps_row)

        audio_row = QHBoxLayout()
        audio_row.addWidget(QLabel("오디오 디바이스:"))
        self.combo_audio_device = QComboBox()
        self._refresh_audio_devices()
        audio_row.addWidget(self.combo_audio_device)
        btn_refresh = QPushButton("🔄")
        btn_refresh.setFixedWidth(36)
        btn_refresh.clicked.connect(self._refresh_audio_devices)
        audio_row.addWidget(btn_refresh)
        audio_row.addStretch()
        cl.addLayout(audio_row)

        parent_layout.addWidget(cg)

    # ── 7. 적용/되돌리기 ─────────────────────────────────────────

    def _build_action_buttons(self, parent_layout):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        parent_layout.addWidget(line)
        al = QHBoxLayout()
        al.addStretch()
        self.btn_apply = QPushButton("💾 적용")
        self.btn_apply.setMinimumHeight(28)
        self.btn_apply.setMinimumWidth(100)
        self.btn_apply.setStyleSheet(
            "QPushButton { background: #2e86c1; color: white; font-size: 13px;"
            " font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #3498db; }"
        )
        self.btn_apply.clicked.connect(self._on_apply_clicked)
        al.addWidget(self.btn_apply)
        self.btn_revert = QPushButton("↩ 되돌리기")
        self.btn_revert.setMinimumHeight(28)
        self.btn_revert.setMinimumWidth(100)
        self.btn_revert.setStyleSheet(
            "QPushButton { background: #555; color: #ccc; font-size: 13px;"
            " font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #666; }"
        )
        self.btn_revert.clicked.connect(self._on_revert_clicked)
        al.addWidget(self.btn_revert)
        parent_layout.addLayout(al)

    # ══════════════════════════════════════════════════════════════
    #  이벤트 핸들러
    # ══════════════════════════════════════════════════════════════

    def _on_start_clicked(self):
        self._apply_all_settings()
        self.config_applied.emit()
        self.request_engine_start.emit(self._current_mode)

    def _on_mode_changed(self, idx):
        self._current_mode = _INDEX_MODE.get(idx, MODE_MIRROR)
        self.mode_stack.setCurrentIndex(idx)
        if self._is_running:
            self._apply_all_settings()
            self.config_applied.emit()
            self.request_mode_switch.emit(self._current_mode)

    def _adjust_stack_size(self, idx):
        for i in range(self.mode_stack.count()):
            w = self.mode_stack.widget(i)
            if i == idx:
                w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            else:
                w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        self.mode_stack.adjustSize()

    def _on_preview_toggled(self, checked):
        self.monitor_preview.setVisible(checked)
        self.btn_preview_toggle.setText(
            "👁 프리뷰 숨기기" if checked else "👁 프리뷰 보기"
        )

    # ── 미러링 패널 → ControlTab 시그널 전달 ─────────────────────

    def _on_mirror_brightness(self, value):
        if self._is_running:
            self.mirror_brightness_changed.emit(value)

    def _on_mirror_layout_changed(self):
        if self._is_running:
            self._layout_debounce.start()

    def _emit_mirror_layout_params(self):
        params = self.panel_mirror.get_layout_params()
        self.mirror_layout_params_changed.emit(params)

    def _on_mirror_zone_count(self, n):
        if self._is_running:
            self.mirror_zone_count_changed.emit(n)

    # ── 적용/되돌리기 ────────────────────────────────────────────

    def _apply_all_settings(self):
        self._apply_common_settings()
        self.panel_mirror.apply_to_config(self.config)
        self.panel_audio.apply_to_config()
        self.panel_hybrid.apply_to_config()
        self._applied_snapshot = copy.deepcopy(self.config)

    def _on_apply_clicked(self):
        self._apply_all_settings()
        self.config_applied.emit()
        if self._is_running:
            if self._current_mode == MODE_AUDIO:
                self.audio_params_changed.emit(self.panel_audio.collect_params())
            elif self._current_mode == MODE_HYBRID:
                self.hybrid_params_changed.emit(self.panel_hybrid.collect_params())

    def _on_revert_clicked(self):
        for key in self._applied_snapshot:
            self.config[key] = copy.deepcopy(self._applied_snapshot[key])
        self._load_common_settings(self._applied_snapshot)
        self.panel_mirror.load_from_config(self._applied_snapshot)
        self.panel_audio.load_from_config()
        self.panel_hybrid.load_from_config()

    def _apply_common_settings(self):
        m = self.config.setdefault("mirror", {})
        orient_map = {0: "auto", 1: "landscape", 2: "portrait"}
        m["orientation"] = orient_map.get(self.combo_orientation.currentIndex(), "auto")
        m["portrait_rotation"] = "cw" if self.combo_rotation.currentIndex() == 0 else "ccw"
        m["target_fps"] = self.spin_target_fps.value()

    def _load_common_settings(self, cfg):
        m = cfg.get("mirror", {})
        orient_val = m.get("orientation", "auto")
        idx_map = {"auto": 0, "landscape": 1, "portrait": 2}
        self.combo_orientation.setCurrentIndex(idx_map.get(orient_val, 0))
        rot_val = m.get("portrait_rotation", "cw")
        self.combo_rotation.setCurrentIndex(0 if rot_val == "cw" else 1)
        self.spin_target_fps.setValue(m.get("target_fps", 60))

    def _refresh_audio_devices(self):
        self.combo_audio_device.clear()
        self.combo_audio_device.addItem("자동 (기본 출력 디바이스)", None)
        if HAS_PYAUDIO:
            for idx, name, sr, ch in list_loopback_devices():
                self.combo_audio_device.addItem(f"{name} ({sr}Hz, {ch}ch)", idx)

    # ══════════════════════════════════════════════════════════════
    #  외부 인터페이스 (MainWindow에서 호출)
    # ══════════════════════════════════════════════════════════════

    @property
    def current_mode(self):
        return self._current_mode

    def set_running_state(self, running):
        self._is_running = running
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.combo_orientation.setEnabled(not running)
        self.combo_rotation.setEnabled(not running)
        self.spin_target_fps.setEnabled(not running)
        self.combo_audio_device.setEnabled(not running)
        self.panel_audio.set_running(running)
        self.panel_hybrid.set_running(running)

    def set_switching(self, switching):
        for btn_id in _MODE_INDEX.values():
            btn = self._mode_buttons.button(btn_id)
            if btn:
                btn.setEnabled(not switching)
        self.btn_start.setEnabled(not switching)
        self.btn_stop.setEnabled(not switching)
        self.btn_pause.setEnabled(not switching)
        if switching:
            self.update_status("모드 전환 중...")

    def update_fps(self, fps):
        self.fps_label.setText(f"{fps:.1f} fps")

    def update_status(self, text):
        self.status_label.setText(text)

    def update_preview_colors(self, colors):
        if self.monitor_preview.isVisible():
            self.monitor_preview.set_colors(colors)

    def update_energy(self, bass, mid, high):
        self.panel_audio.update_energy(bass, mid, high)
        self.panel_hybrid.update_energy(bass, mid, high)

    def update_spectrum(self, spec):
        self.panel_audio.update_spectrum(spec)
        self.panel_hybrid.update_spectrum(spec)

    def update_pause_button(self, is_paused):
        self.btn_pause.setText("▶ 재개" if is_paused else "⏸ 일시정지")

    def get_audio_device_index(self):
        return self.combo_audio_device.currentData()

    def get_mirror_brightness(self):
        return self.panel_mirror.brightness_slider.value() / 100.0

    def get_mirror_smoothing_enabled(self):
        return self.panel_mirror.chk_smoothing.isChecked()

    def get_mirror_smoothing_factor(self):
        return self.panel_mirror.spin_smoothing.value()

    def get_audio_mode(self):
        return self.panel_audio._mode_key

    def collect_engine_init_params(self):
        mode = self._current_mode
        params = {"mode": mode}
        if mode == MODE_MIRROR:
            params.update({
                "brightness": self.get_mirror_brightness(),
                "smoothing_enabled": self.get_mirror_smoothing_enabled(),
                "smoothing_factor": self.get_mirror_smoothing_factor(),
                "mirror_n_zones": self.panel_mirror.combo_zone_count.currentData()
                    or N_ZONES_PER_LED,
            })
        elif mode == MODE_AUDIO:
            params.update(self.panel_audio.collect_params())
        elif mode == MODE_HYBRID:
            params.update(self.panel_hybrid.collect_params())
        return params

    def _update_resource_usage(self):
        try:
            cpu = self._process.cpu_percent() / psutil.cpu_count()
            ram = self._process.memory_info().rss / (1024 * 1024)
            self.cpu_label.setText(f"CPU: {cpu:.1f}%")
            self.ram_label.setText(f"RAM: {ram:.0f} MB")
            color = "#c0392b" if cpu >= 20 else "#e67e22" if cpu >= 10 else "#d35400"
            self.cpu_label.setStyleSheet(
                f"font-size: 12px; color: {color}; margin-right: 6px;"
            )
        except Exception:
            pass

    def cleanup(self):
        self._res_timer.stop()
        self.panel_audio.cleanup()
        self.panel_hybrid.cleanup()
