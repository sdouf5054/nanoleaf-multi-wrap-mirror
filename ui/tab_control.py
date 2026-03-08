"""통합 컨트롤 탭 — 미러링 + 오디오 + 하이브리드 통합 UI

[Step 7] 기본 골격
- 상태바 (실행 상태, CPU/RAM, FPS)
- 제어 버튼 (시작/일시정지/중지)
- 모드 선택 (미러링/하이브리드/오디오) — 라디오 버튼 스타일
- LED 프리뷰 (MonitorPreview, 모든 모드에서 접기/펼치기)
- 모드별 패널 (QStackedWidget — Step 8-10에서 채움)
- 공통 설정 (화면 방향, Target FPS, 오디오 디바이스)
- 저장 로직 ("💾 적용" / "↩ 되돌리기")

[Step 8] 미러링 모드 패널
- 밝기 슬라이더 (0~100%)
- 스무딩 체크박스 + 계수 스핀박스
- 감쇠 반경 + 타원 페널티 (전역)
- 변별 오버라이드 (per-side 감쇠/페널티)
- 실행 중 실시간 반영 (감쇠/페널티/스무딩은 디바운스 300ms)
- 적용/되돌리기에 미러링 값 포함

[Step 9] 오디오 모드 패널
- 에너지 레벨 바 (bass/mid/high) + 스펙트럼 위젯
- 색상 팔레트 (프리셋 10개 + 커스텀 + 무지개)
- 비주얼라이저 모드 (pulse/spectrum/bass_detail) — 모드 전환 시 파라미터 자동 저장/로드
- 감도 (bass + spectrum 전용 mid/high), 밝기, attack/release
- 대역 비율 (ZoneBalanceWidget — spectrum/bass_detail 전용)
- audio_params_changed 시그널로 실행 중 엔진에 반영
- 적용/되돌리기에 오디오 값 포함

[Step 11] 공통 설정 + 저장 로직
- "💾 적용" → config.json 저장 (config_applied 시그널) + 엔진에 파라미터 push
- "↩ 되돌리기" → 마지막 적용 시점으로 전체 UI 복원
- "▶ 시작" → 자동 적용 후 엔진 시작
- request_engine_pause 시그널 추가 (일시정지/재개)
- collect_engine_init_params(): 엔진 초기화용 전체 파라미터 dict 수집
- apply_to_running_engine(): 실행 중 엔진에 현재 UI 값 push
- "적용" 버튼을 누를 때만 config.json 저장 + 엔진에 반영
- "되돌리기" → 마지막 적용 시점의 값으로 UI 복원
- 기존 오디오 탭의 편집 잠금 모드(🔒) → 제거 ("적용" 버튼이 대체)
- 모드 전환 시 엔진 재시작 (소스 변경이 필요하므로)

Signals:
    request_engine_start(str): 모드 문자열과 함께 엔진 시작 요청
    request_engine_stop(): 엔진 중지 요청
    mirror_layout_params_changed(dict): 실행 중 감쇠/페널티 변경 (디바운스)
    mirror_brightness_changed(int): 실행 중 밝기 변경
    mirror_smoothing_changed(bool): 스무딩 on/off 변경
    mirror_smoothing_factor_changed(float): 스무딩 계수 변경
"""

import os
import copy
import psutil
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QComboBox, QFrame, QScrollArea, QStackedWidget,
    QButtonGroup, QSizePolicy, QSpinBox, QDoubleSpinBox,
    QCheckBox, QGridLayout, QSlider, QColorDialog, QProgressBar,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QEvent
from PyQt5.QtGui import QColor

from core.audio_engine import list_loopback_devices, HAS_PYAUDIO
from core.engine import (
    MODE_MIRROR, MODE_AUDIO, MODE_HYBRID,
    AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    COLOR_SOURCE_SOLID, COLOR_SOURCE_SCREEN, N_ZONES_PER_LED,
)
from ui.widgets.monitor_preview import MonitorPreview
from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.spectrum import SpectrumWidget
from ui.widgets.zone_balance import ZoneBalanceWidget


# ── 모드 인덱스 ──────────────────────────────────────────────────
_MODE_INDEX = {MODE_MIRROR: 0, MODE_HYBRID: 1, MODE_AUDIO: 2}
_INDEX_MODE = {0: MODE_MIRROR, 1: MODE_HYBRID, 2: MODE_AUDIO}

# ── 오디오 서브모드 인덱스 ────────────────────────────────────────
_AUDIO_MODE_INDEX = {AUDIO_PULSE: 0, AUDIO_SPECTRUM: 1, AUDIO_BASS_DETAIL: 2}
_INDEX_AUDIO_MODE = {0: AUDIO_PULSE, 1: AUDIO_SPECTRUM, 2: AUDIO_BASS_DETAIL}

# ── 색상 프리셋 ──────────────────────────────────────────────────
_COLOR_PRESETS = [
    ("🌈 무지개", None, None, None),
    ("핑크/마젠타", 255, 0, 80), ("빨강", 255, 30, 0),
    ("주황", 255, 120, 0), ("노랑", 255, 220, 0),
    ("초록", 0, 255, 80), ("시안", 0, 220, 255),
    ("파랑", 30, 0, 255), ("보라", 150, 0, 255),
    ("흰색", 255, 255, 255),
]

# ── 오디오 모드별 기본 파라미터 ───────────────────────────────────
_AUDIO_DEFAULTS = {
    "pulse": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100, "attack": 50, "release": 50,
        "zone_bass": 33, "zone_mid": 33, "zone_high": 34,
    },
    "spectrum": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100, "attack": 50, "release": 50,
        "zone_bass": 33, "zone_mid": 33, "zone_high": 34,
    },
    "bass_detail": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100, "attack": 10, "release": 70,
        "zone_bass": 48, "zone_mid": 26, "zone_high": 26,
    },
}

# ── 구역 수 선택지 (하이브리드 화면 연동) ─────────────────────────
_ZONE_OPTIONS = [
    (1, "1구역 (화면 전체 평균)"), (2, "2구역 (상/하)"),
    (4, "4구역 (상하좌우)"), (8, "8구역 (모서리 포함)"),
    (16, "16구역"), (32, "32구역"),
    (N_ZONES_PER_LED, "LED별 개별 (미러링)"),
]


class _NoScrollFilter(QObject):
    """마우스 휠로 위젯 값이 변경되는 것을 방지하는 이벤트 필터.

    QComboBox, QSpinBox, QDoubleSpinBox, QSlider 등에 설치하여
    스크롤 영역 내에서 의도치 않은 값 변경을 막습니다.
    """

    _FILTERED_TYPES = (QComboBox, QSpinBox, QDoubleSpinBox, QSlider)

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Wheel
                and isinstance(obj, self._FILTERED_TYPES)):
            event.ignore()
            return True
        return False


class _ModeButton(QPushButton):
    """모드 선택용 토글 버튼 — 라디오 버튼처럼 동작."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._update_style()

    def _update_style(self):
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
    """통합 컨트롤 탭 — 미러링/오디오/하이브리드 모드를 하나의 탭에서 제어.

    Step 7: 기본 골격 구현.
    Step 8-10: 모드별 패널 채움.
    Step 11: 공통 설정 + 저장 로직 완성.
    """

    # MainWindow로 전달되는 시그널
    request_engine_start = pyqtSignal(str)    # 모드 문자열
    request_engine_stop = pyqtSignal()
    request_engine_pause = pyqtSignal()       # 일시정지/재개 토글
    request_mode_switch = pyqtSignal(str)     # 실행 중 모드 전환 요청
    config_applied = pyqtSignal()             # config.json 저장 요청

    # 미러링 실시간 반영 시그널 (Step 8)
    mirror_layout_params_changed = pyqtSignal(dict)
    mirror_brightness_changed = pyqtSignal(int)
    mirror_smoothing_changed = pyqtSignal(bool)
    mirror_smoothing_factor_changed = pyqtSignal(float)
    mirror_zone_count_changed = pyqtSignal(int)

    # 오디오 실시간 반영 시그널 (Step 9)
    audio_params_changed = pyqtSignal(dict)
    audio_min_brightness_changed = pyqtSignal(float)

    # 하이브리드 실시간 반영 시그널 (Step 10)
    hybrid_params_changed = pyqtSignal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._is_running = False
        self._current_mode = MODE_MIRROR

        # 오디오 상태
        self._audio_current_color = (255, 0, 80)
        self._audio_is_rainbow = True
        self._audio_mode_key = "pulse"  # 현재 오디오 서브모드 config 키

        # 하이브리드 상태
        self._hybrid_mode_key = "pulse"  # 현재 하이브리드 서브모드 config 키

        # 마지막 "적용" 시점의 config 스냅샷 (되돌리기용)
        self._applied_snapshot = copy.deepcopy(config)

        # config에 오디오 키 확보
        for key in ("audio_pulse", "audio_spectrum", "audio_bass_detail"):
            if key not in self.config:
                mode_name = key.replace("audio_", "")
                self.config[key] = dict(_AUDIO_DEFAULTS.get(mode_name, _AUDIO_DEFAULTS["pulse"]))

        self._build_ui()

        # 미러링 레이아웃 파라미터 디바운스 타이머 (300ms)
        self._layout_debounce = QTimer(self)
        self._layout_debounce.setSingleShot(True)
        self._layout_debounce.setInterval(300)
        self._layout_debounce.timeout.connect(self._emit_mirror_layout_params)

        # 자원 모니터링 타이머
        self._process = psutil.Process(os.getpid())
        self._process.cpu_percent()  # 첫 호출 기준값
        self._res_timer = QTimer(self)
        self._res_timer.timeout.connect(self._update_resource_usage)
        self._res_timer.start(2000)

    # ══════════════════════════════════════════════════════════════
    #  UI 빌드
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        # 스크롤 영역
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 4, 6, 4)

        # 컴팩트 그룹박스 스타일 — 내부 패딩 축소
        container.setStyleSheet(
            "QGroupBox { padding-top: 14px; margin-top: 4px; }"
            "QGroupBox::title { subcontrol-position: top left;"
            " padding: 0 4px; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll.setWidget(container)

        # ═══ 1. 상태 ═══
        self._build_status_section(layout)

        # ═══ 2. 제어 버튼 ═══
        self._build_control_buttons(layout)

        # ═══ 3. 모드 선택 ═══
        self._build_mode_selector(layout)

        # ═══ 4. LED 프리뷰 ═══
        self._build_preview_section(layout)

        # ═══ 5. 모드별 패널 (QStackedWidget) ═══
        self._build_mode_panels(layout)

        # ═══ 6. 공통 설정 ═══
        self._build_common_settings(layout)

        # ═══ 7. 적용/되돌리기 ═══
        self._build_action_buttons(layout)

        layout.addStretch()

        # 모든 QComboBox/QSpinBox/QDoubleSpinBox/QSlider에
        # 마우스 휠 방지 필터 설치
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
        self.cpu_label.setStyleSheet(
            "font-size: 12px; color: #d35400; margin-right: 6px;"
        )
        sl.addWidget(self.cpu_label)

        self.ram_label = QLabel("RAM: — MB")
        self.ram_label.setStyleSheet(
            "font-size: 12px; color: #27ae60; margin-right: 10px;"
        )
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
        self.btn_pause.clicked.connect(self._on_pause_clicked)
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
        self.btn_stop.clicked.connect(self._on_stop_clicked)
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
            (MODE_MIRROR,  "🖥  미러링"),
            (MODE_HYBRID,  "🎵+🖥  하이브리드"),
            (MODE_AUDIO,   "🎵  오디오"),
        ]

        for mode_key, label in modes:
            btn = _ModeButton(label)
            idx = _MODE_INDEX[mode_key]
            self._mode_buttons.addButton(btn, idx)
            ml.addWidget(btn)

        # 기본 선택: 미러링
        self._mode_buttons.button(_MODE_INDEX[MODE_MIRROR]).setChecked(True)
        self._mode_buttons.idClicked.connect(self._on_mode_changed)

        parent_layout.addWidget(mg)

    # ── 4. LED 프리뷰 ────────────────────────────────────────────

    def _build_preview_section(self, parent_layout):
        pg = QGroupBox("LED 프리뷰")
        pl = QVBoxLayout(pg)
        pl.setContentsMargins(6, 16, 6, 4)
        pl.setSpacing(2)

        # 접기/펼치기 버튼
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

        # MonitorPreview 위젯
        self.monitor_preview = MonitorPreview(self.config)
        self.monitor_preview.setVisible(False)
        pl.addWidget(self.monitor_preview)

        parent_layout.addWidget(pg)

    # ── 5. 모드별 패널 ───────────────────────────────────────────

    def _build_mode_panels(self, parent_layout):
        """QStackedWidget로 모드별 패널을 전환.

        Step 7: 빈 플레이스홀더
        Step 8: 미러링 패널 채움
        Step 9: 오디오 패널 채움
        Step 10: 하이브리드 패널 채움
        """
        self.mode_stack = QStackedWidget()
        self.mode_stack.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Maximum
        )

        # 미러링 패널 (index 0)
        self.panel_mirror = QWidget()
        self._build_mirror_panel(self.panel_mirror)
        self.mode_stack.addWidget(self.panel_mirror)

        # 하이브리드 패널 (index 1)
        self.panel_hybrid = QWidget()
        self._build_hybrid_panel(self.panel_hybrid)
        self.mode_stack.addWidget(self.panel_hybrid)

        # 오디오 패널 (index 2)
        self.panel_audio = QWidget()
        self._build_audio_panel(self.panel_audio)
        self.mode_stack.addWidget(self.panel_audio)

        self.mode_stack.setCurrentIndex(0)
        self.mode_stack.currentChanged.connect(self._adjust_stack_size)
        self._adjust_stack_size(0)  # 초기 크기 조정
        parent_layout.addWidget(self.mode_stack)

    def _build_mirror_panel(self, panel):
        """미러링 모드 패널 — 구역 수, 밝기/스무딩, 감쇠/페널티."""
        mirror_cfg = self.config.get("mirror", {})
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(2)

        # ── 구역 수 (per-LED vs zone 기반) ──
        zone_row = QHBoxLayout()
        zone_row.addWidget(QLabel("구역 수:"))
        self.mirror_combo_zone_count = QComboBox()
        self.mirror_combo_zone_count.addItem("LED별 개별 (기본)", N_ZONES_PER_LED)
        for n, label in _ZONE_OPTIONS:
            if n != N_ZONES_PER_LED:
                self.mirror_combo_zone_count.addItem(label, n)
        self.mirror_combo_zone_count.currentIndexChanged.connect(
            self._on_mirror_zone_count_changed
        )
        zone_row.addWidget(self.mirror_combo_zone_count)
        zone_row.addStretch()
        layout.addLayout(zone_row)

        # ── 밝기 + 스무딩 ──
        ctrl_group = QGroupBox("밝기 / 스무딩")
        cl = QVBoxLayout(ctrl_group)
        cl.setSpacing(2)
        cl.setContentsMargins(6, 14, 6, 2)

        bright_row = QHBoxLayout()
        bright_row.addWidget(QLabel("밝기:"))
        self.mirror_brightness_slider = QSlider(Qt.Horizontal)
        self.mirror_brightness_slider.setRange(0, 100)
        self.mirror_brightness_slider.setValue(
            int(mirror_cfg.get("brightness", 1.0) * 100)
        )
        self.mirror_brightness_slider.valueChanged.connect(
            self._on_mirror_brightness_changed
        )
        bright_row.addWidget(self.mirror_brightness_slider)
        self.mirror_brightness_label = QLabel(
            f'{int(mirror_cfg.get("brightness", 1.0) * 100)}%'
        )
        self.mirror_brightness_label.setMinimumWidth(35)
        self.mirror_brightness_label.setAlignment(
            Qt.AlignRight | Qt.AlignVCenter
        )
        bright_row.addWidget(self.mirror_brightness_label)
        cl.addLayout(bright_row)

        smooth_row = QHBoxLayout()
        self.mirror_chk_smoothing = QCheckBox("스무딩")
        self.mirror_chk_smoothing.setChecked(True)
        self.mirror_chk_smoothing.stateChanged.connect(
            self._on_mirror_smoothing_changed
        )
        smooth_row.addWidget(self.mirror_chk_smoothing)
        smooth_row.addWidget(QLabel("계수:"))
        self.mirror_spin_smoothing = QDoubleSpinBox()
        self.mirror_spin_smoothing.setRange(0.0, 0.95)
        self.mirror_spin_smoothing.setSingleStep(0.05)
        self.mirror_spin_smoothing.setValue(
            mirror_cfg.get("smoothing_factor", 0.5)
        )
        self.mirror_spin_smoothing.valueChanged.connect(
            self._on_mirror_smoothing_factor_changed
        )
        smooth_row.addWidget(self.mirror_spin_smoothing)
        smooth_row.addStretch()
        cl.addLayout(smooth_row)

        layout.addWidget(ctrl_group)

        # ── 감쇠 / 페널티 ──
        decay_group = QGroupBox("감쇠 / 타원 페널티")
        decay_layout = QVBoxLayout(decay_group)
        decay_layout.setSpacing(3)
        decay_layout.setContentsMargins(6, 16, 6, 4)

        # 전역 값
        global_row = QHBoxLayout()
        global_row.addWidget(QLabel("감쇠 반경:"))
        self.mirror_spin_decay = QDoubleSpinBox()
        self.mirror_spin_decay.setRange(0.05, 1.0)
        self.mirror_spin_decay.setSingleStep(0.05)
        self.mirror_spin_decay.setValue(
            mirror_cfg.get("decay_radius", 0.3)
        )
        self.mirror_spin_decay.valueChanged.connect(
            self._on_mirror_layout_param_changed
        )
        global_row.addWidget(self.mirror_spin_decay)
        global_row.addWidget(QLabel("타원 페널티:"))
        self.mirror_spin_penalty = QDoubleSpinBox()
        self.mirror_spin_penalty.setRange(1.0, 10.0)
        self.mirror_spin_penalty.setSingleStep(0.5)
        self.mirror_spin_penalty.setValue(
            mirror_cfg.get("parallel_penalty", 5.0)
        )
        self.mirror_spin_penalty.valueChanged.connect(
            self._on_mirror_layout_param_changed
        )
        global_row.addWidget(self.mirror_spin_penalty)
        global_row.addStretch()
        decay_layout.addLayout(global_row)

        # 변별 오버라이드
        per_decay = mirror_cfg.get("decay_radius_per_side", {})
        per_penalty = mirror_cfg.get("parallel_penalty_per_side", {})
        has_per_side = bool(per_decay or per_penalty)

        self.mirror_chk_per_side = QCheckBox("변별 값 사용")
        self.mirror_chk_per_side.setChecked(has_per_side)
        self.mirror_chk_per_side.stateChanged.connect(
            self._on_mirror_layout_param_changed
        )
        decay_layout.addWidget(self.mirror_chk_per_side)

        # 변별 그리드
        per_side_grid = QGridLayout()
        per_side_grid.setSpacing(2)
        sides = ["top", "bottom", "left", "right"]
        side_labels = {
            "top": "상단", "bottom": "하단",
            "left": "좌측", "right": "우측",
        }

        per_side_grid.addWidget(QLabel(""), 0, 0)
        per_side_grid.addWidget(QLabel("감쇠 반경"), 0, 1)
        per_side_grid.addWidget(QLabel("타원 페널티"), 0, 2)

        self.mirror_spin_decay_per = {}
        self.mirror_spin_penalty_per = {}

        for row_i, side in enumerate(sides, 1):
            per_side_grid.addWidget(QLabel(side_labels[side]), row_i, 0)

            sp_d = QDoubleSpinBox()
            sp_d.setRange(0.05, 1.0)
            sp_d.setSingleStep(0.05)
            sp_d.setValue(
                per_decay.get(side, mirror_cfg.get("decay_radius", 0.3))
            )
            sp_d.valueChanged.connect(self._on_mirror_layout_param_changed)
            self.mirror_spin_decay_per[side] = sp_d
            per_side_grid.addWidget(sp_d, row_i, 1)

            sp_p = QDoubleSpinBox()
            sp_p.setRange(1.0, 10.0)
            sp_p.setSingleStep(0.5)
            sp_p.setValue(
                per_penalty.get(
                    side, mirror_cfg.get("parallel_penalty", 5.0)
                )
            )
            sp_p.valueChanged.connect(self._on_mirror_layout_param_changed)
            self.mirror_spin_penalty_per[side] = sp_p
            per_side_grid.addWidget(sp_p, row_i, 2)

        self.mirror_per_side_widget = QWidget()
        self.mirror_per_side_widget.setLayout(per_side_grid)
        self.mirror_per_side_widget.setVisible(has_per_side)
        self.mirror_chk_per_side.stateChanged.connect(
            lambda s: self.mirror_per_side_widget.setVisible(bool(s))
        )
        decay_layout.addWidget(self.mirror_per_side_widget)

        layout.addWidget(decay_group)

    def _build_hybrid_panel(self, panel):
        """하이브리드 모드 패널 — 에너지 + 화면 연동 + 오디오 파라미터."""
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        # ── 에너지 레벨 ──
        energy_group = QGroupBox("에너지 레벨")
        hel = QVBoxLayout(energy_group)
        hel.setSpacing(3)
        hel.setContentsMargins(6, 16, 6, 4)
        heg = QGridLayout()
        self.hybrid_bar_bass = self._make_progress_bar(heg, 0, "Bass", "#e74c3c")
        self.hybrid_bar_mid = self._make_progress_bar(heg, 1, "Mid", "#27ae60")
        self.hybrid_bar_high = self._make_progress_bar(heg, 2, "High", "#3498db")
        hel.addLayout(heg)

        # 스펙트럼 (16밴드) — 오디오 탭과 동일
        hel.addWidget(QLabel("스펙트럼 (16밴드)"))
        self.hybrid_spectrum_widget = SpectrumWidget(16)
        hel.addWidget(self.hybrid_spectrum_widget)

        layout.addWidget(energy_group)

        # ── 화면 연동 설정 ──
        screen_group = QGroupBox("화면 연동")
        scl = QVBoxLayout(screen_group)
        scl.setSpacing(3)
        scl.setContentsMargins(6, 16, 6, 4)

        # 구역 수
        zcr = QHBoxLayout()
        zcr.addWidget(QLabel("구역 수:"))
        self.hybrid_combo_zone_count = QComboBox()
        for n, label in _ZONE_OPTIONS:
            self.hybrid_combo_zone_count.addItem(label, n)
        self.hybrid_combo_zone_count.currentIndexChanged.connect(
            self._on_hybrid_zone_count_changed
        )
        zcr.addWidget(self.hybrid_combo_zone_count)
        zcr.addStretch()
        scl.addLayout(zcr)

        # 최소 밝기
        mbr = QHBoxLayout()
        mbr.addWidget(QLabel("최소 밝기:"))
        self.hybrid_slider_min_brightness = NoScrollSlider(Qt.Horizontal)
        self.hybrid_slider_min_brightness.setRange(0, 100)
        self.hybrid_slider_min_brightness.setValue(5)
        self.hybrid_slider_min_brightness.valueChanged.connect(
            self._on_hybrid_min_brightness_changed
        )
        mbr.addWidget(self.hybrid_slider_min_brightness)
        self.hybrid_lbl_min_brightness = QLabel("5%")
        self.hybrid_lbl_min_brightness.setMinimumWidth(35)
        self.hybrid_lbl_min_brightness.setAlignment(
            Qt.AlignRight | Qt.AlignVCenter
        )
        mbr.addWidget(self.hybrid_lbl_min_brightness)
        scl.addLayout(mbr)

        layout.addWidget(screen_group)

        # ── 비주얼라이저 모드 ──
        mode_group = QGroupBox("비주얼라이저 모드")
        hml = QVBoxLayout(mode_group)
        hml.setContentsMargins(6, 16, 6, 4)
        self.hybrid_combo_mode = QComboBox()
        self.hybrid_combo_mode.addItems([
            "🔴 Bass 반응", "🌈 Spectrum", "🔊 Bass Detail",
        ])
        self.hybrid_combo_mode.currentIndexChanged.connect(
            self._on_hybrid_audio_mode_changed
        )
        hml.addWidget(self.hybrid_combo_mode)
        layout.addWidget(mode_group)

        # ── 파라미터 ──
        param_group = QGroupBox("파라미터")
        hpl = QVBoxLayout(param_group)
        hpl.setSpacing(3)
        hpl.setContentsMargins(6, 16, 6, 4)

        self.hybrid_label_sens = QLabel("감도 (Bass)")
        hpl.addWidget(self.hybrid_label_sens)
        self.hybrid_slider_bass_sens, self.hybrid_lbl_bass_sens = \
            self._add_audio_slider(hpl, "Bass:", 10, 300, 100)

        self.hybrid_row_mid_sens = QWidget()
        rm = QHBoxLayout(self.hybrid_row_mid_sens)
        rm.setContentsMargins(0, 0, 0, 0)
        rm.addWidget(QLabel("Mid:"))
        self.hybrid_slider_mid_sens = NoScrollSlider(Qt.Horizontal)
        self.hybrid_slider_mid_sens.setRange(10, 300)
        self.hybrid_slider_mid_sens.setValue(100)
        rm.addWidget(self.hybrid_slider_mid_sens)
        self.hybrid_lbl_mid_sens = QLabel("1.00")
        self.hybrid_lbl_mid_sens.setMinimumWidth(40)
        self.hybrid_lbl_mid_sens.setAlignment(
            Qt.AlignRight | Qt.AlignVCenter
        )
        rm.addWidget(self.hybrid_lbl_mid_sens)
        hpl.addWidget(self.hybrid_row_mid_sens)

        self.hybrid_row_high_sens = QWidget()
        rh = QHBoxLayout(self.hybrid_row_high_sens)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(QLabel("High:"))
        self.hybrid_slider_high_sens = NoScrollSlider(Qt.Horizontal)
        self.hybrid_slider_high_sens.setRange(10, 300)
        self.hybrid_slider_high_sens.setValue(100)
        rh.addWidget(self.hybrid_slider_high_sens)
        self.hybrid_lbl_high_sens = QLabel("1.00")
        self.hybrid_lbl_high_sens.setMinimumWidth(40)
        self.hybrid_lbl_high_sens.setAlignment(
            Qt.AlignRight | Qt.AlignVCenter
        )
        rh.addWidget(self.hybrid_lbl_high_sens)
        hpl.addWidget(self.hybrid_row_high_sens)

        ln = QFrame()
        ln.setFrameShape(QFrame.HLine)
        ln.setFrameShadow(QFrame.Sunken)
        hpl.addWidget(ln)
        self.hybrid_slider_brightness, self.hybrid_lbl_brightness = \
            self._add_audio_slider(hpl, "밝기:", 0, 100, 100, suffix="%")

        hpl.addWidget(QLabel("반응 특성"))
        self.hybrid_slider_attack, self.hybrid_lbl_attack = \
            self._add_audio_slider(hpl, "Attack:", 0, 100, 50)
        self.hybrid_slider_release, self.hybrid_lbl_release = \
            self._add_audio_slider(hpl, "Release:", 0, 100, 50)

        # 대역 비율
        self.hybrid_zone_line = QFrame()
        self.hybrid_zone_line.setFrameShape(QFrame.HLine)
        self.hybrid_zone_line.setFrameShadow(QFrame.Sunken)
        hpl.addWidget(self.hybrid_zone_line)
        self.hybrid_zone_label = QLabel("대역 비율 (주파수 분배)")
        self.hybrid_zone_label.setStyleSheet("font-weight:bold;")
        hpl.addWidget(self.hybrid_zone_label)
        self.hybrid_zone_balance = ZoneBalanceWidget(33, 33, 34)
        hpl.addWidget(self.hybrid_zone_balance)

        layout.addWidget(param_group)

        # spectrum 전용 위젯 목록
        self._hybrid_spectrum_only = [
            self.hybrid_row_mid_sens, self.hybrid_row_high_sens,
            self.hybrid_zone_line, self.hybrid_zone_label,
            self.hybrid_zone_balance,
        ]

        # 초기 모드 UI
        self._update_hybrid_mode_ui("pulse")
        self._load_hybrid_mode_params("pulse")

    def _build_audio_panel(self, panel):
        """오디오 모드 패널 — 에너지, 색상, 모드, 파라미터, 대역 비율."""
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)

        # ── 에너지 레벨 ──
        energy_group = QGroupBox("에너지 레벨")
        el = QVBoxLayout(energy_group)
        eg = QGridLayout()
        self.audio_bar_bass = self._make_progress_bar(eg, 0, "Bass", "#e74c3c")
        self.audio_bar_mid = self._make_progress_bar(eg, 1, "Mid", "#27ae60")
        self.audio_bar_high = self._make_progress_bar(eg, 2, "High", "#3498db")
        el.addLayout(eg)
        el.addWidget(QLabel("스펙트럼 (16밴드)"))
        self.audio_spectrum_widget = SpectrumWidget(n_bands=16)
        el.addWidget(self.audio_spectrum_widget)
        layout.addWidget(energy_group)

        # ── 색상 팔레트 ──
        color_group = QGroupBox("색상")
        cl = QVBoxLayout(color_group)
        pg = QGridLayout()
        for i, (name, r, g, b) in enumerate(_COLOR_PRESETS):
            btn = QPushButton(name)
            btn.setMinimumHeight(26)
            if r is None:
                btn.setStyleSheet(
                    "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                    "stop:0 red,stop:0.17 orange,stop:0.33 yellow,"
                    "stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,"
                    "stop:1 purple);color:white;font-weight:bold;"
                    "border-radius:4px;font-size:11px;"
                )
                btn.clicked.connect(lambda _: self._audio_set_rainbow())
            else:
                tc = "#000" if (r + g + b) > 380 else "#fff"
                btn.setStyleSheet(
                    f"background:rgb({r},{g},{b});color:{tc};"
                    f"font-weight:bold;border-radius:4px;font-size:11px;"
                )
                btn.clicked.connect(
                    lambda _, rgb=(r, g, b): self._audio_set_color(*rgb)
                )
            pg.addWidget(btn, i // 5, i % 5)
        cl.addLayout(pg)

        cr = QHBoxLayout()
        btn_custom = QPushButton("🎨 커스텀")
        btn_custom.clicked.connect(self._audio_pick_custom_color)
        cr.addWidget(btn_custom)
        self.audio_color_preview = QFrame()
        self.audio_color_preview.setFixedSize(40, 26)
        self.audio_color_preview.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 red,stop:0.17 orange,stop:0.33 yellow,"
            "stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,"
            "stop:1 purple);border:1px solid #555;border-radius:4px;"
        )
        cr.addWidget(self.audio_color_preview)
        cr.addStretch()
        cl.addLayout(cr)

        # 최소 밝기
        ambr = QHBoxLayout()
        ambr.addWidget(QLabel("최소 밝기:"))
        self.audio_slider_min_brightness = NoScrollSlider(Qt.Horizontal)
        self.audio_slider_min_brightness.setRange(0, 100)
        self.audio_slider_min_brightness.setValue(2)
        self.audio_slider_min_brightness.valueChanged.connect(
            self._on_audio_min_brightness_changed
        )
        ambr.addWidget(self.audio_slider_min_brightness)
        self.audio_lbl_min_brightness = QLabel("2%")
        self.audio_lbl_min_brightness.setMinimumWidth(35)
        self.audio_lbl_min_brightness.setAlignment(
            Qt.AlignRight | Qt.AlignVCenter
        )
        ambr.addWidget(self.audio_lbl_min_brightness)
        cl.addLayout(ambr)

        layout.addWidget(color_group)

        # ── 비주얼라이저 모드 ──
        mode_group = QGroupBox("비주얼라이저 모드")
        ml = QVBoxLayout(mode_group)
        self.audio_combo_mode = QComboBox()
        self.audio_combo_mode.addItems([
            "🔴 Bass 반응 — 저음 기반 전체 밝기",
            "🌈 Spectrum — 16밴드 주파수 매핑",
            "🔊 Bass Detail — 저역 세밀 16밴드",
        ])
        self.audio_combo_mode.currentIndexChanged.connect(
            self._on_audio_mode_changed
        )
        ml.addWidget(self.audio_combo_mode)
        layout.addWidget(mode_group)

        # ── 파라미터 ──
        param_group = QGroupBox("파라미터")
        pl = QVBoxLayout(param_group)

        # 감도
        self.audio_label_sens = QLabel("감도 (Bass)")
        pl.addWidget(self.audio_label_sens)
        self.audio_slider_bass_sens, self.audio_lbl_bass_sens = \
            self._add_audio_slider(pl, "Bass:", 10, 300, 100)

        self.audio_row_mid_sens = QWidget()
        rm = QHBoxLayout(self.audio_row_mid_sens)
        rm.setContentsMargins(0, 0, 0, 0)
        rm.addWidget(QLabel("Mid:"))
        self.audio_slider_mid_sens = NoScrollSlider(Qt.Horizontal)
        self.audio_slider_mid_sens.setRange(10, 300)
        self.audio_slider_mid_sens.setValue(100)
        rm.addWidget(self.audio_slider_mid_sens)
        self.audio_lbl_mid_sens = QLabel("1.00")
        self.audio_lbl_mid_sens.setMinimumWidth(40)
        self.audio_lbl_mid_sens.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        rm.addWidget(self.audio_lbl_mid_sens)
        pl.addWidget(self.audio_row_mid_sens)

        self.audio_row_high_sens = QWidget()
        rh = QHBoxLayout(self.audio_row_high_sens)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(QLabel("High:"))
        self.audio_slider_high_sens = NoScrollSlider(Qt.Horizontal)
        self.audio_slider_high_sens.setRange(10, 300)
        self.audio_slider_high_sens.setValue(100)
        rh.addWidget(self.audio_slider_high_sens)
        self.audio_lbl_high_sens = QLabel("1.00")
        self.audio_lbl_high_sens.setMinimumWidth(40)
        self.audio_lbl_high_sens.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        rh.addWidget(self.audio_lbl_high_sens)
        pl.addWidget(self.audio_row_high_sens)

        # 밝기
        ln = QFrame()
        ln.setFrameShape(QFrame.HLine)
        ln.setFrameShadow(QFrame.Sunken)
        pl.addWidget(ln)
        self.audio_slider_brightness, self.audio_lbl_brightness = \
            self._add_audio_slider(pl, "밝기:", 0, 100, 100, suffix="%")

        # Attack / Release
        pl.addWidget(QLabel("반응 특성"))
        self.audio_slider_attack, self.audio_lbl_attack = \
            self._add_audio_slider(pl, "Attack:", 0, 100, 50)
        self.audio_slider_release, self.audio_lbl_release = \
            self._add_audio_slider(pl, "Release:", 0, 100, 50)
        ht = QLabel("Attack ↑ = 빠르게 반응  |  Release ↑ = 긴 잔향")
        ht.setStyleSheet("color:#888;font-size:10px;")
        ht.setWordWrap(True)
        pl.addWidget(ht)

        # 대역 비율 (spectrum/bass_detail 전용)
        self.audio_zone_line = QFrame()
        self.audio_zone_line.setFrameShape(QFrame.HLine)
        self.audio_zone_line.setFrameShadow(QFrame.Sunken)
        pl.addWidget(self.audio_zone_line)
        self.audio_zone_label = QLabel("대역 비율 (주파수 분배)")
        self.audio_zone_label.setStyleSheet("font-weight:bold;")
        pl.addWidget(self.audio_zone_label)
        self.audio_zone_balance = ZoneBalanceWidget(33, 33, 34)
        pl.addWidget(self.audio_zone_balance)

        layout.addWidget(param_group)

        # spectrum 전용 위젯 목록 (모드에 따라 표시/숨김)
        self._audio_spectrum_only = [
            self.audio_row_mid_sens, self.audio_row_high_sens,
            self.audio_zone_line, self.audio_zone_label,
            self.audio_zone_balance,
        ]

        # 초기 모드 UI 적용
        self._update_audio_mode_ui("pulse")
        self._load_audio_mode_params("pulse")

    @staticmethod
    def _make_progress_bar(grid, row, name, color):
        """에너지 레벨 프로그레스 바 생성."""
        grid.addWidget(QLabel(name), row, 0)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(14)
        bar.setStyleSheet(
            f"QProgressBar{{background:#2b2b2b;border-radius:3px}}"
            f"QProgressBar::chunk{{background:{color};border-radius:3px}}"
        )
        grid.addWidget(bar, row, 1)
        return bar

    def _add_audio_slider(self, parent_layout, label_text, min_v, max_v,
                          default, suffix=""):
        """오디오 파라미터 슬라이더 + 라벨 한 줄 추가."""
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        s = NoScrollSlider(Qt.Horizontal)
        s.setRange(min_v, max_v)
        s.setValue(default)
        row.addWidget(s)
        lbl = QLabel(
            f"{default}{suffix}" if suffix == "%" else f"{default / 100:.2f}"
        )
        lbl.setMinimumWidth(40)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(lbl)
        parent_layout.addLayout(row)
        return s, lbl

    # ── 6. 공통 설정 ─────────────────────────────────────────────

    def _build_common_settings(self, parent_layout):
        cg = QGroupBox("공통 설정")
        cl = QVBoxLayout(cg)
        cl.setSpacing(3)
        cl.setContentsMargins(6, 16, 6, 4)

        # 화면 방향
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

        # Target FPS
        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("Target FPS:"))
        self.spin_target_fps = QSpinBox()
        self.spin_target_fps.setRange(10, 60)
        self.spin_target_fps.setValue(
            self.config.get("mirror", {}).get("target_fps", 60)
        )
        fps_row.addWidget(self.spin_target_fps)
        fps_row.addStretch()
        cl.addLayout(fps_row)

        # 오디오 디바이스
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
        """시작 버튼 → 자동 적용 후 엔진 시작 요청."""
        # 시작 전 현재 UI 값을 config에 반영 + 저장
        self._apply_all_settings()
        self.config_applied.emit()
        self.request_engine_start.emit(self._current_mode)

    def _on_pause_clicked(self):
        """일시정지/재개 토글."""
        self.request_engine_pause.emit()

    def _on_stop_clicked(self):
        """중지 버튼 → 엔진 중지 요청."""
        self.request_engine_stop.emit()

    def _on_mode_changed(self, idx):
        """모드 선택 변경 → 패널 전환 + 실행 중이면 모드 전환 요청."""
        self._current_mode = _INDEX_MODE.get(idx, MODE_MIRROR)
        self.mode_stack.setCurrentIndex(idx)

        # 실행 중이면 MainWindow에 안전한 모드 전환 요청
        if self._is_running:
            self._apply_all_settings()
            self.config_applied.emit()
            self.request_mode_switch.emit(self._current_mode)

    def _adjust_stack_size(self, idx):
        """QStackedWidget 크기를 현재 패널에 맞춤.

        비활성 패널의 sizePolicy를 Ignored로 설정하여
        QStackedWidget이 현재 패널의 sizeHint만 반영하도록 합니다.
        """
        for i in range(self.mode_stack.count()):
            w = self.mode_stack.widget(i)
            if i == idx:
                w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            else:
                w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        self.mode_stack.adjustSize()

    def _on_preview_toggled(self, checked):
        """LED 프리뷰 접기/펼치기."""
        self.monitor_preview.setVisible(checked)
        self.btn_preview_toggle.setText(
            "👁 프리뷰 숨기기" if checked else "👁 프리뷰 보기"
        )

    # ── 미러링 패널 이벤트 (Step 8) ──────────────────────────────

    def _on_mirror_brightness_changed(self, value):
        """밝기 슬라이더 변경."""
        self.mirror_brightness_label.setText(f"{value}%")
        if self._is_running:
            self.mirror_brightness_changed.emit(value)

    def _on_mirror_smoothing_changed(self, state):
        """스무딩 체크박스 변경."""
        if self._is_running:
            self.mirror_smoothing_changed.emit(bool(state))

    def _on_mirror_smoothing_factor_changed(self, value):
        """스무딩 계수 변경."""
        if self._is_running:
            self.mirror_smoothing_factor_changed.emit(value)

    def _on_mirror_zone_count_changed(self, idx):
        """미러링 구역 수 변경."""
        n = self.mirror_combo_zone_count.currentData()
        if n is not None and self._is_running:
            self.mirror_zone_count_changed.emit(n)

    def _on_mirror_layout_param_changed(self, _=None):
        """감쇠/페널티/변별 값 변경 → 디바운스 후 시그널."""
        if self._is_running:
            self._layout_debounce.start()

    def _emit_mirror_layout_params(self):
        """디바운스 만료 — 현재 UI 값을 dict로 모아서 시그널 발생."""
        params = {
            "decay_radius": self.mirror_spin_decay.value(),
            "parallel_penalty": self.mirror_spin_penalty.value(),
        }
        if self.mirror_chk_per_side.isChecked():
            params["decay_per_side"] = {
                side: self.mirror_spin_decay_per[side].value()
                for side in self.mirror_spin_decay_per
            }
            params["penalty_per_side"] = {
                side: self.mirror_spin_penalty_per[side].value()
                for side in self.mirror_spin_penalty_per
            }
        else:
            params["decay_per_side"] = {}
            params["penalty_per_side"] = {}

        self.mirror_layout_params_changed.emit(params)

    # ── 오디오 패널 이벤트 (Step 9) ──────────────────────────────

    def _on_audio_min_brightness_changed(self, value):
        """오디오 최소 밝기 슬라이더 변경."""
        self.audio_lbl_min_brightness.setText(f"{value}%")
        if self._is_running:
            self.audio_min_brightness_changed.emit(value / 100.0)

    def _on_audio_mode_changed(self, idx):
        """오디오 서브모드 변경 → 파라미터 전환 + UI 업데이트."""
        new_key = _INDEX_AUDIO_MODE.get(idx, "pulse")
        if new_key == self._audio_mode_key:
            return
        # 현재 모드 파라미터 저장
        self._save_audio_mode_params(self._audio_mode_key)
        # 새 모드 파라미터 로드
        self._load_audio_mode_params(new_key)
        self._update_audio_mode_ui(new_key)
        self._audio_mode_key = new_key
        # 실행 중이면 엔진에 반영
        if self._is_running:
            self.audio_params_changed.emit(self._collect_audio_params())

    def _update_audio_mode_ui(self, mode_name):
        """오디오 서브모드에 따라 위젯 표시/숨김."""
        is_banded = mode_name in ("spectrum", "bass_detail")
        for w in self._audio_spectrum_only:
            w.setVisible(is_banded)
        if mode_name == "bass_detail":
            self.audio_label_sens.setText("감도 (Bass Detail)")
        elif mode_name == "spectrum":
            self.audio_label_sens.setText("감도 (대역별)")
        else:
            self.audio_label_sens.setText("감도 (Bass)")

    def _save_audio_mode_params(self, mode_name):
        """현재 슬라이더 값을 config의 해당 모드 키에 저장."""
        key = f"audio_{mode_name}"
        d = self.config.setdefault(key, {})
        d["bass_sens"] = self.audio_slider_bass_sens.value()
        d["mid_sens"] = self.audio_slider_mid_sens.value()
        d["high_sens"] = self.audio_slider_high_sens.value()
        d["brightness"] = self.audio_slider_brightness.value()
        d["attack"] = self.audio_slider_attack.value()
        d["release"] = self.audio_slider_release.value()
        zb, zm, zh = self.audio_zone_balance.get_values()
        d["zone_bass"] = zb
        d["zone_mid"] = zm
        d["zone_high"] = zh

    def _load_audio_mode_params(self, mode_name):
        """config에서 해당 모드 파라미터를 슬라이더에 로드."""
        key = f"audio_{mode_name}"
        df = _AUDIO_DEFAULTS.get(mode_name, _AUDIO_DEFAULTS["pulse"])
        d = self.config.get(key, df)

        self.audio_slider_bass_sens.setValue(d.get("bass_sens", df["bass_sens"]))
        self.audio_slider_mid_sens.setValue(d.get("mid_sens", df["mid_sens"]))
        self.audio_slider_high_sens.setValue(d.get("high_sens", df["high_sens"]))
        self.audio_slider_brightness.setValue(d.get("brightness", df["brightness"]))
        self.audio_slider_attack.setValue(d.get("attack", df["attack"]))
        self.audio_slider_release.setValue(d.get("release", df["release"]))
        self.audio_zone_balance.set_values(
            d.get("zone_bass", df["zone_bass"]),
            d.get("zone_mid", df["zone_mid"]),
            d.get("zone_high", df["zone_high"]),
        )

        # 라벨 갱신
        self.audio_lbl_bass_sens.setText(
            f"{self.audio_slider_bass_sens.value() / 100:.2f}"
        )
        self.audio_lbl_mid_sens.setText(
            f"{self.audio_slider_mid_sens.value() / 100:.2f}"
        )
        self.audio_lbl_high_sens.setText(
            f"{self.audio_slider_high_sens.value() / 100:.2f}"
        )
        self.audio_lbl_brightness.setText(
            f"{self.audio_slider_brightness.value()}%"
        )
        self.audio_lbl_attack.setText(
            f"{self.audio_slider_attack.value() / 100:.2f}"
        )
        self.audio_lbl_release.setText(
            f"{self.audio_slider_release.value() / 100:.2f}"
        )

    def _collect_audio_params(self):
        """현재 오디오 슬라이더 값을 dict로 수집 — 엔진에 전달용."""
        return {
            "audio_mode": _INDEX_AUDIO_MODE.get(
                self.audio_combo_mode.currentIndex(), "pulse"
            ),
            "brightness": self.audio_slider_brightness.value() / 100.0,
            "audio_min_brightness": self.audio_slider_min_brightness.value() / 100.0,
            "bass_sensitivity": self.audio_slider_bass_sens.value() / 100.0,
            "mid_sensitivity": self.audio_slider_mid_sens.value() / 100.0,
            "high_sensitivity": self.audio_slider_high_sens.value() / 100.0,
            "attack": self.audio_slider_attack.value() / 100.0,
            "release": self.audio_slider_release.value() / 100.0,
            "zone_weights": self.audio_zone_balance.get_values(),
            "rainbow": self._audio_is_rainbow,
            "base_color": self._audio_current_color,
        }

    def _audio_set_color(self, r, g, b):
        """오디오 단색 설정."""
        self._audio_current_color = (r, g, b)
        self._audio_is_rainbow = False
        self.audio_color_preview.setStyleSheet(
            f"background:rgb({r},{g},{b});"
            f"border:1px solid #555;border-radius:4px;"
        )
        if self._is_running:
            self.audio_params_changed.emit(self._collect_audio_params())

    def _audio_set_rainbow(self):
        """오디오 무지개 설정."""
        self._audio_is_rainbow = True
        self.audio_color_preview.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 red,stop:0.17 orange,stop:0.33 yellow,"
            "stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,"
            "stop:1 purple);border:1px solid #555;border-radius:4px;"
        )
        if self._is_running:
            self.audio_params_changed.emit(self._collect_audio_params())

    def _audio_pick_custom_color(self):
        """커스텀 색상 선택 다이얼로그."""
        r, g, b = self._audio_current_color
        c = QColorDialog.getColor(QColor(r, g, b), self, "기본 색상")
        if c.isValid():
            self._audio_set_color(c.red(), c.green(), c.blue())

    # ── 하이브리드 패널 이벤트 (Step 10) ─────────────────────────

    def _on_hybrid_zone_count_changed(self, idx):
        """화면 구역 수 변경."""
        if self._is_running:
            self.hybrid_params_changed.emit(self._collect_hybrid_params())

    def _on_hybrid_min_brightness_changed(self, value):
        """최소 밝기 슬라이더 변경."""
        self.hybrid_lbl_min_brightness.setText(f"{value}%")
        if self._is_running:
            self.hybrid_params_changed.emit(self._collect_hybrid_params())

    def _on_hybrid_audio_mode_changed(self, idx):
        """하이브리드 오디오 서브모드 변경."""
        new_key = _INDEX_AUDIO_MODE.get(idx, "pulse")
        if new_key == self._hybrid_mode_key:
            return
        self._save_hybrid_mode_params(self._hybrid_mode_key)
        self._load_hybrid_mode_params(new_key)
        self._update_hybrid_mode_ui(new_key)
        self._hybrid_mode_key = new_key
        if self._is_running:
            self.hybrid_params_changed.emit(self._collect_hybrid_params())

    def _update_hybrid_mode_ui(self, mode_name):
        """하이브리드 서브모드에 따라 위젯 표시/숨김."""
        is_banded = mode_name in ("spectrum", "bass_detail")
        for w in self._hybrid_spectrum_only:
            w.setVisible(is_banded)
        if mode_name == "bass_detail":
            self.hybrid_label_sens.setText("감도 (Bass Detail)")
        elif mode_name == "spectrum":
            self.hybrid_label_sens.setText("감도 (대역별)")
        else:
            self.hybrid_label_sens.setText("감도 (Bass)")

    def _save_hybrid_mode_params(self, mode_name):
        """현재 하이브리드 슬라이더 값을 config에 저장."""
        key = f"audio_{mode_name}"
        d = self.config.setdefault(key, {})
        d["bass_sens"] = self.hybrid_slider_bass_sens.value()
        d["mid_sens"] = self.hybrid_slider_mid_sens.value()
        d["high_sens"] = self.hybrid_slider_high_sens.value()
        d["brightness"] = self.hybrid_slider_brightness.value()
        d["attack"] = self.hybrid_slider_attack.value()
        d["release"] = self.hybrid_slider_release.value()
        zb, zm, zh = self.hybrid_zone_balance.get_values()
        d["zone_bass"] = zb
        d["zone_mid"] = zm
        d["zone_high"] = zh

    def _load_hybrid_mode_params(self, mode_name):
        """config에서 하이브리드 파라미터를 슬라이더에 로드."""
        key = f"audio_{mode_name}"
        df = _AUDIO_DEFAULTS.get(mode_name, _AUDIO_DEFAULTS["pulse"])
        d = self.config.get(key, df)

        self.hybrid_slider_bass_sens.setValue(d.get("bass_sens", df["bass_sens"]))
        self.hybrid_slider_mid_sens.setValue(d.get("mid_sens", df["mid_sens"]))
        self.hybrid_slider_high_sens.setValue(d.get("high_sens", df["high_sens"]))
        self.hybrid_slider_brightness.setValue(d.get("brightness", df["brightness"]))
        self.hybrid_slider_attack.setValue(d.get("attack", df["attack"]))
        self.hybrid_slider_release.setValue(d.get("release", df["release"]))
        self.hybrid_zone_balance.set_values(
            d.get("zone_bass", df["zone_bass"]),
            d.get("zone_mid", df["zone_mid"]),
            d.get("zone_high", df["zone_high"]),
        )

        self.hybrid_lbl_bass_sens.setText(
            f"{self.hybrid_slider_bass_sens.value() / 100:.2f}"
        )
        self.hybrid_lbl_mid_sens.setText(
            f"{self.hybrid_slider_mid_sens.value() / 100:.2f}"
        )
        self.hybrid_lbl_high_sens.setText(
            f"{self.hybrid_slider_high_sens.value() / 100:.2f}"
        )
        self.hybrid_lbl_brightness.setText(
            f"{self.hybrid_slider_brightness.value()}%"
        )
        self.hybrid_lbl_attack.setText(
            f"{self.hybrid_slider_attack.value() / 100:.2f}"
        )
        self.hybrid_lbl_release.setText(
            f"{self.hybrid_slider_release.value() / 100:.2f}"
        )

    def _collect_hybrid_params(self):
        """현재 하이브리드 UI 값을 dict로 수집 — 엔진 전달용."""
        return {
            "audio_mode": _INDEX_AUDIO_MODE.get(
                self.hybrid_combo_mode.currentIndex(), "pulse"
            ),
            "color_source": COLOR_SOURCE_SCREEN,
            "n_zones": self.hybrid_combo_zone_count.currentData() or 4,
            "min_brightness": self.hybrid_slider_min_brightness.value() / 100.0,
            "brightness": self.hybrid_slider_brightness.value() / 100.0,
            "bass_sensitivity": self.hybrid_slider_bass_sens.value() / 100.0,
            "mid_sensitivity": self.hybrid_slider_mid_sens.value() / 100.0,
            "high_sensitivity": self.hybrid_slider_high_sens.value() / 100.0,
            "attack": self.hybrid_slider_attack.value() / 100.0,
            "release": self.hybrid_slider_release.value() / 100.0,
            "zone_weights": self.hybrid_zone_balance.get_values(),
        }

    def get_hybrid_zone_count(self):
        """현재 하이브리드 구역 수."""
        return self.hybrid_combo_zone_count.currentData() or 4

    # ── 적용/되돌리기 ────────────────────────────────────────────

    def _apply_all_settings(self):
        """모든 패널의 UI 값을 config dict에 반영."""
        self._apply_common_settings()
        self._apply_mirror_settings()
        self._apply_audio_settings()
        self._apply_hybrid_settings()
        self._applied_snapshot = copy.deepcopy(self.config)

    def _on_apply_clicked(self):
        """적용 버튼 → config 저장 + 실행 중이면 엔진에 파라미터 push."""
        self._apply_all_settings()
        self.config_applied.emit()

        # 실행 중이면 현재 모드에 맞는 파라미터를 엔진에 전달
        if self._is_running:
            if self._current_mode == MODE_AUDIO:
                self.audio_params_changed.emit(self._collect_audio_params())
            elif self._current_mode == MODE_HYBRID:
                self.hybrid_params_changed.emit(self._collect_hybrid_params())
            # mirror는 개별 시그널로 이미 실시간 반영됨

    def _on_revert_clicked(self):
        """되돌리기 → 마지막 적용 시점의 값으로 전체 UI 복원."""
        # config dict도 스냅샷으로 복원
        for key in self._applied_snapshot:
            self.config[key] = copy.deepcopy(self._applied_snapshot[key])

        self._load_common_settings(self._applied_snapshot)
        self._load_mirror_settings(self._applied_snapshot)
        self._load_audio_settings(self._applied_snapshot)
        self._load_hybrid_settings(self._applied_snapshot)

    # ══════════════════════════════════════════════════════════════
    #  공통 설정 읽기/쓰기
    # ══════════════════════════════════════════════════════════════

    def _apply_common_settings(self):
        """UI → config: 공통 설정을 config dict에 반영."""
        mirror_cfg = self.config.setdefault("mirror", {})

        # 화면 방향
        orient_map = {0: "auto", 1: "landscape", 2: "portrait"}
        mirror_cfg["orientation"] = orient_map.get(
            self.combo_orientation.currentIndex(), "auto"
        )
        mirror_cfg["portrait_rotation"] = (
            "cw" if self.combo_rotation.currentIndex() == 0 else "ccw"
        )

        # Target FPS
        mirror_cfg["target_fps"] = self.spin_target_fps.value()

    def _load_common_settings(self, cfg):
        """config → UI: 공통 설정을 UI에 로드."""
        mirror_cfg = cfg.get("mirror", {})

        orient_val = mirror_cfg.get("orientation", "auto")
        idx_map = {"auto": 0, "landscape": 1, "portrait": 2}
        self.combo_orientation.setCurrentIndex(idx_map.get(orient_val, 0))

        rot_val = mirror_cfg.get("portrait_rotation", "cw")
        self.combo_rotation.setCurrentIndex(0 if rot_val == "cw" else 1)

        self.spin_target_fps.setValue(mirror_cfg.get("target_fps", 60))

    def _apply_mirror_settings(self):
        """UI → config: 미러링 패널 값을 config dict에 반영."""
        mirror_cfg = self.config.setdefault("mirror", {})

        mirror_cfg["brightness"] = self.mirror_brightness_slider.value() / 100.0
        mirror_cfg["smoothing_factor"] = self.mirror_spin_smoothing.value()
        mirror_cfg["decay_radius"] = self.mirror_spin_decay.value()
        mirror_cfg["parallel_penalty"] = self.mirror_spin_penalty.value()

        if self.mirror_chk_per_side.isChecked():
            mirror_cfg["decay_radius_per_side"] = {
                side: self.mirror_spin_decay_per[side].value()
                for side in self.mirror_spin_decay_per
            }
            mirror_cfg["parallel_penalty_per_side"] = {
                side: self.mirror_spin_penalty_per[side].value()
                for side in self.mirror_spin_penalty_per
            }
        else:
            mirror_cfg["decay_radius_per_side"] = {}
            mirror_cfg["parallel_penalty_per_side"] = {}

    def _load_mirror_settings(self, cfg):
        """config → UI: 미러링 패널 값을 UI에 로드."""
        mirror_cfg = cfg.get("mirror", {})

        self.mirror_brightness_slider.setValue(
            int(mirror_cfg.get("brightness", 1.0) * 100)
        )
        self.mirror_spin_smoothing.setValue(
            mirror_cfg.get("smoothing_factor", 0.5)
        )
        self.mirror_spin_decay.setValue(
            mirror_cfg.get("decay_radius", 0.3)
        )
        self.mirror_spin_penalty.setValue(
            mirror_cfg.get("parallel_penalty", 5.0)
        )

        per_decay = mirror_cfg.get("decay_radius_per_side", {})
        per_penalty = mirror_cfg.get("parallel_penalty_per_side", {})
        has_per_side = bool(per_decay or per_penalty)

        self.mirror_chk_per_side.setChecked(has_per_side)
        self.mirror_per_side_widget.setVisible(has_per_side)

        for side in self.mirror_spin_decay_per:
            self.mirror_spin_decay_per[side].setValue(
                per_decay.get(side, mirror_cfg.get("decay_radius", 0.3))
            )
        for side in self.mirror_spin_penalty_per:
            self.mirror_spin_penalty_per[side].setValue(
                per_penalty.get(
                    side, mirror_cfg.get("parallel_penalty", 5.0)
                )
            )

    def _apply_audio_settings(self):
        """UI → config: 현재 오디오 서브모드 파라미터를 config에 저장."""
        self._save_audio_mode_params(self._audio_mode_key)

    def _load_audio_settings(self, cfg):
        """config → UI: 오디오 파라미터를 UI에 복원."""
        self._load_audio_mode_params(self._audio_mode_key)

    def _apply_hybrid_settings(self):
        """UI → config: 현재 하이브리드 파라미터를 config에 저장."""
        self._save_hybrid_mode_params(self._hybrid_mode_key)

    def _load_hybrid_settings(self, cfg):
        """config → UI: 하이브리드 파라미터를 UI에 복원."""
        self._load_hybrid_mode_params(self._hybrid_mode_key)

    def _refresh_audio_devices(self):
        """오디오 디바이스 목록 새로고침."""
        self.combo_audio_device.clear()
        self.combo_audio_device.addItem("자동 (기본 출력 디바이스)", None)
        if HAS_PYAUDIO:
            for idx, name, sr, ch in list_loopback_devices():
                self.combo_audio_device.addItem(f"{name} ({sr}Hz, {ch}ch)", idx)

    def get_audio_device_index(self):
        """현재 선택된 오디오 디바이스 인덱스 반환."""
        return self.combo_audio_device.currentData()

    def get_mirror_brightness(self):
        """미러링 밝기 (0.0~1.0)."""
        return self.mirror_brightness_slider.value() / 100.0

    def get_mirror_smoothing_enabled(self):
        """미러링 스무딩 활성화 여부."""
        return self.mirror_chk_smoothing.isChecked()

    def get_mirror_smoothing_factor(self):
        """미러링 스무딩 계수."""
        return self.mirror_spin_smoothing.value()

    # ══════════════════════════════════════════════════════════════
    #  외부 인터페이스 (MainWindow에서 호출)
    # ══════════════════════════════════════════════════════════════

    @property
    def current_mode(self):
        """현재 선택된 모드 문자열."""
        return self._current_mode

    def set_running_state(self, running):
        """엔진 실행/중지 시 UI 상태 전환."""
        self._is_running = running
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)

        # 모드 선택은 실행 중에도 활성화 — 전환 시 엔진 자동 재시작
        # (비활성화하지 않음)

        # 공통 설정 중 재시작 필요한 항목 비활성화
        self.combo_orientation.setEnabled(not running)
        self.combo_rotation.setEnabled(not running)
        self.spin_target_fps.setEnabled(not running)
        self.combo_audio_device.setEnabled(not running)

    def set_switching(self, switching):
        """모드 전환 중 UI 잠금/해제.

        전환 중에는 모드 버튼 + 제어 버튼 모두 비활성화하여
        중복 전환이나 중지/시작 경합을 방지합니다.
        """
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
        """FPS 갱신 (엔진 시그널에서 호출)."""
        self.fps_label.setText(f"{fps:.1f} fps")

    def update_status(self, text):
        """상태 메시지 갱신."""
        self.status_label.setText(text)

    def update_preview_colors(self, colors):
        """LED 프리뷰 색상 갱신 (엔진 시그널에서 호출)."""
        if self.monitor_preview.isVisible():
            self.monitor_preview.set_colors(colors)

    def update_energy(self, bass, mid, high):
        """오디오 에너지 레벨 갱신 (엔진 시그널에서 호출).

        오디오 패널과 하이브리드 패널 모두에 반영.
        """
        self.audio_bar_bass.setValue(int(bass * 100))
        self.audio_bar_mid.setValue(int(mid * 100))
        self.audio_bar_high.setValue(int(high * 100))
        # 하이브리드 패널 에너지 바
        self.hybrid_bar_bass.setValue(int(bass * 100))
        self.hybrid_bar_mid.setValue(int(mid * 100))
        self.hybrid_bar_high.setValue(int(high * 100))

    def update_spectrum(self, spec):
        """스펙트럼 갱신 (엔진 시그널에서 호출).

        오디오 패널과 하이브리드 패널 모두에 반영.
        """
        self.audio_spectrum_widget.set_values(spec)
        self.hybrid_spectrum_widget.set_values(spec)

    def get_audio_mode(self):
        """현재 오디오 서브모드 문자열."""
        return _INDEX_AUDIO_MODE.get(
            self.audio_combo_mode.currentIndex(), "pulse"
        )

    def update_pause_button(self, is_paused):
        """일시정지 버튼 텍스트 갱신."""
        self.btn_pause.setText("▶ 재개" if is_paused else "⏸ 일시정지")

    def collect_engine_init_params(self):
        """엔진 초기화용 전체 파라미터 dict 수집.

        MainWindow가 UnifiedEngine 생성 시 이 dict를 참조하여
        엔진의 초기 파라미터를 설정합니다.

        Returns:
            dict with keys depending on current_mode:
                mirror: brightness, smoothing_enabled, smoothing_factor
                audio: audio_mode, brightness, sensitivities, attack, release,
                       zone_weights, rainbow, base_color
                hybrid: audio params + color_source, n_zones, min_brightness
        """
        mode = self._current_mode
        params = {"mode": mode}

        if mode == MODE_MIRROR:
            params.update({
                "brightness": self.get_mirror_brightness(),
                "smoothing_enabled": self.get_mirror_smoothing_enabled(),
                "smoothing_factor": self.get_mirror_smoothing_factor(),
                "mirror_n_zones": self.mirror_combo_zone_count.currentData()
                    or N_ZONES_PER_LED,
            })
        elif mode == MODE_AUDIO:
            params.update(self._collect_audio_params())
        elif mode == MODE_HYBRID:
            params.update(self._collect_hybrid_params())

        return params

    def _update_resource_usage(self):
        """CPU/RAM 사용량 갱신."""
        try:
            cpu = self._process.cpu_percent() / psutil.cpu_count()
            ram = self._process.memory_info().rss / (1024 * 1024)
            self.cpu_label.setText(f"CPU: {cpu:.1f}%")
            self.ram_label.setText(f"RAM: {ram:.0f} MB")

            if cpu >= 20:
                color = "#c0392b"
            elif cpu >= 10:
                color = "#e67e22"
            else:
                color = "#d35400"
            self.cpu_label.setStyleSheet(
                f"font-size: 12px; color: {color}; margin-right: 6px;"
            )
        except Exception:
            pass

    def cleanup(self):
        """탭 정리 — 타이머 중지 + 현재 서브모드 파라미터 저장."""
        self._res_timer.stop()
        # 현재 오디오/하이브리드 서브모드 파라미터를 config에 반영
        self._save_audio_mode_params(self._audio_mode_key)
        self._save_hybrid_mode_params(self._hybrid_mode_key)