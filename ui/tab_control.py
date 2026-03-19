"""통합 컨트롤 탭 — 토글 기반 반응형 UI (Phase 4: 엔진 연결)

[설계]
디스플레이 토글 + 오디오 토글 + 미디어 연동 토글 조합으로 상태를 표현.
각 토글 on/off에 따라 관련 패널이 부드럽게 펼쳐지거나 접힘.

[미디어 연동 추가]
- toggle_media: 미디어 연동 토글 스위치
- tag_media: 상태 태그 (ON/OFF)
- _media_on 상태 관리
- HAS_MEDIA_SESSION=False 시 토글 비활성 + 툴팁 안내

[v6 변경]
- lbl_media_info 및 _media_info_timer 제거 (상태바 미디어 메시지 제거)
- section_mirror.refresh_thumbnail_requested 시그널 연결
  → MediaFrameProvider 캐시 리셋 + 즉시 재폴링
- _update_media_thumbnail(): 3초마다 썸네일만 갱신 (곡 정보 라벨 없음)

[Refactor] 범용 위젯 분리
- ToggleSwitch → ui/widgets/toggle_switch.py
- CollapsiblePanel → ui/widgets/collapsible_panel.py
- _NoScrollFilter → ui/widgets/no_scroll_filter.py (NoScrollFilter로 이름 변경)

[시그널]
  request_engine_start(str)   — 모드 문자열로 엔진 시작 요청
  request_engine_stop()       — 엔진 중지 요청
  request_engine_pause()      — 일시정지 토글
  request_mode_switch(str)    — 실행 중 모드 전환 요청
  config_applied()            — config 저장됨
"""

import os
import copy
import psutil
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QComboBox, QFrame, QScrollArea,
    QSizePolicy, QSpinBox, QDoubleSpinBox,
    QSlider, QCheckBox,
)
from PySide6.QtCore import Qt, QTimer, Signal, QEvent

from core.engine_params import EngineParams
from core.engine_utils import N_ZONES_PER_LED

# ── 파라미터 빌더 ──
from ui.engine_params_builder import EngineParamsBuilder

# ── 재사용 위젯 (ui/widgets에서 import) ──
from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.no_scroll_filter import NoScrollFilter
from ui.widgets.collapsible_panel import CollapsiblePanel
from ui.widgets.toggle_switch import ToggleSwitch
from ui.widgets.monitor_preview import MonitorPreview

# ── Phase 2: 디스플레이 패널 섹션 ──
from ui.panels.display_color_section import DisplayColorSection
from ui.panels.display_mirror_section import DisplayMirrorSection

# ── Phase 3: 오디오 패널 섹션 ──
from ui.panels.audio_reactive_section import AudioReactiveSection

# ── 오디오 디바이스 목록 ──
from core.audio_engine import list_loopback_devices, HAS_PYAUDIO

# ── ★ 미디어 연동 ──
from core.media_session import HAS_MEDIA_SESSION


# ══════════════════════════════════════════════════════════════════
#  ControlTab
# ══════════════════════════════════════════════════════════════════

class ControlTab(QWidget):
    """통합 컨트롤 탭 — 토글 기반 반응형 UI."""

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
        self._params_builder = EngineParamsBuilder()

        # 토글 상태
        opts = config.get("options", {})
        self._display_on = opts.get("default_display_enabled", False)
        self._audio_on = opts.get("default_audio_enabled", False)
        self._media_on = opts.get("default_media_enabled", False)

        # 저장/되돌리기 스냅샷
        self._applied_snapshot = copy.deepcopy(config)

        self._build_ui()
        self._update_toggle_panels(animate=False)

        # Phase 2: 레이아웃 변경 디바운스 타이머
        self._layout_debounce = QTimer(self)
        self._layout_debounce.setSingleShot(True)
        self._layout_debounce.setInterval(300)
        self._layout_debounce.timeout.connect(self._emit_layout_params)

        # ★ 미디어 썸네일 갱신 타이머 (3초마다, 곡 정보 라벨 없이 썸네일만)
        self._media_thumbnail_timer = QTimer(self)
        self._media_thumbnail_timer.setInterval(3000)
        self._media_thumbnail_timer.timeout.connect(self._update_media_thumbnail)

        # 자원 모니터링
        self._process = psutil.Process(os.getpid())
        self._process.cpu_percent()
        self._res_timer = QTimer(self)
        self._res_timer.timeout.connect(self._update_resource_usage)
        self._res_timer.start(2000)

    # ══════════════════════════════════════════════════════════════
    #  UI 빌드
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        """전체 UI 구성: scroll 영역 + 하단 고정 버튼."""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 스크롤 영역 ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        self._main_layout = QVBoxLayout(container)
        self._main_layout.setSpacing(4)
        self._main_layout.setContentsMargins(6, 4, 6, 4)
        container.setStyleSheet(
            "QGroupBox{padding-top:14px;margin-top:4px;}"
            "QGroupBox::title{subcontrol-position:top left;padding:0 4px;}"
            "QToolTip{background:#3a3a42;color:#e0e0e0;border:1px solid #666;"
            "padding:4px 8px;font-size:11px;}"
        )

        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        # ── 각 섹션 빌드 ──
        self._build_status_section()
        self._build_basic_settings_section()
        self._build_preview_section()
        self._build_reactive_panels()
        self._main_layout.addStretch()

        # ── 하단 고정 버튼 (스크롤 밖) ──
        self._build_bottom_actions(root)

        # ── 스크롤 방지 필터 ──
        self._no_scroll_filter = NoScrollFilter(self)
        for w in container.findChildren(QWidget):
            if isinstance(w, NoScrollFilter.FILTERED_TYPES):
                w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                w.installEventFilter(self._no_scroll_filter)

    # ── ① 상태 패널 ─────────────────────────────────────────────

    def _build_status_section(self):
        grp = QGroupBox("상태")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 16, 6, 6)
        lay.setSpacing(6)

        # 상태 행: 메시지 + 토글 태그 + 자원 + fps
        info_row = QHBoxLayout()

        self.status_label = QLabel("대기 중")
        self.status_label.setStyleSheet("font-size:13px;font-weight:bold;")
        info_row.addWidget(self.status_label)

        self.tag_display = QLabel("디스플레이 OFF")
        self.tag_display.setStyleSheet(
            "background:#2b2b2b;color:#6a6a74;padding:2px 8px;"
            "border-radius:8px;font-size:10px;font-weight:600;"
        )
        info_row.addWidget(self.tag_display)

        self.tag_audio = QLabel("오디오 OFF")
        self.tag_audio.setStyleSheet(
            "background:#2b2b2b;color:#6a6a74;padding:2px 8px;"
            "border-radius:8px;font-size:10px;font-weight:600;"
        )
        info_row.addWidget(self.tag_audio)

        # ★ 미디어 연동 태그
        self.tag_media = QLabel("미디어 OFF")
        self.tag_media.setStyleSheet(
            "background:#2b2b2b;color:#6a6a74;padding:2px 8px;"
            "border-radius:8px;font-size:10px;font-weight:600;"
        )
        info_row.addWidget(self.tag_media)

        info_row.addStretch()

        self.cpu_label = QLabel("CPU: —%")
        self.cpu_label.setStyleSheet("font-size:12px;color:#d35400;margin-right:6px;")
        info_row.addWidget(self.cpu_label)

        self.ram_label = QLabel("RAM: — MB")
        self.ram_label.setStyleSheet("font-size:12px;color:#27ae60;margin-right:10px;")
        info_row.addWidget(self.ram_label)

        self.fps_label = QLabel("— fps")
        self.fps_label.setStyleSheet("font-size:14px;color:#888;")
        info_row.addWidget(self.fps_label)

        lay.addLayout(info_row)

        # 버튼 행
        btn_row = QHBoxLayout()

        self.btn_start = QPushButton("▶ 시작")
        self.btn_start.setMinimumHeight(32)
        self.btn_start.setStyleSheet(
            "QPushButton{background:#2d8c46;color:white;font-size:14px;"
            "font-weight:bold;border-radius:6px;}"
            "QPushButton:hover{background:#35a352;}"
            "QPushButton:disabled{background:#555;color:#999;}"
        )
        self.btn_start.clicked.connect(self._on_start_clicked)
        btn_row.addWidget(self.btn_start)

        self.btn_pause = QPushButton("⏸ 일시정지")
        self.btn_pause.setMinimumHeight(32)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setStyleSheet(
            "QPushButton{background:#2c3e50;color:white;font-size:14px;"
            "font-weight:bold;border-radius:6px;}"
            "QPushButton:hover{background:#34495e;}"
            "QPushButton:disabled{background:#555;color:#999;}"
        )
        self.btn_pause.clicked.connect(lambda: self.request_engine_pause.emit())
        btn_row.addWidget(self.btn_pause)

        self.btn_stop = QPushButton("⏹ 중지")
        self.btn_stop.setMinimumHeight(32)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-size:14px;"
            "font-weight:bold;border-radius:6px;}"
            "QPushButton:hover{background:#e74c3c;}"
            "QPushButton:disabled{background:#555;color:#999;}"
        )
        self.btn_stop.clicked.connect(lambda: self.request_engine_stop.emit())
        btn_row.addWidget(self.btn_stop)

        lay.addLayout(btn_row)
        self._main_layout.addWidget(grp)

    # ── ② 기초 설정 ─────────────────────────────────────────────

    def _build_basic_settings_section(self):
        grp = QGroupBox("기초 설정")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 16, 6, 6)
        lay.setSpacing(6)

        # 토글 행
        toggle_row = QHBoxLayout()
        self.toggle_display = ToggleSwitch("디스플레이 미러링")
        self.toggle_display.setChecked(self._display_on)
        self.toggle_display.toggled.connect(self._on_display_toggled)
        toggle_row.addWidget(self.toggle_display)
        toggle_row.addSpacing(20)
        self.toggle_audio = ToggleSwitch("오디오 반응")
        self.toggle_audio.setChecked(self._audio_on)
        self.toggle_audio.toggled.connect(self._on_audio_toggled)
        toggle_row.addWidget(self.toggle_audio)

        # ★ 미디어 연동 토글
        toggle_row.addSpacing(20)
        self.toggle_media = ToggleSwitch("미디어 연동")
        self.toggle_media.setChecked(self._media_on)
        self.toggle_media.toggled.connect(self._on_media_toggled)
        toggle_row.addWidget(self.toggle_media)

        # HAS_MEDIA_SESSION이 False면 토글 비활성
        if not HAS_MEDIA_SESSION:
            self.toggle_media.setEnabled(False)
            self.toggle_media.setToolTip(
                "winrt 패키지가 설치되지 않았습니다.\n"
                "pip install winrt-runtime winrt-Windows.Media.Control "
                "winrt-Windows.Storage.Streams winrt-Windows.Foundation"
            )
        else:
            # ★ 디스플레이 OFF면 미디어 토글도 비활성
            self.toggle_media.setEnabled(self._display_on)

        toggle_row.addStretch()
        lay.addLayout(toggle_row)

        # 기본값 설정 버튼 + 힌트
        default_toggle_row = QHBoxLayout()
        self.btn_set_default = QPushButton("현재 토글 설정을 기본값으로 설정")
        self.btn_set_default.setFixedHeight(24)
        self.btn_set_default.setStyleSheet(
            "QPushButton{background:#444;color:#bbb;font-size:11px;"
            "border-radius:4px;padding:2px 10px;}"
            "QPushButton:hover{background:#555;color:#eee;}"
        )
        self.btn_set_default.clicked.connect(self._on_set_default_toggles)
        default_toggle_row.addWidget(self.btn_set_default)
        self.lbl_toggle_default_hint = QLabel("")
        self.lbl_toggle_default_hint.setStyleSheet(
            "color:#6a6a74;font-size:10px;font-style:italic;"
        )
        default_toggle_row.addWidget(self.lbl_toggle_default_hint)
        default_toggle_row.addStretch()
        lay.addLayout(default_toggle_row)
        self._update_toggle_default_hint()

        lay.addWidget(self._make_separator())

        # master 밝기
        bright_row = QHBoxLayout()
        bright_row.addWidget(QLabel("master 밝기"))
        self.slider_master_brightness = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_master_brightness.setRange(0, 100)
        saved_bright = int(self.config.get("mirror", {}).get("master_brightness", 1.0) * 100)
        self.slider_master_brightness.setValue(saved_bright)
        self.slider_master_brightness.valueChanged.connect(self._on_master_brightness_changed)
        bright_row.addWidget(self.slider_master_brightness)
        self.lbl_master_brightness = QLabel(f"{saved_bright}%")
        self.lbl_master_brightness.setMinimumWidth(35)
        self.lbl_master_brightness.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        bright_row.addWidget(self.lbl_master_brightness)
        lay.addLayout(bright_row)

        hint = QLabel("모든 모드의 최대 밝기. 오디오 모드에서는 최대 밝기로 기능합니다.")
        hint.setStyleSheet("color:#6a6a74;font-size:10px;font-style:italic;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        lay.addWidget(self._make_separator())

        # 공통 설정
        orient_row = QHBoxLayout()
        orient_row.addWidget(QLabel("화면 방향:"))
        self.combo_orientation = QComboBox()
        self.combo_orientation.addItems(["자동 감지", "가로 (Landscape)", "세로 (Portrait)"])
        idx_map = {"auto": 0, "landscape": 1, "portrait": 2}
        self.combo_orientation.setCurrentIndex(
            idx_map.get(self.config.get("mirror", {}).get("orientation", "auto"), 0)
        )
        orient_row.addWidget(self.combo_orientation)
        orient_row.addWidget(QLabel("세로 회전:"))
        self.combo_rotation = QComboBox()
        self.combo_rotation.addItems(["시계방향 (CW)", "반시계방향 (CCW)"])
        self.combo_rotation.setCurrentIndex(
            0 if self.config.get("mirror", {}).get("portrait_rotation", "cw") == "cw" else 1
        )
        orient_row.addWidget(self.combo_rotation)
        orient_row.addStretch()
        lay.addLayout(orient_row)

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("Target FPS:"))
        self.spin_target_fps = QSpinBox()
        self.spin_target_fps.setRange(10, 60)
        self.spin_target_fps.setValue(
            self.config.get("mirror", {}).get("target_fps", 60)
        )
        fps_row.addWidget(self.spin_target_fps)
        fps_row.addStretch()
        lay.addLayout(fps_row)

        audio_row = QHBoxLayout()
        audio_row.addWidget(QLabel("오디오 디바이스:"))
        self.combo_audio_device = QComboBox()
        self._refresh_audio_devices()
        saved_dev = self.config.get("options", {}).get("audio_device_index")
        if saved_dev is not None:
            for i in range(self.combo_audio_device.count()):
                if self.combo_audio_device.itemData(i) == saved_dev:
                    self.combo_audio_device.setCurrentIndex(i)
                    break
        audio_row.addWidget(self.combo_audio_device)
        btn_refresh = QPushButton("🔄")
        btn_refresh.setFixedWidth(36)
        btn_refresh.clicked.connect(self._refresh_audio_devices)
        audio_row.addWidget(btn_refresh)
        audio_row.addStretch()
        lay.addLayout(audio_row)

        self._main_layout.addWidget(grp)

    # ── ③ LED 프리뷰 ────────────────────────────────────────────

    def _build_preview_section(self):
        grp = QGroupBox("LED 프리뷰")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 16, 6, 4)
        lay.setSpacing(2)

        self.btn_preview_toggle = QPushButton("👁 프리뷰 보기")
        self.btn_preview_toggle.setCheckable(True)
        self.btn_preview_toggle.setChecked(False)
        self.btn_preview_toggle.setFixedWidth(130)
        self.btn_preview_toggle.setStyleSheet(
            "QPushButton{background:#34495e;color:#bdc3c7;border-radius:4px;"
            "padding:5px;font-size:11px;}"
            "QPushButton:checked{background:#2980b9;color:white;}"
        )
        self.btn_preview_toggle.toggled.connect(self._on_preview_toggled)
        lay.addWidget(self.btn_preview_toggle)

        self.monitor_preview = MonitorPreview(self.config)
        self.monitor_preview.setVisible(False)
        lay.addWidget(self.monitor_preview)

        hint = QLabel("색상 보정 적용 전 RGB 값으로 표시됩니다")
        hint.setStyleSheet("color:#6a6a74;font-size:10px;font-style:italic;")
        hint.setVisible(False)
        self._preview_hint = hint
        lay.addWidget(hint)

        self._main_layout.addWidget(grp)

    # ── ④ 토글 반응형 패널 ──────────────────────────────────────

    def _build_reactive_panels(self):
        # ── 디스플레이 side ──
        self.panel_display_off = CollapsiblePanel()
        self._build_display_off_content(self.panel_display_off)
        self._main_layout.addWidget(self.panel_display_off)

        self.panel_display_on = CollapsiblePanel()
        self._build_display_on_content(self.panel_display_on)
        self._main_layout.addWidget(self.panel_display_on)

        # ── 오디오 side ──
        self.panel_audio_on = CollapsiblePanel()
        self._build_audio_on_content(self.panel_audio_on)
        self._main_layout.addWidget(self.panel_audio_on)

    def _build_display_off_content(self, panel):
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.section_color = DisplayColorSection(self.config)
        self.section_color.params_changed.connect(self._on_display_params_changed)
        lay.addWidget(self.section_color)
        panel.set_content_layout(lay)

    def _build_display_on_content(self, panel):
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.section_mirror = DisplayMirrorSection(self.config)
        self.section_mirror.params_changed.connect(self._on_display_params_changed)
        self.section_mirror.layout_params_changed.connect(self._on_layout_changed)
        self.section_mirror.zone_count_changed.connect(self._on_zone_count_changed)
        # ★ 썸네일 새로고침 시그널 연결
        self.section_mirror.refresh_thumbnail_requested.connect(
            self._on_refresh_thumbnail_requested
        )
        lay.addWidget(self.section_mirror)
        panel.set_content_layout(lay)

    def _build_audio_on_content(self, panel):
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.section_audio = AudioReactiveSection(self.config)
        self.section_audio.set_display_enabled(self._display_on)
        self.section_audio.params_changed.connect(self._on_audio_params_changed)
        self.section_audio.audio_mode_changed.connect(self._on_audio_mode_changed)
        self.section_audio.default_mode_saved.connect(self._on_default_mode_saved)
        lay.addWidget(self.section_audio)
        panel.set_content_layout(lay)

    # ── ⑤ 하단 고정 버튼 ────────────────────────────────────────

    def _build_bottom_actions(self, root_layout):
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        root_layout.addWidget(sep)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(6, 4, 6, 6)
        action_row.addStretch()

        self.btn_apply = QPushButton("💾 저장")
        self.btn_apply.setMinimumHeight(28)
        self.btn_apply.setMinimumWidth(100)
        self.btn_apply.setStyleSheet(
            "QPushButton{background:#2e86c1;color:white;font-size:13px;"
            "font-weight:bold;border-radius:6px;}"
            "QPushButton:hover{background:#3498db;}"
        )
        self.btn_apply.clicked.connect(self._on_apply)
        action_row.addWidget(self.btn_apply)

        self.btn_revert = QPushButton("↩ 되돌리기")
        self.btn_revert.setMinimumHeight(28)
        self.btn_revert.setMinimumWidth(100)
        self.btn_revert.setStyleSheet(
            "QPushButton{background:#555;color:#ccc;font-size:13px;"
            "font-weight:bold;border-radius:6px;}"
            "QPushButton:hover{background:#666;}"
        )
        self.btn_revert.clicked.connect(self._on_revert)
        action_row.addWidget(self.btn_revert)

        root_layout.addLayout(action_row)

    # ══════════════════════════════════════════════════════════════
    #  토글 이벤트
    # ══════════════════════════════════════════════════════════════

    def _on_display_toggled(self, checked):
        self._display_on = checked
        self._update_toggle_panels(animate=True)
        if hasattr(self, 'section_audio'):
            self.section_audio.set_display_enabled(checked)

        # ★ 디스플레이 OFF → 미디어 토글도 끄기 (종속 관계)
        if not checked and self._media_on:
            self._media_on = False
            self.toggle_media.blockSignals(True)
            self.toggle_media.setChecked(False)
            self.toggle_media.blockSignals(False)
            self._media_thumbnail_timer.stop()
        # 미디어 토글은 디스플레이 ON일 때만 활성
        self.toggle_media.setEnabled(checked and HAS_MEDIA_SESSION)
        # 미러링 패널에 소스 상태 갱신
        if hasattr(self, 'section_mirror'):
            self.section_mirror.set_media_active(self._media_on and checked)

        self._sync_flowing_state()
        if self._is_running:
            self._sync_config_from_ui()
            self.request_mode_switch.emit(self._get_engine_mode_string())

    def _on_audio_toggled(self, checked):
        self._audio_on = checked
        self._update_toggle_panels(animate=True)
        if checked and self._display_on and hasattr(self, 'section_audio'):
            self.section_audio.set_display_enabled(self._display_on)
        self._sync_flowing_state()
        if self._is_running:
            self._sync_config_from_ui()
            self.request_mode_switch.emit(self._get_engine_mode_string())

    def _on_media_toggled(self, checked):
        """★ 미디어 연동 토글 변경 (디스플레이 ON 시에만 동작)."""
        self._media_on = checked
        self._update_toggle_panels(animate=True)

        # 미러링 패널에 소스 상태 갱신
        if hasattr(self, 'section_mirror'):
            self.section_mirror.set_media_active(checked)

        # ★ 썸네일 타이머 제어
        if checked and self._is_running:
            self._media_thumbnail_timer.start()
        else:
            self._media_thumbnail_timer.stop()

        if self._is_running:
            self._push_params_to_engine()

    def _sync_flowing_state(self):
        if not hasattr(self, 'section_mirror') or not hasattr(self, 'section_audio'):
            return
        is_flowing = (
            self._audio_on
            and self._display_on
            and self.section_audio._mode_key == "flowing"
        )
        self.section_mirror.set_flowing_active(is_flowing)

    def _update_toggle_panels(self, animate=True):
        # ── 태그 갱신 (각 토글마다 고유 색상) ──
        if self._display_on:
            self.tag_display.setText("디스플레이 ON")
            self.tag_display.setStyleSheet(
                "background:#1a3456;color:#7ec8e3;padding:2px 8px;"
                "border-radius:8px;font-size:10px;font-weight:600;"
            )
        else:
            self.tag_display.setText("디스플레이 OFF")
            self.tag_display.setStyleSheet(
                "background:#2b2b2b;color:#6a6a74;padding:2px 8px;"
                "border-radius:8px;font-size:10px;font-weight:600;"
            )

        if self._audio_on:
            self.tag_audio.setText("오디오 ON")
            self.tag_audio.setStyleSheet(
                "background:#2e1a45;color:#c49be8;padding:2px 8px;"
                "border-radius:8px;font-size:10px;font-weight:600;"
            )
        else:
            self.tag_audio.setText("오디오 OFF")
            self.tag_audio.setStyleSheet(
                "background:#2b2b2b;color:#6a6a74;padding:2px 8px;"
                "border-radius:8px;font-size:10px;font-weight:600;"
            )

        # ★ 미디어 태그 갱신 — display ON + media ON일 때만 실제 활성
        media_effective = self._media_on and self._display_on
        if media_effective:
            self.tag_media.setText("미디어 ON")
            self.tag_media.setStyleSheet(
                "background:#2d3a1a;color:#a3d977;padding:2px 8px;"
                "border-radius:8px;font-size:10px;font-weight:600;"
            )
        else:
            self.tag_media.setText("미디어 OFF")
            self.tag_media.setStyleSheet(
                "background:#2b2b2b;color:#6a6a74;padding:2px 8px;"
                "border-radius:8px;font-size:10px;font-weight:600;"
            )

        self.panel_display_off.set_expanded(not self._display_on, animate=animate)
        self.panel_display_on.set_expanded(self._display_on, animate=animate)
        self.panel_audio_on.set_expanded(self._audio_on, animate=animate)

        # ★ 미러링 패널 소스 상태 동기화 (초기 로드 시 포함)
        if hasattr(self, 'section_mirror'):
            self.section_mirror.set_media_active(self._media_on and self._display_on)

    def _on_set_default_toggles(self):
        opts = self.config.setdefault("options", {})
        opts["default_display_enabled"] = self._display_on
        opts["default_audio_enabled"] = self._audio_on
        opts["default_media_enabled"] = self._media_on
        self._applied_snapshot.setdefault("options", {})["default_display_enabled"] = self._display_on
        self._applied_snapshot.setdefault("options", {})["default_audio_enabled"] = self._audio_on
        self._applied_snapshot.setdefault("options", {})["default_media_enabled"] = self._media_on
        self.config_applied.emit()
        self._update_toggle_default_hint()
        self.btn_set_default.setText("✅ 저장됨")
        QTimer.singleShot(2000, lambda: self.btn_set_default.setText(
            "현재 토글 설정을 기본값으로 설정"
        ))

    def _update_toggle_default_hint(self):
        opts = self.config.get("options", {})
        d_on = opts.get("default_display_enabled", False)
        a_on = opts.get("default_audio_enabled", False)
        m_on = opts.get("default_media_enabled", False)
        parts = [
            f"디스플레이 {'ON' if d_on else 'OFF'}",
            f"오디오 {'ON' if a_on else 'OFF'}",
            f"미디어 {'ON' if m_on else 'OFF'}",
        ]
        self.lbl_toggle_default_hint.setText(f"기본값: {' / '.join(parts)}")

    # ══════════════════════════════════════════════════════════════
    #  ★ 미디어 썸네일 + 곡 정보 툴팁 + 현재 소스 상태 갱신
    # ══════════════════════════════════════════════════════════════

    def _update_media_thumbnail(self):
        """3초마다 엔진에서 썸네일 + 곡 정보 + 현재 소스 판별 결과를 갱신."""
        if not self._media_on or not self._is_running:
            return
        if not self._engine_ctrl or not self._engine_ctrl.engine:
            return

        engine = self._engine_ctrl.engine
        provider = getattr(engine, '_media_provider', None)
        if provider is None:
            return

        # ★ 미러링 패널의 앨범아트 썸네일 갱신
        if hasattr(self, 'section_mirror'):
            frame = provider.get_frame()
            self.section_mirror.update_media_thumbnail(frame)

        # ★ 곡 정보를 카드 라벨 + 썸네일 툴팁에 설정
        info = provider.get_media_info()
        if info and hasattr(self, 'section_mirror'):
            artist = info.get("artist", "")
            title = info.get("title", "")
            if artist and title:
                song_text = f"♪ {artist} — {title}"
            elif title:
                song_text = f"♪ {title}"
            else:
                song_text = ""
            self.section_mirror.lbl_media_song.setText(song_text)
            self.section_mirror.lbl_media_thumbnail.setToolTip(song_text)

        # ★ 현재 소스 판별 결과를 미러링 패널에 전달
        if hasattr(self, 'section_mirror'):
            decision = getattr(engine, '_media_detect_decision', "media")
            state = getattr(engine, '_media_detect_state', "idle")
            self.section_mirror.update_current_source(decision, state)

    def _on_refresh_thumbnail_requested(self):
        """★ 썸네일 새로고침 버튼 클릭 — MediaFrameProvider 캐시 리셋 + 즉시 재폴링."""
        if not self._engine_ctrl or not self._engine_ctrl.engine:
            return

        engine = self._engine_ctrl.engine
        provider = getattr(engine, '_media_provider', None)
        if provider is None:
            return

        # 미디어 해시를 0으로 리셋 → 다음 폴링에서 강제 재추출
        with provider._lock:
            provider._media_hash = 0

        # 즉시 썸네일 갱신 시도
        self._update_media_thumbnail()

    # ══════════════════════════════════════════════════════════════
    #  기타 이벤트
    # ══════════════════════════════════════════════════════════════

    def _on_start_clicked(self):
        self._sync_config_from_ui()
        self.request_engine_start.emit(self._get_engine_mode_string())

    def _on_master_brightness_changed(self, value):
        self.lbl_master_brightness.setText(f"{value}%")
        self.config.setdefault("mirror", {})["master_brightness"] = value / 100.0
        if self._is_running:
            self._push_params_to_engine()

    def _on_display_params_changed(self):
        if self._is_running:
            self._push_params_to_engine()

    def _on_layout_changed(self):
        if self._is_running:
            self._layout_debounce.start()

    def _emit_layout_params(self):
        if self._engine_ctrl and hasattr(self, 'section_mirror'):
            params = self.section_mirror.get_layout_params()
            self._engine_ctrl.update_layout_params(**params)

    def _on_zone_count_changed(self, n):
        if self._is_running:
            self._sync_config_from_ui()
            self.request_mode_switch.emit(self._get_engine_mode_string())

    def _on_audio_params_changed(self):
        if self._is_running:
            self._push_params_to_engine()

    def _on_audio_mode_changed(self, mode_key):
        self._sync_flowing_state()

    def _on_default_mode_saved(self):
        audio_state = self.config.get("options", {}).get("audio_state", {})
        snap_state = (self._applied_snapshot
                      .setdefault("options", {})
                      .setdefault("audio_state", {}))
        snap_state["default_audio_mode"] = audio_state.get("default_audio_mode", "pulse")

    def _on_preview_toggled(self, checked):
        self.monitor_preview.setVisible(checked)
        self._preview_hint.setVisible(checked)
        self.btn_preview_toggle.setText(
            "👁 프리뷰 숨기기" if checked else "👁 프리뷰 보기"
        )

    # ══════════════════════════════════════════════════════════════
    #  저장 / 되돌리기
    # ══════════════════════════════════════════════════════════════

    def _on_apply(self):
        self._sync_config_from_ui()
        self._applied_snapshot = copy.deepcopy(self.config)
        self.config_applied.emit()
        self.btn_apply.setText("✅ 저장됨")
        QTimer.singleShot(2000, lambda: self.btn_apply.setText("💾 저장"))

    def _on_revert(self):
        for key in self._applied_snapshot:
            self.config[key] = copy.deepcopy(self._applied_snapshot[key])
        self._load_from_config()
        if self._is_running:
            self._push_params_to_engine()

    def _sync_config_from_ui(self):
        m = self.config.setdefault("mirror", {})
        m["orientation"] = {0: "auto", 1: "landscape", 2: "portrait"}.get(
            self.combo_orientation.currentIndex(), "auto"
        )
        m["portrait_rotation"] = "cw" if self.combo_rotation.currentIndex() == 0 else "ccw"
        m["target_fps"] = self.spin_target_fps.value()
        self.config.setdefault("options", {})["audio_device_index"] = (
            self.combo_audio_device.currentData()
        )
        self.section_color.apply_to_config()
        self.section_mirror.apply_to_config()
        self.section_audio.apply_to_config()

    def _load_from_config(self):
        m = self.config.get("mirror", {})
        self.combo_orientation.setCurrentIndex(
            {"auto": 0, "landscape": 1, "portrait": 2}.get(m.get("orientation", "auto"), 0)
        )
        self.combo_rotation.setCurrentIndex(
            0 if m.get("portrait_rotation", "cw") == "cw" else 1
        )
        self.spin_target_fps.setValue(m.get("target_fps", 60))
        self.section_color.load_from_config()
        self.section_mirror.load_from_config()
        self.section_audio.load_from_config()

        # ★ 미디어 토글 복원
        opts = self.config.get("options", {})
        self._media_on = opts.get("default_media_enabled", False)
        self.toggle_media.blockSignals(True)
        self.toggle_media.setChecked(self._media_on)
        self.toggle_media.blockSignals(False)

        self._update_toggle_default_hint()

    # ══════════════════════════════════════════════════════════════
    #  외부 인터페이스
    # ══════════════════════════════════════════════════════════════

    @property
    def display_enabled(self) -> bool:
        return self._display_on

    @property
    def audio_enabled(self) -> bool:
        return self._audio_on

    @property
    def media_enabled(self) -> bool:
        return self._media_on

    @property
    def saved_config(self):
        return self._applied_snapshot

    def set_engine_ctrl(self, ctrl):
        self._engine_ctrl = ctrl

    def set_running_state(self, running):
        self._is_running = running
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.combo_orientation.setEnabled(not running)
        self.combo_rotation.setEnabled(not running)
        self.spin_target_fps.setEnabled(not running)
        self.combo_audio_device.setEnabled(not running)

        # ★ 썸네일 타이머 제어
        if running and self._media_on:
            self._media_thumbnail_timer.start()
        elif not running:
            self._media_thumbnail_timer.stop()

    def update_fps(self, fps):
        self.fps_label.setText(f"{fps:.1f} fps")

    def update_status(self, text):
        self.status_label.setText(text)

    def update_preview_colors(self, colors):
        if self.monitor_preview.isVisible():
            self.monitor_preview.set_colors(colors)

    def update_energy(self, bass, mid, high):
        if hasattr(self, 'section_audio'):
            self.section_audio.update_energy(bass, mid, high)

    def update_spectrum(self, spec):
        if hasattr(self, 'section_audio'):
            if isinstance(spec, dict) and spec.get("type") == "flow_palette":
                self.section_audio.update_flow_palette(
                    spec["colors"], spec.get("ratios")
                )
            else:
                self.section_audio.update_spectrum(spec)

    def update_pause_button(self, is_paused):
        self.btn_pause.setText("▶ 재개" if is_paused else "⏸ 일시정지")

    def get_audio_device_index(self):
        return self.combo_audio_device.currentData()

    def collect_engine_init_params(self):
        params = {
            "display_enabled": self._display_on,
            "audio_enabled": self._audio_on,
            "media_color_enabled": self._media_on,
            "master_brightness": self.slider_master_brightness.value() / 100.0,
        }
        if self._display_on:
            params.update(self.section_mirror.collect_params())
        else:
            params.update(self.section_color.collect_params())
        if self._audio_on:
            params.update(self.section_audio.collect_params())
        return params

    # ══════════════════════════════════════════════════════════════
    #  엔진 파라미터 빌드 + 전달
    # ══════════════════════════════════════════════════════════════

    def _build_engine_params(self):
        return self._params_builder.build(
            display_enabled=self._display_on,
            audio_enabled=self._audio_on,
            media_color_enabled=self._media_on,
            master_brightness=self.slider_master_brightness.value() / 100.0,
            display_params=self.section_mirror.collect_params() if self._display_on else None,
            color_params=self.section_color.collect_params() if not self._display_on else None,
            audio_params=self.section_audio.collect_params() if self._audio_on else None,
        )

    def _push_params_to_engine(self):
        if not self._engine_ctrl:
            return
        ep = self._build_engine_params()
        self._engine_ctrl.set_params(ep)

    def build_init_params_for_start(self):
        ep = self._build_engine_params()
        mode = self._get_engine_mode_string()
        return mode, ep

    # ══════════════════════════════════════════════════════════════
    #  헬퍼
    # ══════════════════════════════════════════════════════════════

    def _get_engine_mode_string(self):
        return "unified"

    def _refresh_audio_devices(self):
        self.combo_audio_device.clear()
        self.combo_audio_device.addItem("자동 (기본 출력 디바이스)", None)
        if HAS_PYAUDIO:
            for idx, name, sr, ch in list_loopback_devices():
                self.combo_audio_device.addItem(f"{name} ({sr}Hz, {ch}ch)", idx)

    def _update_resource_usage(self):
        try:
            cpu = self._process.cpu_percent() / psutil.cpu_count()
            mem_info = self._process.memory_full_info()
            ram = getattr(mem_info, 'uss', mem_info.rss) / (1024 * 1024)
            self.cpu_label.setText(f"CPU: {cpu:.1f}%")
            self.ram_label.setText(f"RAM: {ram:.0f} MB")
            color = "#c0392b" if cpu >= 20 else "#e67e22" if cpu >= 10 else "#d35400"
            self.cpu_label.setStyleSheet(f"font-size:12px;color:{color};margin-right:6px;")
        except Exception:
            pass

    @staticmethod
    def _make_separator():
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        return sep

    def cleanup(self):
        self._res_timer.stop()
        self._media_thumbnail_timer.stop()
        if hasattr(self, 'section_audio'):
            self.section_audio.cleanup()