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

[★ 프리셋 기능 추가]
- 하단 고정 영역: 기존 저장/되돌리기 → 프리셋 콤보 + 저장/새로/되돌리기/삭제
- _applied_snapshot / _on_apply / _on_revert 제거
- config.json auto-save (종료 시 자동 저장)
- 프리셋 변경 감지: 콤보 텍스트에 * 표시
- saved_config 프로퍼티: 현재 config 직접 반환 (스냅샷 불필요)

[QSS 테마] 인라인 setStyleSheet → objectName + QSS property 기반으로 전환.
  - 모든 인라인 스타일시트 제거 (동적 포함)
  - objectName 기반: dark.qss의 셀렉터와 매칭
  - property 기반: tagState, isDefault, level 등 동적 상태 전환
  - 이모지 → 유니코드 텍스트 심볼 (★, ✕, ✓)

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
    QSlider, QCheckBox, QInputDialog, QMessageBox,
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

# ── ★ 프리셋 ──
from core.preset import (
    list_presets, load_preset, save_preset, delete_preset,
    preset_exists, collect_preset_data, preset_differs,
)


# 프리셋 콤보 첫 항목 텍스트
_PRESET_NONE_TEXT = "(선택 안 함)"


# ══════════════════════════════════════════════════════════════
#  QSS property 헬퍼
# ══════════════════════════════════════════════════════════════

def _set_property(widget, name, value):
    """QSS dynamic property를 설정하고 스타일을 다시 적용."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ══════════════════════════════════════════════════════════════
#  아이콘 없는 다이얼로그 헬퍼
# ══════════════════════════════════════════════════════════════

from ui.dialogs import msg_info, msg_warning, msg_question, input_text


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
        self._last_media_confirmed = None  # ★ 엔진 재시작 시 판별값 보존

        # ★ 프리셋 상태
        self._current_preset_name = None   # 현재 선택된 프리셋 이름 (None=미선택)
        self._loaded_preset_data = None    # 로드된 프리셋 원본 데이터 (되돌리기용)
        self._preset_modified = False      # 변경 감지 플래그

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

        # ★ 프리셋 콤보 초기화 (마지막 프리셋 복원)
        self._init_preset_combo()

    # ══════════════════════════════════════════════════════════════
    #  UI 빌드
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        """전체 UI 구성: scroll 영역 + 하단 고정 프리셋 바."""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 스크롤 영역 ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        self._main_layout = QVBoxLayout(container)
        self._main_layout.setSpacing(8)
        self._main_layout.setContentsMargins(6, 4, 6, 4)
        # ★ container.setStyleSheet(...) 제거 → dark.qss로 이전

        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        # ── 각 섹션 빌드 ──
        self._build_status_section()
        self._build_basic_settings_section()
        self._build_preview_section()
        self._build_reactive_panels()
        self._main_layout.addStretch()

        # ── ★ 하단 고정 프리셋 바 (스크롤 밖) ──
        self._build_preset_bar(root)

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
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        # 상태 행: 메시지 + 토글 태그 + 자원 + fps
        info_row = QHBoxLayout()

        self.status_label = QLabel("대기 중")
        self.status_label.setObjectName("statusLabel")
        info_row.addWidget(self.status_label)

        self.tag_display = QLabel("디스플레이 OFF")
        self.tag_display.setObjectName("tagDisplay")
        _set_property(self.tag_display, "tagState", "off")
        info_row.addWidget(self.tag_display)

        self.tag_audio = QLabel("오디오 OFF")
        self.tag_audio.setObjectName("tagAudio")
        _set_property(self.tag_audio, "tagState", "off")
        info_row.addWidget(self.tag_audio)

        # ★ 미디어 연동 태그
        self.tag_media = QLabel("미디어 OFF")
        self.tag_media.setObjectName("tagMedia")
        _set_property(self.tag_media, "tagState", "off")
        info_row.addWidget(self.tag_media)

        info_row.addStretch()

        self.cpu_label = QLabel("CPU: —%")
        self.cpu_label.setObjectName("cpuLabel")
        info_row.addWidget(self.cpu_label)

        self.ram_label = QLabel("RAM: — MB")
        self.ram_label.setObjectName("ramLabel")
        info_row.addWidget(self.ram_label)

        self.fps_label = QLabel("— fps")
        self.fps_label.setObjectName("fpsLabel")
        info_row.addWidget(self.fps_label)

        lay.addLayout(info_row)

        # 버튼 행
        btn_row = QHBoxLayout()

        self.btn_start = QPushButton("▶ 시작")
        self.btn_start.setObjectName("btnStart")
        self.btn_start.setMinimumHeight(32)
        self.btn_start.clicked.connect(self._on_start_clicked)
        btn_row.addWidget(self.btn_start)

        self.btn_pause = QPushButton("∥ 일시정지")
        self.btn_pause.setObjectName("btnPause")
        self.btn_pause.setMinimumHeight(32)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(lambda: self.request_engine_pause.emit())
        btn_row.addWidget(self.btn_pause)

        self.btn_stop = QPushButton("■ 중지")
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.setMinimumHeight(32)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(lambda: self.request_engine_stop.emit())
        btn_row.addWidget(self.btn_stop)

        lay.addLayout(btn_row)
        self._main_layout.addWidget(grp)

    # ── ② 기초 설정 ─────────────────────────────────────────────

    def _build_basic_settings_section(self):
        grp = QGroupBox("기초 설정")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 6, 6, 6)
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
        hint.setProperty("role", "hint")
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
        btn_refresh = QPushButton("↻")
        btn_refresh.setObjectName("btnRefreshThumb")
        btn_refresh.setFixedSize(32, 32)
        btn_refresh.clicked.connect(self._refresh_audio_devices)
        audio_row.addWidget(btn_refresh)
        audio_row.addStretch()
        lay.addLayout(audio_row)

        self._main_layout.addWidget(grp)

    # ── ③ LED 프리뷰 ────────────────────────────────────────────

    def _build_preview_section(self):
        grp = QGroupBox("LED 프리뷰")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 6, 6, 4)
        lay.setSpacing(2)

        self.btn_preview_toggle = QPushButton("프리뷰 보기")
        self.btn_preview_toggle.setObjectName("btnPreviewToggle")
        self.btn_preview_toggle.setCheckable(True)
        self.btn_preview_toggle.setChecked(False)
        self.btn_preview_toggle.setFixedWidth(130)
        self.btn_preview_toggle.toggled.connect(self._on_preview_toggled)
        lay.addWidget(self.btn_preview_toggle)

        self.monitor_preview = MonitorPreview(self.config)
        self.monitor_preview.setVisible(False)
        lay.addWidget(self.monitor_preview)

        hint = QLabel("색상 보정 적용 전 RGB 값으로 표시됩니다")
        hint.setProperty("role", "hint")
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
        lay.addWidget(self.section_audio)
        panel.set_content_layout(lay)

    # ── ⑤ ★ 하단 고정 프리셋 바 ─────────────────────────────────

    def _build_preset_bar(self, root_layout):
        """하단 고정 프리셋 바 — 스크롤 밖, 항상 보임."""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        root_layout.addWidget(sep)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 5, 8, 7)
        bar.setSpacing(6)

        bar.addWidget(QLabel("프리셋:"))

        # 프리셋 콤보박스
        self.combo_preset = QComboBox()
        self.combo_preset.setMinimumWidth(180)
        self.combo_preset.addItem(_PRESET_NONE_TEXT, None)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_selected)
        bar.addWidget(self.combo_preset, 1)

        # 저장 (덮어쓰기)
        self.btn_preset_save = QPushButton("저장")
        self.btn_preset_save.setObjectName("btnPresetSave")
        self.btn_preset_save.setFixedHeight(28)
        self.btn_preset_save.setMinimumWidth(70)
        self.btn_preset_save.setToolTip("현재 설정을 선택된 프리셋에 덮어씁니다")
        self.btn_preset_save.clicked.connect(self._on_preset_save)
        bar.addWidget(self.btn_preset_save)

        # + 새로
        self.btn_preset_new = QPushButton("+ 새로")
        self.btn_preset_new.setObjectName("btnPresetNew")
        self.btn_preset_new.setFixedHeight(28)
        self.btn_preset_new.setMinimumWidth(70)
        self.btn_preset_new.setToolTip("현재 설정을 새 프리셋으로 저장합니다")
        self.btn_preset_new.clicked.connect(self._on_preset_new)
        bar.addWidget(self.btn_preset_new)

        # ↩ 되돌리기
        self.btn_preset_revert = QPushButton("↩ 되돌리기")
        self.btn_preset_revert.setObjectName("btnPresetRevert")
        self.btn_preset_revert.setFixedHeight(28)
        self.btn_preset_revert.setMinimumWidth(85)
        self.btn_preset_revert.setToolTip("프리셋의 원래 설정으로 되돌립니다")
        self.btn_preset_revert.clicked.connect(self._on_preset_revert)
        bar.addWidget(self.btn_preset_revert)

        # ★ 기본 프리셋 설정
        self.btn_preset_default = QPushButton("★")
        self.btn_preset_default.setObjectName("btnPresetDefault")
        self.btn_preset_default.setFixedSize(28, 28)
        self.btn_preset_default.setToolTip("이 프리셋을 앱 시작 시 기본값으로 설정합니다")
        self.btn_preset_default.clicked.connect(self._on_preset_set_default)
        bar.addWidget(self.btn_preset_default)

        # ✕ 삭제
        self.btn_preset_delete = QPushButton("✕")
        self.btn_preset_delete.setObjectName("btnPresetDelete")
        self.btn_preset_delete.setFixedSize(28, 28)
        self.btn_preset_delete.setToolTip("선택된 프리셋을 삭제합니다")
        self.btn_preset_delete.clicked.connect(self._on_preset_delete)
        bar.addWidget(self.btn_preset_delete)

        root_layout.addLayout(bar)

        # 초기 버튼 상태
        self._update_preset_button_states()

    # ══════════════════════════════════════════════════════════════
    #  ★ 프리셋 로직
    # ══════════════════════════════════════════════════════════════

    def _init_preset_combo(self):
        """앱 시작 시 프리셋 콤보 초기화.

        우선순위:
        1. default_preset이 있으면 → 해당 프리셋 로드 + UI 적용
        2. last_preset이 있으면 → 콤보에서 선택 (UI 적용 없이 변경 여부만 체크)
        3. 둘 다 없으면 → "(선택 안 함)"
        """
        self.combo_preset.blockSignals(True)

        # 기존 항목 정리 (첫 항목 "(선택 안 함)"은 유지)
        while self.combo_preset.count() > 1:
            self.combo_preset.removeItem(1)

        # 프리셋 목록 추가 (★ 표시 포함)
        default_name = self.config.get("options", {}).get("default_preset")
        for name in list_presets():
            display = f"★ {name}" if name == default_name else name
            self.combo_preset.addItem(display, name)

        # ★ 기본 프리셋 우선, 없으면 마지막 프리셋
        target = None
        if default_name and preset_exists(default_name):
            target = default_name
        else:
            last = self.config.get("options", {}).get("last_preset")
            if last and preset_exists(last):
                target = last

        if target:
            for i in range(self.combo_preset.count()):
                if self.combo_preset.itemData(i) == target:
                    self.combo_preset.setCurrentIndex(i)
                    self._current_preset_name = target
                    self._loaded_preset_data = load_preset(target)

                    if target == default_name:
                        # ★ 기본 프리셋 → UI에 적용 (앱 시작 시 프리셋 값으로 초기화)
                        if self._loaded_preset_data:
                            self._apply_preset_to_ui(self._loaded_preset_data)
                            self._preset_modified = False
                    else:
                        # 마지막 프리셋 → 변경 여부만 체크 (UI는 config 값 유지)
                        if self._loaded_preset_data:
                            current = collect_preset_data(self)
                            self._preset_modified = preset_differs(current, self._loaded_preset_data)
                            if self._preset_modified:
                                self._update_combo_text_with_star()
                    break

        self.combo_preset.blockSignals(False)
        self._update_preset_button_states()

    def _refresh_preset_combo(self, select_name=None):
        """프리셋 콤보 새로고침 — 저장/삭제 후 호출. 기본 프리셋에 ★ 표시."""
        self.combo_preset.blockSignals(True)

        while self.combo_preset.count() > 1:
            self.combo_preset.removeItem(1)

        default_name = self.config.get("options", {}).get("default_preset")

        for name in list_presets():
            display = f"★ {name}" if name == default_name else name
            self.combo_preset.addItem(display, name)

        if select_name:
            for i in range(self.combo_preset.count()):
                if self.combo_preset.itemData(i) == select_name:
                    self.combo_preset.setCurrentIndex(i)
                    break
        else:
            self.combo_preset.setCurrentIndex(0)

        self.combo_preset.blockSignals(False)

    def _on_preset_selected(self, index):
        """프리셋 콤보 선택 변경."""
        name = self.combo_preset.itemData(index)

        if name is None:
            # "(선택 안 함)" 선택
            self._current_preset_name = None
            self._loaded_preset_data = None
            self._preset_modified = False
            self._update_preset_button_states()
            return

        # 프리셋 로드
        data = load_preset(name)
        if data is None:
            return

        # ★ 토글 변경 여부 판단 (재시작 필요 여부 결정)
        prev_display = self._display_on
        prev_audio = self._audio_on
        prev_media = self._media_on

        self._current_preset_name = name
        self._loaded_preset_data = data.copy()
        self._preset_modified = False

        # UI에 적용
        self._apply_preset_to_ui(data)

        # 엔진 실행 중이면 반영
        if self._is_running:
            toggles_changed = (
                prev_display != self._display_on
                or prev_audio != self._audio_on
                or prev_media != self._media_on
            )
            if toggles_changed:
                # 토글 조합 변경 → 엔진 재시작 필요
                self._sync_config_from_ui()
                if self._engine_ctrl and self._engine_ctrl.engine:
                    self._last_media_confirmed = getattr(
                        self._engine_ctrl.engine, '_media_detect_last_confirmed', None
                    )
                self.request_mode_switch.emit(self._get_engine_mode_string())
            else:
                # 파라미터만 변경 → 재시작 없이 즉시 반영
                self._push_params_to_engine()

        self._update_preset_button_states()

    def _on_preset_save(self):
        """저장 — 현재 프리셋에 덮어쓰기."""
        if not self._current_preset_name:
            return

        name = self._current_preset_name
        if not msg_question(
            self, "프리셋 저장",
            f"프리셋 '{name}'을(를) 현재 설정으로 덮어쓰시겠습니까?",
        ):
            return

        data = collect_preset_data(self)
        if save_preset(name, data):
            self._loaded_preset_data = data.copy()
            self._preset_modified = False
            self._update_combo_text_without_star()
            self._update_preset_button_states()

    def _on_preset_new(self):
        """+ 새로 — 새 프리셋 저장."""
        name, ok = input_text(self, "새 프리셋", "프리셋 이름을 입력하세요:")
        if not ok or not name.strip():
            return

        name = name.strip()

        # 이름 중복 확인
        if preset_exists(name):
            if not msg_question(
                self, "프리셋 중복",
                f"프리셋 '{name}'이(가) 이미 존재합니다. 덮어쓰시겠습니까?",
            ):
                return

        data = collect_preset_data(self)
        if save_preset(name, data):
            self._current_preset_name = name
            self._loaded_preset_data = data.copy()
            self._preset_modified = False

            # 콤보 새로고침 + 선택
            self._refresh_preset_combo(select_name=name)
            self._update_preset_button_states()

            # ★ 트레이 메뉴 갱신 (main_window에서 연결)
            self.config_applied.emit()

    def _on_preset_revert(self):
        """↩ 되돌리기 — 프리셋 원래 값으로 복원."""
        if not self._loaded_preset_data:
            return

        self._apply_preset_to_ui(self._loaded_preset_data)
        self._preset_modified = False
        self._update_combo_text_without_star()
        self._update_preset_button_states()

        if self._is_running:
            self._push_params_to_engine()

    def _on_preset_delete(self):
        """✕ 삭제 — 현재 프리셋 삭제."""
        if not self._current_preset_name:
            return

        name = self._current_preset_name
        if not msg_question(
            self, "프리셋 삭제",
            f"프리셋 '{name}'을(를) 삭제하시겠습니까?",
        ):
            return

        if delete_preset(name):
            # ★ 삭제된 프리셋이 기본 프리셋이면 기본 설정도 해제
            opts = self.config.get("options", {})
            if opts.get("default_preset") == name:
                opts["default_preset"] = None

            self._current_preset_name = None
            self._loaded_preset_data = None
            self._preset_modified = False

            self._refresh_preset_combo()
            self._update_preset_button_states()

            # ★ 트레이 메뉴 갱신
            self.config_applied.emit()

    def _on_preset_set_default(self):
        """★ 기본 — 현재 프리셋을 앱 시작 시 기본값으로 설정."""
        if not self._current_preset_name:
            return

        name = self._current_preset_name
        opts = self.config.setdefault("options", {})
        old_default = opts.get("default_preset")

        if old_default == name:
            # 이미 기본 → 해제
            opts["default_preset"] = None
            self.config_applied.emit()
            self._refresh_preset_combo(select_name=name)
            self._update_preset_button_states()
            return

        # 새 기본 설정
        opts["default_preset"] = name
        self.config_applied.emit()

        # 콤보 갱신 (★ 표시 반영)
        self._refresh_preset_combo(select_name=name)
        self._update_preset_button_states()

        # 버튼 피드백
        self.btn_preset_default.setText("✓")
        QTimer.singleShot(1500, lambda: self.btn_preset_default.setText("★"))

    # ── 프리셋 → UI 적용 ────────────────────────────────────────

    def _apply_preset_to_ui(self, data):
        """프리셋 dict를 전체 UI에 적용.

        순서:
        1. 토글 (blockSignals)
        2. master 밝기
        3. 각 섹션 apply_from_preset()
        4. 패널 펼침/접힘
        5. flowing 상태 동기화
        """
        # ── 1. 토글 ──
        if "display_enabled" in data:
            self._display_on = data["display_enabled"]
            self.toggle_display.blockSignals(True)
            self.toggle_display.setChecked(self._display_on)
            self.toggle_display.blockSignals(False)

        if "audio_enabled" in data:
            self._audio_on = data["audio_enabled"]
            self.toggle_audio.blockSignals(True)
            self.toggle_audio.setChecked(self._audio_on)
            self.toggle_audio.blockSignals(False)

        if "media_color_enabled" in data:
            self._media_on = data["media_color_enabled"]
            self.toggle_media.blockSignals(True)
            self.toggle_media.setChecked(self._media_on)
            self.toggle_media.blockSignals(False)

        # 미디어 토글 활성화 상태 동기화
        self.toggle_media.setEnabled(self._display_on and HAS_MEDIA_SESSION)

        # ── 2. master 밝기 ──
        if "master_brightness" in data:
            self.slider_master_brightness.blockSignals(True)
            self.slider_master_brightness.setValue(int(data["master_brightness"]))
            self.slider_master_brightness.blockSignals(False)
            self.lbl_master_brightness.setText(f"{int(data['master_brightness'])}%")

        # ── 3. 각 섹션 ──
        if hasattr(self, 'section_mirror'):
            self.section_mirror.apply_from_preset(data)
        if hasattr(self, 'section_color'):
            self.section_color.apply_from_preset(data)
        if hasattr(self, 'section_audio'):
            self.section_audio.set_display_enabled(self._display_on)
            self.section_audio.apply_from_preset(data)

        # ── 4. 패널 펼침/접힘 ──
        self._update_toggle_panels(animate=False)

        # ── 5. flowing 동기화 ──
        self._sync_flowing_state()

        # ── 6. 미러링 패널 미디어 상태 ──
        if hasattr(self, 'section_mirror'):
            self.section_mirror.set_media_active(self._media_on and self._display_on)

    # ── 변경 감지 ────────────────────────────────────────────────

    def _check_preset_modified(self):
        """현재 UI 상태와 로드된 프리셋을 비교하여 변경 여부 갱신."""
        if self._loaded_preset_data is None:
            return

        current = collect_preset_data(self)
        was_modified = self._preset_modified
        self._preset_modified = preset_differs(current, self._loaded_preset_data)

        if self._preset_modified != was_modified:
            if self._preset_modified:
                self._update_combo_text_with_star()
            else:
                self._update_combo_text_without_star()
            self._update_preset_button_states()

    def _update_combo_text_with_star(self):
        """콤보 텍스트에 * 추가."""
        if self._current_preset_name is None:
            return
        idx = self.combo_preset.currentIndex()
        text = self.combo_preset.itemText(idx)
        if not text.endswith(" *"):
            self.combo_preset.setItemText(idx, text + " *")

    def _update_combo_text_without_star(self):
        """콤보 텍스트에서 * 제거."""
        if self._current_preset_name is None:
            return
        idx = self.combo_preset.currentIndex()
        text = self.combo_preset.itemText(idx)
        if text.endswith(" *"):
            self.combo_preset.setItemText(idx, text[:-2])

    def _update_preset_button_states(self):
        """프리셋 버튼 활성/비활성 상태 갱신."""
        has_preset = self._current_preset_name is not None
        modified = self._preset_modified and has_preset

        self.btn_preset_save.setEnabled(modified)
        self.btn_preset_new.setEnabled(True)
        self.btn_preset_revert.setEnabled(modified)
        self.btn_preset_default.setEnabled(has_preset)
        self.btn_preset_delete.setEnabled(has_preset)

        # ★ 기본 버튼: QSS property로 시각적 피드백
        if has_preset:
            default_name = self.config.get("options", {}).get("default_preset")
            is_default = (self._current_preset_name == default_name)
            _set_property(self.btn_preset_default, "isDefault",
                          "true" if is_default else "false")
        else:
            _set_property(self.btn_preset_default, "isDefault", "false")

    # ── 트레이/외부에서 프리셋 선택 ──────────────────────────────

    def select_preset_by_name(self, name):
        """외부(트레이 등)에서 이름으로 프리셋 선택."""
        for i in range(self.combo_preset.count()):
            if self.combo_preset.itemData(i) == name:
                self.combo_preset.setCurrentIndex(i)
                return True
        return False

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
        self._check_preset_modified()
        if self._is_running:
            self._sync_config_from_ui()
            # ★ 엔진 재시작 전 직전 확정값 보존
            if self._engine_ctrl and self._engine_ctrl.engine:
                self._last_media_confirmed = getattr(
                    self._engine_ctrl.engine, '_media_detect_last_confirmed', None
                )
            self.request_mode_switch.emit(self._get_engine_mode_string())

    def _on_audio_toggled(self, checked):
        self._audio_on = checked
        self._update_toggle_panels(animate=True)
        if checked and self._display_on and hasattr(self, 'section_audio'):
            self.section_audio.set_display_enabled(self._display_on)
        self._sync_flowing_state()
        self._check_preset_modified()
        if self._is_running:
            self._sync_config_from_ui()
            # ★ 엔진 재시작 전 직전 확정값 보존
            if self._engine_ctrl and self._engine_ctrl.engine:
                self._last_media_confirmed = getattr(
                    self._engine_ctrl.engine, '_media_detect_last_confirmed', None
                )
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

        self._check_preset_modified()
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
        """★ 태그 + 패널 상태를 QSS property로 갱신."""

        # ── 태그 텍스트 + QSS property 갱신 ──
        if self._display_on:
            self.tag_display.setText("디스플레이 ON")
            _set_property(self.tag_display, "tagState", "on")
        else:
            self.tag_display.setText("디스플레이 OFF")
            _set_property(self.tag_display, "tagState", "off")

        if self._audio_on:
            self.tag_audio.setText("오디오 ON")
            _set_property(self.tag_audio, "tagState", "on")
        else:
            self.tag_audio.setText("오디오 OFF")
            _set_property(self.tag_audio, "tagState", "off")

        # ★ 미디어 태그 — display ON + media ON일 때만 실제 활성
        media_effective = self._media_on and self._display_on
        if media_effective:
            self.tag_media.setText("미디어 ON")
            _set_property(self.tag_media, "tagState", "on")
        else:
            self.tag_media.setText("미디어 OFF")
            _set_property(self.tag_media, "tagState", "off")

        self.panel_display_off.set_expanded(not self._display_on, animate=animate)
        self.panel_display_on.set_expanded(self._display_on, animate=animate)
        self.panel_audio_on.set_expanded(self._audio_on, animate=animate)

        # ★ 미러링 패널 소스 상태 동기화 (초기 로드 시 포함)
        if hasattr(self, 'section_mirror'):
            self.section_mirror.set_media_active(self._media_on and self._display_on)

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
            if frame is not None:
                self.section_mirror.update_media_thumbnail(frame)
            else:
                self.section_mirror.set_media_thumbnail_placeholder()

        # ★ 곡 정보를 카드 라벨 + 썸네일 툴팁에 설정
        info = provider.get_media_info()
        if hasattr(self, 'section_mirror'):
            if info:
                artist = info.get("artist", "")
                title = info.get("title", "")
                if artist and title:
                    song_text = f"♪ {artist} — {title}"
                elif title:
                    song_text = f"♪ {title}"
                else:
                    song_text = "재생 중인 미디어 없음"
            else:
                song_text = "재생 중인 미디어 없음"
            self.section_mirror.lbl_media_song.setText(song_text)
            self.section_mirror.lbl_media_thumbnail.setToolTip(song_text)

            if not info:
                from styles.palette import current as _pal_current
                self.section_mirror.lbl_media_source.setText("미디어 연동 활성")
                self.section_mirror.lbl_media_source.setStyleSheet(
                    f"color:{_pal_current()['media_active']};font-size:11px;font-weight:bold;"
                    "border:none;background:transparent;"
                )

        # ★ 현재 소스 판별 결과를 미러링 패널에 전달 (미디어 있을 때만)
        if hasattr(self, 'section_mirror') and info:
            decision = getattr(engine, '_media_detect_decision', "media")
            state = getattr(engine, '_media_detect_state', "idle")
            self.section_mirror.update_current_source(decision, state)

    def _on_refresh_thumbnail_requested(self):
        """★ 썸네일 새로고침 버튼 클릭."""
        if not self._engine_ctrl or not self._engine_ctrl.engine:
            return

        engine = self._engine_ctrl.engine
        provider = getattr(engine, '_media_provider', None)
        if provider is None:
            return

        with provider._lock:
            provider._media_hash = 0

        engine._media_detect_state = "phase1"
        engine._media_detect_decision = "media"
        engine._media_detect_start_time = __import__('time').monotonic()
        engine._media_detect_last_hash = 0
        engine._media_detect_phase1_dynamic_hits = 0
        engine._media_detect_prev_frame = None

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
        self._check_preset_modified()
        if self._is_running:
            self._push_params_to_engine()

    def _on_display_params_changed(self):
        self._check_preset_modified()
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
        self._check_preset_modified()
        if self._is_running:
            self._push_params_to_engine()

    def _on_audio_mode_changed(self, mode_key):
        self._sync_flowing_state()
        self._check_preset_modified()

    def _on_preview_toggled(self, checked):
        self.monitor_preview.setVisible(checked)
        self._preview_hint.setVisible(checked)
        self.btn_preview_toggle.setText(
            "프리뷰 숨기기" if checked else "프리뷰 보기"
        )

    # ══════════════════════════════════════════════════════════════
    #  ★ 오디오 모드 순환 (핫키/트레이 메뉴에서 호출)
    # ══════════════════════════════════════════════════════════════

    # 기본 순서 — 기본 모드 인덱스에서 rotate하여 사용
    _AUDIO_CYCLE_BASE = ["pulse", "spectrum", "bass_detail", "wave", "dynamic", "flowing"]

    # 모드 키 → 표시 이름 (트레이 알림용)
    _AUDIO_MODE_DISPLAY_NAMES = {
        "pulse": "Pulse",
        "spectrum": "Spectrum",
        "bass_detail": "Bass Detail",
        "wave": "Wave",
        "dynamic": "Dynamic",
        "flowing": "Flowing",
    }

    def cycle_audio_mode(self):
        """오디오 모드 순환 — circular 방식."""
        from ui.panels.audio_reactive_section import _MODE_TO_INDEX

        available = [m for m in self._AUDIO_CYCLE_BASE
                     if m != "flowing" or self._display_on]

        if not self._audio_on:
            # ★ 프리셋 기본 기능이 기본 모드를 대체하므로 pulse로 시작
            default_mode = "pulse"
            if default_mode not in available:
                default_mode = available[0] if available else "pulse"

            self._audio_on = True
            self.toggle_audio.blockSignals(True)
            self.toggle_audio.setChecked(True)
            self.toggle_audio.blockSignals(False)

            self.section_audio.combo_audio_mode.blockSignals(True)
            self.section_audio._mode_key = default_mode
            self.section_audio._load_mode_params(default_mode)
            self.section_audio.combo_audio_mode.setCurrentIndex(
                _MODE_TO_INDEX.get(default_mode, 0)
            )
            self.section_audio.combo_audio_mode.blockSignals(False)
            self.section_audio._update_mode_visibility()

            self._update_toggle_panels(animate=False)
            self._sync_flowing_state()
            self._check_preset_modified()

            if self._is_running:
                self._sync_config_from_ui()
                self.request_mode_switch.emit(self._get_engine_mode_string())

            return {
                "action": "on",
                "mode": default_mode,
                "display_name": self._AUDIO_MODE_DISPLAY_NAMES.get(default_mode, default_mode),
            }

        current = self.section_audio._mode_key
        default_mode = "pulse"
        if default_mode not in available:
            default_mode = available[0]

        start_idx = available.index(default_mode)
        rotated = available[start_idx:] + available[:start_idx]

        if current not in rotated:
            next_mode = rotated[0]
        else:
            current_pos = rotated.index(current)
            if current_pos >= len(rotated) - 1:
                self._audio_on = False
                self.toggle_audio.blockSignals(True)
                self.toggle_audio.setChecked(False)
                self.toggle_audio.blockSignals(False)
                self._update_toggle_panels(animate=False)
                self._sync_flowing_state()
                self._check_preset_modified()
                if self._is_running:
                    self._sync_config_from_ui()
                    self.request_mode_switch.emit(self._get_engine_mode_string())
                return {"action": "off", "mode": "", "display_name": ""}
            else:
                next_mode = rotated[current_pos + 1]

        self.section_audio._save_mode_params(current)
        self.section_audio._mode_key = next_mode
        self.section_audio._load_mode_params(next_mode)
        self.section_audio.combo_audio_mode.blockSignals(True)
        self.section_audio.combo_audio_mode.setCurrentIndex(
            _MODE_TO_INDEX.get(next_mode, 0)
        )
        self.section_audio.combo_audio_mode.blockSignals(False)
        self.section_audio._update_mode_visibility()
        self._sync_flowing_state()
        self._check_preset_modified()

        if self._is_running:
            self._push_params_to_engine()

        return {
            "action": "on",
            "mode": next_mode,
            "display_name": self._AUDIO_MODE_DISPLAY_NAMES.get(next_mode, next_mode),
        }

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
    def current_preset_name(self) -> str:
        """현재 선택된 프리셋 이름 (None이면 미선택)."""
        return self._current_preset_name

    @property
    def saved_config(self):
        """★ auto-save: 현재 config를 직접 반환 (스냅샷 불필요)."""
        return self.config

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
        self.btn_pause.setText("▶ 재개" if is_paused else "∥ 일시정지")

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

    def _update_resource_usage(self):
        """★ CPU/RAM 갱신 — QSS property로 CPU 색상 제어."""
        try:
            cpu = self._process.cpu_percent() / psutil.cpu_count()
            mem_info = self._process.memory_full_info()
            ram = getattr(mem_info, 'uss', mem_info.rss) / (1024 * 1024)
            self.cpu_label.setText(f"CPU: {cpu:.1f}%")
            self.ram_label.setText(f"RAM: {ram:.0f} MB")

            # ★ QSS property로 색상 전환 (dark.qss의 QLabel#cpuLabel[level=...])
            level = "danger" if cpu >= 20 else "warning" if cpu >= 10 else "normal"
            _set_property(self.cpu_label, "level", level)
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
