"""통합 컨트롤 탭 — 미러링 + 오디오 + 하이브리드 통합 UI (PySide6)

[ADR-019] EngineController를 통한 파라미터 전달.
[ADR-021] MirrorParams/AudioParams typed dataclass로 통일.
[ADR-040] 모드 전환 시 공통 섹션(상태, 모드, 프리뷰) 크기 고정 — 통일성 확보.
"""

import os
import copy
import psutil
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QComboBox, QFrame, QScrollArea, QStackedWidget,
    QButtonGroup, QSizePolicy, QSpinBox, QDoubleSpinBox,
    QCheckBox, QSlider,
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QEvent

from core.audio_engine import list_loopback_devices, HAS_PYAUDIO
from core.engine_utils import MODE_MIRROR, MODE_AUDIO, MODE_HYBRID, N_ZONES_PER_LED
from core.engine_params import MirrorParams, AudioParams
from ui.widgets.monitor_preview import MonitorPreview
from ui.panels.mirror_panel import MirrorPanel
from ui.panels.audio_panel import AudioPanel
from ui.panels.hybrid_panel import HybridPanel

_MODE_INDEX = {MODE_MIRROR: 0, MODE_HYBRID: 1, MODE_AUDIO: 2}
_INDEX_MODE = {0: MODE_MIRROR, 1: MODE_HYBRID, 2: MODE_AUDIO}
_MODE_NAMES = {"mirror": "미러링", "hybrid": "하이브리드", "audio": "오디오"}


class _NoScrollFilter(QObject):
    _FILTERED_TYPES = (QComboBox, QSpinBox, QDoubleSpinBox, QSlider)
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, self._FILTERED_TYPES):
            event.ignore(); return True
        return False


class _ModeButton(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True); self.setMinimumHeight(32)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("""
            QPushButton { background:#2b2b2b;color:#aaa;border:1px solid #444;border-radius:6px;font-size:13px;font-weight:bold;padding:6px 12px; }
            QPushButton:hover { background:#353535;color:#ccc; }
            QPushButton:checked { background:#1a5276;color:#eee;border:2px solid #2e86c1; }
        """)


class ControlTab(QWidget):
    """통합 컨트롤 탭.

    Signals → MainWindow/EngineController:
        request_engine_start(str), request_engine_stop(), request_engine_pause(),
        request_mode_switch(str), config_applied()
    """
    request_engine_start = Signal(str)
    request_engine_stop = Signal()
    request_engine_pause = Signal()
    request_mode_switch = Signal(str)
    config_applied = Signal()

    def __init__(self, config, engine_ctrl=None, parent=None):
        super().__init__(parent)
        self.config = config
        self._engine_ctrl = engine_ctrl
        self._is_running = False
        self._current_mode = MODE_MIRROR
        self._applied_snapshot = copy.deepcopy(config)

        from ui.widgets.audio_param_widget import AUDIO_DEFAULTS
        for key in ("audio_pulse", "audio_spectrum", "audio_bass_detail", "audio_wave", "audio_dynamic"):
            if key not in self.config:
                mode_name = key.replace("audio_", "")
                self.config[key] = dict(AUDIO_DEFAULTS.get(mode_name, AUDIO_DEFAULTS["pulse"]))

        self._build_ui()

        saved_mode = config.get("options", {}).get("default_mode", "mirror")
        saved_idx = _MODE_INDEX.get(saved_mode, 0)
        self._mode_buttons.button(saved_idx).setChecked(True)
        self._current_mode = saved_mode
        self.mode_stack.setCurrentIndex(saved_idx)

        self._layout_debounce = QTimer(self); self._layout_debounce.setSingleShot(True)
        self._layout_debounce.setInterval(300); self._layout_debounce.timeout.connect(self._emit_layout_params)

        self._process = psutil.Process(os.getpid()); self._process.cpu_percent()
        self._res_timer = QTimer(self); self._res_timer.timeout.connect(self._update_resource_usage); self._res_timer.start(2000)

    def _build_ui(self):
        scroll = QScrollArea(self); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container); layout.setSpacing(4); layout.setContentsMargins(6, 4, 6, 4)
        container.setStyleSheet("QGroupBox{padding-top:14px;margin-top:4px;}QGroupBox::title{subcontrol-position:top left;padding:0 4px;}")
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(scroll); scroll.setWidget(container)

        self._build_status(layout); self._build_mode_selector(layout)
        self._build_preview(layout); self._build_panels(layout); self._build_common(layout); self._build_actions(layout)
        layout.addStretch()
        self._no_scroll_filter = _NoScrollFilter(self)
        for w in container.findChildren(QWidget):
            if isinstance(w, _NoScrollFilter._FILTERED_TYPES):
                w.setFocusPolicy(Qt.FocusPolicy.StrongFocus); w.installEventFilter(self._no_scroll_filter)

    def _build_status(self, parent):
        sg = QGroupBox("상태")
        sl = QVBoxLayout(sg)
        sl.setContentsMargins(6, 16, 6, 6)
        sl.setSpacing(6)

        # 상태 표시 행
        info_row = QHBoxLayout()
        self.status_label = QLabel("대기 중"); self.status_label.setStyleSheet("font-size:13px;font-weight:bold;"); info_row.addWidget(self.status_label); info_row.addStretch()
        self.cpu_label = QLabel("CPU: —%"); self.cpu_label.setStyleSheet("font-size:12px;color:#d35400;margin-right:6px;"); info_row.addWidget(self.cpu_label)
        self.ram_label = QLabel("RAM: — MB"); self.ram_label.setStyleSheet("font-size:12px;color:#27ae60;margin-right:10px;"); info_row.addWidget(self.ram_label)
        self.fps_label = QLabel("— fps"); self.fps_label.setStyleSheet("font-size:14px;color:#888;"); info_row.addWidget(self.fps_label)
        sl.addLayout(info_row)

        # 시작/일시정지/중지 버튼 행
        bl = QHBoxLayout()
        self.btn_start = QPushButton("▶ 시작"); self.btn_start.setMinimumHeight(32)
        self.btn_start.setStyleSheet("QPushButton{background:#2d8c46;color:white;font-size:14px;font-weight:bold;border-radius:6px;}QPushButton:hover{background:#35a352;}QPushButton:disabled{background:#555;color:#999;}")
        self.btn_start.clicked.connect(self._on_start_clicked); bl.addWidget(self.btn_start)
        self.btn_pause = QPushButton("⏸ 일시정지"); self.btn_pause.setMinimumHeight(32); self.btn_pause.setEnabled(False)
        self.btn_pause.setStyleSheet("QPushButton{background:#2c3e50;color:white;font-size:14px;font-weight:bold;border-radius:6px;}QPushButton:hover{background:#34495e;}QPushButton:disabled{background:#555;color:#999;}")
        self.btn_pause.clicked.connect(lambda: self.request_engine_pause.emit()); bl.addWidget(self.btn_pause)
        self.btn_stop = QPushButton("⏹ 중지"); self.btn_stop.setMinimumHeight(32); self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("QPushButton{background:#c0392b;color:white;font-size:14px;font-weight:bold;border-radius:6px;}QPushButton:hover{background:#e74c3c;}QPushButton:disabled{background:#555;color:#999;}")
        self.btn_stop.clicked.connect(lambda: self.request_engine_stop.emit()); bl.addWidget(self.btn_stop)
        sl.addLayout(bl)

        parent.addWidget(sg)

    def _build_mode_selector(self, parent):
        mg = QGroupBox("모드"); ml = QVBoxLayout(mg); ml.setSpacing(8); ml.setContentsMargins(6, 16, 6, 6)
        btn_row = QHBoxLayout()
        self._mode_buttons = QButtonGroup(self); self._mode_buttons.setExclusive(True)
        for mode_key, label in [(MODE_MIRROR, "🖥  미러링"), (MODE_HYBRID, "하이브리드"), (MODE_AUDIO, "🎵  오디오")]:
            btn = _ModeButton(label); self._mode_buttons.addButton(btn, _MODE_INDEX[mode_key]); btn_row.addWidget(btn)
        self._mode_buttons.button(0).setChecked(True)
        self._mode_buttons.idClicked.connect(self._on_mode_changed); ml.addLayout(btn_row)
        dr = QHBoxLayout(); dr.addStretch()
        self.btn_set_default = QPushButton("현재 모드를 기본값으로 설정"); self.btn_set_default.setFixedHeight(24)
        self.btn_set_default.setStyleSheet("QPushButton{background:#444;color:#bbb;font-size:11px;border-radius:4px;padding:2px 10px;}QPushButton:hover{background:#555;color:#eee;}")
        self.btn_set_default.clicked.connect(self._on_set_default); dr.addWidget(self.btn_set_default); dr.addStretch(); ml.addLayout(dr)
        parent.addWidget(mg)

    def _build_preview(self, parent):
        pg = QGroupBox("LED 프리뷰"); pl = QVBoxLayout(pg); pl.setContentsMargins(6, 16, 6, 4); pl.setSpacing(2)
        self.btn_preview_toggle = QPushButton("👁 프리뷰 보기"); self.btn_preview_toggle.setCheckable(True); self.btn_preview_toggle.setChecked(False)
        self.btn_preview_toggle.setFixedWidth(130)
        self.btn_preview_toggle.setStyleSheet("QPushButton{background:#34495e;color:#bdc3c7;border-radius:4px;padding:5px;font-size:11px;}QPushButton:checked{background:#2980b9;color:white;}")
        self.btn_preview_toggle.toggled.connect(self._on_preview_toggled); pl.addWidget(self.btn_preview_toggle)
        self.monitor_preview = MonitorPreview(self.config); self.monitor_preview.setVisible(False); pl.addWidget(self.monitor_preview)
        parent.addWidget(pg)

    def _build_panels(self, parent):
        self.mode_stack = QStackedWidget()
        self.mode_stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self.panel_mirror = MirrorPanel(self.config)
        self.panel_mirror.brightness_changed.connect(self._on_mirror_brightness)
        self.panel_mirror.smoothing_changed.connect(self._on_mirror_smoothing)
        self.panel_mirror.smoothing_factor_changed.connect(self._on_mirror_smoothing_factor)
        self.panel_mirror.layout_params_changed.connect(self._on_layout_changed)
        self.panel_mirror.zone_count_changed.connect(self._on_zone_count)
        self.mode_stack.addWidget(self.panel_mirror)
        self.panel_hybrid = HybridPanel(self.config)
        self.panel_hybrid.hybrid_params_changed.connect(self._on_hybrid_params)
        self.mode_stack.addWidget(self.panel_hybrid)
        self.panel_audio = AudioPanel(self.config)
        self.panel_audio.audio_params_changed.connect(self._on_audio_params)
        self.panel_audio.audio_min_brightness_changed.connect(self._on_audio_min_brightness)
        self.mode_stack.addWidget(self.panel_audio)
        self.mode_stack.setCurrentIndex(0)
        self.mode_stack.currentChanged.connect(self._adjust_stack)
        self._adjust_stack(0)
        parent.addWidget(self.mode_stack)

    def _build_common(self, parent):
        cg = QGroupBox("공통 설정"); cl = QVBoxLayout(cg); cl.setSpacing(3); cl.setContentsMargins(6, 16, 6, 4)
        or_ = QHBoxLayout(); or_.addWidget(QLabel("화면 방향:"))
        self.combo_orientation = QComboBox(); self.combo_orientation.addItems(["자동 감지", "가로 (Landscape)", "세로 (Portrait)"])
        idx_map = {"auto": 0, "landscape": 1, "portrait": 2}
        self.combo_orientation.setCurrentIndex(idx_map.get(self.config.get("mirror", {}).get("orientation", "auto"), 0))
        or_.addWidget(self.combo_orientation); or_.addWidget(QLabel("세로 회전:"))
        self.combo_rotation = QComboBox(); self.combo_rotation.addItems(["시계방향 (CW)", "반시계방향 (CCW)"])
        self.combo_rotation.setCurrentIndex(0 if self.config.get("mirror", {}).get("portrait_rotation", "cw") == "cw" else 1)
        or_.addWidget(self.combo_rotation); or_.addStretch(); cl.addLayout(or_)
        fr = QHBoxLayout(); fr.addWidget(QLabel("Target FPS:"))
        self.spin_target_fps = QSpinBox(); self.spin_target_fps.setRange(10, 60)
        self.spin_target_fps.setValue(self.config.get("mirror", {}).get("target_fps", 60)); fr.addWidget(self.spin_target_fps); fr.addStretch(); cl.addLayout(fr)
        ar = QHBoxLayout(); ar.addWidget(QLabel("오디오 디바이스:"))
        self.combo_audio_device = QComboBox(); self._refresh_audio_devices()
        # 저장된 오디오 디바이스 복원
        saved_dev = self.config.get("options", {}).get("audio_device_index")
        if saved_dev is not None:
            for i in range(self.combo_audio_device.count()):
                if self.combo_audio_device.itemData(i) == saved_dev:
                    self.combo_audio_device.setCurrentIndex(i); break
        ar.addWidget(self.combo_audio_device)
        btn_ref = QPushButton("🔄"); btn_ref.setFixedWidth(36); btn_ref.clicked.connect(self._refresh_audio_devices); ar.addWidget(btn_ref); ar.addStretch(); cl.addLayout(ar)
        parent.addWidget(cg)

    def _build_actions(self, parent):
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine); line.setFrameShadow(QFrame.Shadow.Sunken); parent.addWidget(line)
        al = QHBoxLayout(); al.addStretch()
        self.btn_apply = QPushButton("💾 저장"); self.btn_apply.setMinimumHeight(28); self.btn_apply.setMinimumWidth(100)
        self.btn_apply.setStyleSheet("QPushButton{background:#2e86c1;color:white;font-size:13px;font-weight:bold;border-radius:6px;}QPushButton:hover{background:#3498db;}")
        self.btn_apply.clicked.connect(self._on_apply); al.addWidget(self.btn_apply)
        self.btn_revert = QPushButton("↩ 되돌리기"); self.btn_revert.setMinimumHeight(28); self.btn_revert.setMinimumWidth(100)
        self.btn_revert.setStyleSheet("QPushButton{background:#555;color:#ccc;font-size:13px;font-weight:bold;border-radius:6px;}QPushButton:hover{background:#666;}")
        self.btn_revert.clicked.connect(self._on_revert); al.addWidget(self.btn_revert)
        parent.addLayout(al)

    # ── 이벤트 ───────────────────────────────────────────────────

    def _on_start_clicked(self):
        self._sync_config_from_ui(); self.request_engine_start.emit(self._current_mode)

    def _on_mode_changed(self, idx):
        self._current_mode = _INDEX_MODE.get(idx, MODE_MIRROR); self.mode_stack.setCurrentIndex(idx)
        if self._is_running:
            self._sync_config_from_ui(); self.request_mode_switch.emit(self._current_mode)

    def _adjust_stack(self, idx):
        """[ADR-040] QStackedWidget 높이를 현재 패널에 맞추되,
        오디오/하이브리드 간 전환 시 크기 변동을 방지.

        - 미러링(0)이 현재: 미러링만 Preferred, 나머지 Ignored
        - 오디오(2)/하이브리드(1)가 현재: 둘 다 Preferred 유지 →
          QStackedWidget가 둘 중 큰 sizeHint를 사용하여 크기 고정
        """
        # 미러링 패널 인덱스
        IDX_MIRROR = 0
        # 오디오/하이브리드 패널 인덱스들
        IDX_AUDIO_GROUP = {1, 2}

        for i in range(self.mode_stack.count()):
            w = self.mode_stack.widget(i)
            if i == idx:
                # 현재 패널은 항상 Preferred
                w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            elif idx in IDX_AUDIO_GROUP and i in IDX_AUDIO_GROUP:
                # 오디오/하이브리드 간 전환: 상대 패널도 Preferred 유지
                w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            else:
                # 그 외(미러링 ↔ 오디오/하이브리드): 숨겨진 패널 Ignored
                w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self.mode_stack.adjustSize()

    def _on_preview_toggled(self, checked):
        self.monitor_preview.setVisible(checked)
        self.btn_preview_toggle.setText("👁 프리뷰 숨기기" if checked else "👁 프리뷰 보기")

    def _on_set_default(self):
        self.config.setdefault("options", {})["default_mode"] = self._current_mode
        self._applied_snapshot.setdefault("options", {})["default_mode"] = self._current_mode
        self.config_applied.emit()
        name = _MODE_NAMES.get(self._current_mode, self._current_mode)
        self.btn_set_default.setText(f"{name}이(가) 기본 모드로 설정됨")
        QTimer.singleShot(2000, lambda: self.btn_set_default.setText("현재 모드를 기본값으로 설정"))

    # ── ADR-019/021: 패널 → EngineController 전달 ────────────────

    def _on_mirror_brightness(self, value):
        if self._is_running and self._engine_ctrl:
            mp = MirrorParams(brightness=value / 100.0,
                              smoothing_enabled=self.panel_mirror.chk_smoothing.isChecked(),
                              smoothing_factor=self.panel_mirror.spin_smoothing.value(),
                              mirror_n_zones=self.panel_mirror.combo_zone_count.currentData() or N_ZONES_PER_LED)
            self._engine_ctrl.set_mirror_params(mp)

    def _on_mirror_smoothing(self, enabled):
        if self._is_running and self._engine_ctrl:
            mp = MirrorParams(brightness=self.panel_mirror.brightness_slider.value() / 100.0,
                              smoothing_enabled=enabled,
                              smoothing_factor=self.panel_mirror.spin_smoothing.value(),
                              mirror_n_zones=self.panel_mirror.combo_zone_count.currentData() or N_ZONES_PER_LED)
            self._engine_ctrl.set_mirror_params(mp)

    def _on_mirror_smoothing_factor(self, value):
        if self._is_running and self._engine_ctrl:
            mp = MirrorParams(brightness=self.panel_mirror.brightness_slider.value() / 100.0,
                              smoothing_enabled=self.panel_mirror.chk_smoothing.isChecked(),
                              smoothing_factor=value,
                              mirror_n_zones=self.panel_mirror.combo_zone_count.currentData() or N_ZONES_PER_LED)
            self._engine_ctrl.set_mirror_params(mp)

    def _on_layout_changed(self):
        if self._is_running: self._layout_debounce.start()

    def _emit_layout_params(self):
        if self._engine_ctrl:
            params = self.panel_mirror.get_layout_params()
            self._engine_ctrl.update_layout_params(**params)

    def _on_zone_count(self, n):
        """미러링 구역 수 변경 → config 갱신 후 엔진 재시작 (즉시 적용)."""
        if self._is_running:
            self._sync_config_from_ui()
            self.request_mode_switch.emit(self._current_mode)

    def _on_audio_params(self, params_dict):
        if self._is_running and self._engine_ctrl:
            filtered = {k: v for k, v in params_dict.items()
                        if k in AudioParams.__dataclass_fields__}
            ap = AudioParams(**filtered)
            self._engine_ctrl.set_audio_params(ap)

    def _on_audio_min_brightness(self, value):
        """오디오 최소 밝기 변경 → AudioParams 전체 스냅샷으로 전달."""
        if self._is_running and self._engine_ctrl:
            params_dict = self.panel_audio.collect_params()
            filtered = {k: v for k, v in params_dict.items()
                        if k in AudioParams.__dataclass_fields__}
            ap = AudioParams(**filtered)
            self._engine_ctrl.set_audio_params(ap)

    def _on_hybrid_params(self, params_dict):
        if self._is_running and self._engine_ctrl:
            filtered = {k: v for k, v in params_dict.items()
                        if k in AudioParams.__dataclass_fields__}
            ap = AudioParams(**filtered)
            self._engine_ctrl.set_audio_params(ap)

    # ── Apply / Revert ───────────────────────────────────────────

    def _sync_config_from_ui(self):
        """UI 위젯 값을 config dict에 반영 (디스크 저장·스냅샷 갱신 없음).
        엔진 시작, 모드 전환 등에서 호출."""
        self._apply_common()
        self.panel_mirror.apply_to_config(self.config)
        if self._current_mode == MODE_AUDIO:
            self.panel_audio.apply_to_config()
        elif self._current_mode == MODE_HYBRID:
            self.panel_hybrid.apply_to_config()

    def _on_apply(self):
        """💾 저장 — UI→dict 반영 + 스냅샷 갱신 + 디스크 기록."""
        self._sync_config_from_ui()
        self._applied_snapshot = copy.deepcopy(self.config)
        self.config_applied.emit()
        # 저장 확인 피드백
        self.btn_apply.setText("✅ 저장됨")
        QTimer.singleShot(2000, lambda: self.btn_apply.setText("💾 저장"))

    def _on_revert(self):
        for key in self._applied_snapshot: self.config[key] = copy.deepcopy(self._applied_snapshot[key])
        self._load_common(self._applied_snapshot); self.panel_mirror.load_from_config(self._applied_snapshot)
        self.panel_audio.load_from_config(); self.panel_hybrid.load_from_config()

    def _apply_common(self):
        m = self.config.setdefault("mirror", {})
        m["orientation"] = {0: "auto", 1: "landscape", 2: "portrait"}.get(self.combo_orientation.currentIndex(), "auto")
        m["portrait_rotation"] = "cw" if self.combo_rotation.currentIndex() == 0 else "ccw"
        m["target_fps"] = self.spin_target_fps.value()
        # 오디오 디바이스 선택 저장
        self.config.setdefault("options", {})["audio_device_index"] = self.combo_audio_device.currentData()

    def _load_common(self, cfg):
        m = cfg.get("mirror", {})
        self.combo_orientation.setCurrentIndex({"auto": 0, "landscape": 1, "portrait": 2}.get(m.get("orientation", "auto"), 0))
        self.combo_rotation.setCurrentIndex(0 if m.get("portrait_rotation", "cw") == "cw" else 1)
        self.spin_target_fps.setValue(m.get("target_fps", 60))
        # 오디오 디바이스 선택 복원
        saved_dev = cfg.get("options", {}).get("audio_device_index")
        if saved_dev is not None:
            for i in range(self.combo_audio_device.count()):
                if self.combo_audio_device.itemData(i) == saved_dev:
                    self.combo_audio_device.setCurrentIndex(i)
                    break

    def _refresh_audio_devices(self):
        self.combo_audio_device.clear(); self.combo_audio_device.addItem("자동 (기본 출력 디바이스)", None)
        if HAS_PYAUDIO:
            for idx, name, sr, ch in list_loopback_devices():
                self.combo_audio_device.addItem(f"{name} ({sr}Hz, {ch}ch)", idx)

    # ── 외부 인터페이스 ──────────────────────────────────────────

    @property
    def current_mode(self): return self._current_mode

    @property
    def saved_config(self):
        """💾 저장 버튼으로 확정된 config 스냅샷. 종료 시 이 값을 디스크에 기록."""
        return self._applied_snapshot

    def set_engine_ctrl(self, ctrl):
        """EngineController 참조 설정 (MainWindow에서 호출)."""
        self._engine_ctrl = ctrl

    def set_running_state(self, running):
        self._is_running = running
        self.btn_start.setEnabled(not running); self.btn_pause.setEnabled(running); self.btn_stop.setEnabled(running)
        self.combo_orientation.setEnabled(not running); self.combo_rotation.setEnabled(not running)
        self.spin_target_fps.setEnabled(not running); self.combo_audio_device.setEnabled(not running)
        self.panel_audio.set_running(running); self.panel_hybrid.set_running(running)

    def set_switching(self, switching):
        for btn_id in _MODE_INDEX.values():
            btn = self._mode_buttons.button(btn_id)
            if btn: btn.setEnabled(not switching)
        self.btn_start.setEnabled(not switching); self.btn_stop.setEnabled(not switching); self.btn_pause.setEnabled(not switching)
        if switching: self.update_status("모드 전환 중...")

    def update_fps(self, fps): self.fps_label.setText(f"{fps:.1f} fps")
    def update_status(self, text): self.status_label.setText(text)
    def update_preview_colors(self, colors):
        if self.monitor_preview.isVisible(): self.monitor_preview.set_colors(colors)
    def update_energy(self, bass, mid, high):
        self.panel_audio.update_energy(bass, mid, high); self.panel_hybrid.update_energy(bass, mid, high)
    def update_spectrum(self, spec):
        self.panel_audio.update_spectrum(spec); self.panel_hybrid.update_spectrum(spec)
    def update_pause_button(self, is_paused): self.btn_pause.setText("▶ 재개" if is_paused else "⏸ 일시정지")
    def get_audio_device_index(self): return self.combo_audio_device.currentData()

    def collect_engine_init_params(self):
        mode = self._current_mode; params = {"mode": mode}
        if mode == MODE_MIRROR:
            params.update({"brightness": self.panel_mirror.brightness_slider.value() / 100.0,
                           "smoothing_enabled": self.panel_mirror.chk_smoothing.isChecked(),
                           "smoothing_factor": self.panel_mirror.spin_smoothing.value(),
                           "mirror_n_zones": self.panel_mirror.combo_zone_count.currentData() or N_ZONES_PER_LED})
        elif mode == MODE_AUDIO: params.update(self.panel_audio.collect_params())
        elif mode == MODE_HYBRID: params.update(self.panel_hybrid.collect_params())
        return params

    def _update_resource_usage(self):
        try:
            cpu = self._process.cpu_percent() / psutil.cpu_count()
            mem_info = self._process.memory_full_info()
            ram = getattr(mem_info, 'uss', mem_info.rss) / (1024 * 1024)
            self.cpu_label.setText(f"CPU: {cpu:.1f}%"); self.ram_label.setText(f"RAM: {ram:.0f} MB")
            color = "#c0392b" if cpu >= 20 else "#e67e22" if cpu >= 10 else "#d35400"
            self.cpu_label.setStyleSheet(f"font-size:12px;color:{color};margin-right:6px;")
        except Exception: pass

    def cleanup(self): self._res_timer.stop(); self.panel_audio.cleanup(); self.panel_hybrid.cleanup()