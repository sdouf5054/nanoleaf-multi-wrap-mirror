"""통합 컨트롤 탭 — 토글 기반 반응형 UI (Phase 4: 엔진 연결)

[설계]
디스플레이 토글 + 오디오 토글 조합으로 4가지 상태를 표현.
각 토글 on/off에 따라 관련 패널이 부드럽게 펼쳐지거나 접힘.

[Phase 4 — 엔진 연결]
- 토글 조합 → 기존 엔진 클래스 매핑 (과도기 호환)
- collect_engine_init_params → EngineParams 빌드
- 파라미터 변경 시 실행 중인 엔진에 즉시 전달
- master 밝기 → 엔진 즉시 반영
- 토글 변경 시 실행 중이면 엔진 모드 전환
- MainWindow 시그널 연결 완성

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
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QEvent, QPropertyAnimation, QEasingCurve, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush

from core.engine_params import EngineParams
from core.engine_utils import N_ZONES_PER_LED

# ── 기존 재사용 위젯 ──
from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.monitor_preview import MonitorPreview

# ── Phase 2: 디스플레이 패널 섹션 ──
from ui.panels.display_color_section import DisplayColorSection
from ui.panels.display_mirror_section import DisplayMirrorSection

# ── Phase 3: 오디오 패널 섹션 ──
from ui.panels.audio_reactive_section import AudioReactiveSection

# ── 오디오 디바이스 목록 ──
from core.audio_engine import list_loopback_devices, HAS_PYAUDIO


# ══════════════════════════════════════════════════════════════════
#  헬퍼: 스크롤 방지 이벤트 필터
# ══════════════════════════════════════════════════════════════════

class _NoScrollFilter(QObject):
    """QComboBox 등에서 마우스 휠 스크롤 방지."""
    _FILTERED = (QComboBox, QSpinBox, QDoubleSpinBox, QSlider)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, self._FILTERED):
            event.ignore()
            return True
        return False


# ══════════════════════════════════════════════════════════════════
#  CollapsiblePanel: 토글에 따라 부드럽게 펼쳐지는 컨테이너
# ══════════════════════════════════════════════════════════════════

class CollapsiblePanel(QWidget):
    """max-height 애니메이션으로 부드럽게 펼치고 접는 패널.

    사용법:
        panel = CollapsiblePanel()
        panel.set_content_layout(some_layout)
        panel.set_expanded(True)   # 펼치기
        panel.set_expanded(False)  # 접기
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded = False

        # 내부 컨테이너
        self._container = QWidget()
        self._container.setMaximumHeight(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._container)

        # 애니메이션
        self._anim = QPropertyAnimation(self._container, b"maximumHeight")
        self._anim.setDuration(250)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def set_content_layout(self, layout):
        """내부 레이아웃 설정."""
        self._container.setLayout(layout)

    @property
    def container(self):
        """내부 위젯 — 직접 레이아웃을 설정할 때 사용."""
        return self._container

    def set_expanded(self, expanded, animate=True):
        """펼침/접힘 상태 설정."""
        if self._expanded == expanded:
            return
        self._expanded = expanded

        if expanded:
            # 펼치기: 보이게 한 뒤 높이 측정
            self._container.setVisible(True)
            self._container.setMaximumHeight(0)
            self._container.adjustSize()
            target = self._container.sizeHint().height()
            if target < 10:
                target = 2000  # fallback
        else:
            target = 0
            # 접기: 애니메이션 시작 전에 즉시 숨김 → QPainter 경고 방지
            self._container.setVisible(False)

        if animate and self.isVisible():
            self._anim.stop()
            self._anim.setStartValue(self._container.maximumHeight())
            self._anim.setEndValue(target)
            if expanded:
                self._anim.finished.connect(self._unlock_height)
            self._anim.start()
        else:
            if expanded:
                self._container.setMaximumHeight(16777215)
            else:
                self._container.setMaximumHeight(0)

    def _unlock_height(self):
        """펼침 애니메이션 완료 후 max-height 제한 해제."""
        if self._expanded:
            self._container.setMaximumHeight(16777215)
        try:
            self._anim.finished.disconnect(self._unlock_height)
        except (TypeError, RuntimeError):
            pass

    @property
    def is_expanded(self):
        return self._expanded


# ══════════════════════════════════════════════════════════════════
#  ToggleSwitch: 커스텀 토글 스위치 위젯
# ══════════════════════════════════════════════════════════════════

class ToggleSwitch(QCheckBox):
    """iOS 스타일 토글 스위치 — 커스텀 paintEvent.

    QCheckBox를 상속하여 기존 시그널(stateChanged, toggled) 사용 가능.
    스타일시트 indicator 대신 직접 그려서 QPainter engine==0 경고 방지.
    """

    _TRACK_W = 38
    _TRACK_H = 20
    _KNOB_MARGIN = 2
    _SPACING = 8

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        # indicator를 숨기고 직접 그림
        self.setStyleSheet("""
            QCheckBox {
                spacing: 8px;
                font-size: 13px;
                color: #d0d0d0;
            }
            QCheckBox::indicator {
                width: 0px;
                height: 0px;
                margin: 0px;
                padding: 0px;
                border: none;
                background: transparent;
            }
        """)

    def sizeHint(self):
        base = super().sizeHint()
        # 트랙 너비 + 간격 + 텍스트 너비
        w = self._TRACK_W + self._SPACING + base.width()
        h = max(self._TRACK_H + 4, base.height())
        from PySide6.QtCore import QSize
        return QSize(w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        checked = self.isChecked()
        tw, th = self._TRACK_W, self._TRACK_H
        km = self._KNOB_MARGIN
        knob_d = th - 2 * km

        # 트랙 위치: 수직 중앙
        y = (self.height() - th) / 2

        # ── 트랙 ──
        track_rect = QRectF(0, y, tw, th)
        if checked:
            track_color = QColor("#2e86c1")
        else:
            track_color = QColor("#3a3a42")
        painter.setPen(QPen(QColor("#555") if not checked else track_color, 1))
        painter.setBrush(QBrush(track_color))
        painter.drawRoundedRect(track_rect, th / 2, th / 2)

        # ── 놉 ──
        if checked:
            knob_x = tw - km - knob_d
        else:
            knob_x = km
        knob_rect = QRectF(knob_x, y + km, knob_d, knob_d)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(knob_rect)

        # ── 텍스트 ──
        text = self.text()
        if text:
            painter.setPen(QColor("#d0d0d0"))
            font = self.font()
            painter.setFont(font)
            text_x = tw + self._SPACING
            text_rect = QRectF(text_x, 0, self.width() - text_x, self.height())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, text)

        painter.end()

    def hitButton(self, pos):
        """클릭 영역을 위젯 전체로."""
        return self.rect().contains(pos)


# ══════════════════════════════════════════════════════════════════
#  ControlTab
# ══════════════════════════════════════════════════════════════════

class ControlTab(QWidget):
    """통합 컨트롤 탭 — 토글 기반 반응형 UI.

    Signals → MainWindow (Phase 4에서 연결):
        request_engine_start(str)
        request_engine_stop()
        request_engine_pause()
        config_applied()
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

        # 토글 상태
        opts = config.get("options", {})
        self._display_on = opts.get("default_display_enabled", False)
        self._audio_on = opts.get("default_audio_enabled", False)

        # 저장/되돌리기 스냅샷
        self._applied_snapshot = copy.deepcopy(config)

        self._build_ui()
        self._update_toggle_panels(animate=False)

        # Phase 2: 레이아웃 변경 디바운스 타이머
        self._layout_debounce = QTimer(self)
        self._layout_debounce.setSingleShot(True)
        self._layout_debounce.setInterval(300)
        self._layout_debounce.timeout.connect(self._emit_layout_params)

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
        )

        scroll.setWidget(container)
        root.addWidget(scroll, 1)  # stretch=1 → 남는 공간 흡수

        # ── 각 섹션 빌드 ──
        self._build_status_section()
        self._build_basic_settings_section()
        self._build_preview_section()
        self._build_reactive_panels()
        self._main_layout.addStretch()

        # ── 하단 고정 버튼 (스크롤 밖) ──
        self._build_bottom_actions(root)

        # ── 스크롤 방지 필터 ──
        self._no_scroll_filter = _NoScrollFilter(self)
        for w in container.findChildren(QWidget):
            if isinstance(w, _NoScrollFilter._FILTERED):
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

        # 토글 상태 태그
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

        toggle_row.addStretch()
        lay.addLayout(toggle_row)

        # 기본값 설정 버튼
        self.btn_set_default = QPushButton("현재 토글 설정을 기본값으로 설정")
        self.btn_set_default.setFixedHeight(24)
        self.btn_set_default.setStyleSheet(
            "QPushButton{background:#444;color:#bbb;font-size:11px;"
            "border-radius:4px;padding:2px 10px;}"
            "QPushButton:hover{background:#555;color:#eee;}"
        )
        self.btn_set_default.clicked.connect(self._on_set_default_toggles)
        lay.addWidget(self.btn_set_default, alignment=Qt.AlignmentFlag.AlignLeft)

        # 구분선
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

        # 구분선
        lay.addWidget(self._make_separator())

        # 공통 설정: 화면 방향, 세로 회전, FPS
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

        # 오디오 디바이스
        audio_row = QHBoxLayout()
        audio_row.addWidget(QLabel("오디오 디바이스:"))
        self.combo_audio_device = QComboBox()
        self._refresh_audio_devices()
        # 저장된 디바이스 복원
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
        """디스플레이 side + 오디오 side 패널 빌드.

        각 side에 두 개의 CollapsiblePanel이 있고,
        토글 상태에 따라 하나만 펼쳐짐.
        """
        # ── 디스플레이 side ──
        # OFF 상태: 색상 패널
        self.panel_display_off = CollapsiblePanel()
        self._build_display_off_content(self.panel_display_off)
        self._main_layout.addWidget(self.panel_display_off)

        # ON 상태: 미러링 설정 패널
        self.panel_display_on = CollapsiblePanel()
        self._build_display_on_content(self.panel_display_on)
        self._main_layout.addWidget(self.panel_display_on)

        # ── 오디오 side ──
        # ON 상태: 오디오 반응 설정 패널
        self.panel_audio_on = CollapsiblePanel()
        self._build_audio_on_content(self.panel_audio_on)
        self._main_layout.addWidget(self.panel_audio_on)

    def _build_display_off_content(self, panel):
        """디스플레이 OFF: 색상 패널 — DisplayColorSection 사용."""
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.section_color = DisplayColorSection(self.config)
        self.section_color.params_changed.connect(self._on_display_params_changed)
        lay.addWidget(self.section_color)

        panel.set_content_layout(lay)

    def _build_display_on_content(self, panel):
        """디스플레이 ON: 미러링 설정 패널 — DisplayMirrorSection 사용."""
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.section_mirror = DisplayMirrorSection(self.config)
        self.section_mirror.params_changed.connect(self._on_display_params_changed)
        self.section_mirror.layout_params_changed.connect(self._on_layout_changed)
        self.section_mirror.zone_count_changed.connect(self._on_zone_count_changed)
        lay.addWidget(self.section_mirror)

        panel.set_content_layout(lay)

    def _build_audio_on_content(self, panel):
        """오디오 ON: 오디오 반응 설정 패널 — AudioReactiveSection 사용."""
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.section_audio = AudioReactiveSection(self.config)
        self.section_audio.set_display_enabled(self._display_on)
        self.section_audio.params_changed.connect(self._on_audio_params_changed)
        lay.addWidget(self.section_audio)

        panel.set_content_layout(lay)

    # ── ⑤ 하단 고정 버튼 ────────────────────────────────────────

    def _build_bottom_actions(self, root_layout):
        """스크롤 밖 하단에 고정되는 저장/되돌리기 버튼."""
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
        if self._is_running:
            self._sync_config_from_ui()
            self.request_mode_switch.emit(self._get_engine_mode_string())

    def _on_audio_toggled(self, checked):
        self._audio_on = checked
        self._update_toggle_panels(animate=True)
        if self._is_running:
            self._sync_config_from_ui()
            self.request_mode_switch.emit(self._get_engine_mode_string())

    def _update_toggle_panels(self, animate=True):
        """토글 상태에 따라 패널 펼침/접힘 + 태그 갱신."""
        # ── 태그 갱신 ──
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
                "background:#1a3456;color:#7ec8e3;padding:2px 8px;"
                "border-radius:8px;font-size:10px;font-weight:600;"
            )
        else:
            self.tag_audio.setText("오디오 OFF")
            self.tag_audio.setStyleSheet(
                "background:#2b2b2b;color:#6a6a74;padding:2px 8px;"
                "border-radius:8px;font-size:10px;font-weight:600;"
            )

        # ── 디스플레이 side ──
        #  OFF → 색상 패널 표시
        #  ON  → 미러링 설정 패널 표시
        self.panel_display_off.set_expanded(not self._display_on, animate=animate)
        self.panel_display_on.set_expanded(self._display_on, animate=animate)

        # ── 오디오 side ──
        #  OFF → 패널 없음 (접힘)
        #  ON  → 오디오 반응 패널 표시
        self.panel_audio_on.set_expanded(self._audio_on, animate=animate)

    def _on_set_default_toggles(self):
        """현재 토글 상태를 기본값으로 저장."""
        opts = self.config.setdefault("options", {})
        opts["default_display_enabled"] = self._display_on
        opts["default_audio_enabled"] = self._audio_on
        # 스냅샷에도 반영
        self._applied_snapshot.setdefault("options", {})["default_display_enabled"] = self._display_on
        self._applied_snapshot.setdefault("options", {})["default_audio_enabled"] = self._audio_on
        self.config_applied.emit()
        self.btn_set_default.setText("✅ 저장됨")
        QTimer.singleShot(2000, lambda: self.btn_set_default.setText(
            "현재 토글 설정을 기본값으로 설정"
        ))

    # ══════════════════════════════════════════════════════════════
    #  기타 이벤트
    # ══════════════════════════════════════════════════════════════

    def _on_start_clicked(self):
        """시작 버튼 — config 동기화 후 엔진 시작 요청."""
        self._sync_config_from_ui()
        self.request_engine_start.emit(self._get_engine_mode_string())

    def _on_master_brightness_changed(self, value):
        """master 밝기 슬라이더 변경 — config 즉시 반영 + 엔진 전달."""
        self.lbl_master_brightness.setText(f"{value}%")
        self.config.setdefault("mirror", {})["master_brightness"] = value / 100.0
        if self._is_running:
            self._push_params_to_engine()

    def _on_display_params_changed(self):
        """디스플레이 색상/미러링 파라미터 변경 → 실행 중이면 엔진에 전달."""
        if self._is_running:
            self._push_params_to_engine()

    def _on_layout_changed(self):
        """감쇠/페널티 변경 — 디바운스 후 엔진에 전달."""
        if self._is_running:
            self._layout_debounce.start()

    def _emit_layout_params(self):
        """디바운스 만료 — 레이아웃 파라미터를 엔진에 전달."""
        if self._engine_ctrl and hasattr(self, 'section_mirror'):
            params = self.section_mirror.get_layout_params()
            self._engine_ctrl.update_layout_params(**params)

    def _on_zone_count_changed(self, n):
        """구역 수 변경 → 실행 중이면 엔진 모드 재시작."""
        if self._is_running:
            self._sync_config_from_ui()
            self.request_mode_switch.emit(self._get_engine_mode_string())

    def _on_audio_params_changed(self):
        """오디오 파라미터 변경 → 실행 중이면 엔진에 전달."""
        if self._is_running:
            self._push_params_to_engine()

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
        """💾 저장 — UI→config 반영 + 스냅샷 갱신."""
        self._sync_config_from_ui()
        self._applied_snapshot = copy.deepcopy(self.config)
        self.config_applied.emit()
        self.btn_apply.setText("✅ 저장됨")
        QTimer.singleShot(2000, lambda: self.btn_apply.setText("💾 저장"))

    def _on_revert(self):
        """↩ 되돌리기 — 마지막 저장 스냅샷으로 복원."""
        for key in self._applied_snapshot:
            self.config[key] = copy.deepcopy(self._applied_snapshot[key])
        self._load_from_config()
        # 실행 중이면 복원된 파라미터를 엔진에 전달
        if self._is_running:
            self._push_params_to_engine()

    def _sync_config_from_ui(self):
        """UI 위젯 값을 config에 반영 (저장/되돌리기용)."""
        m = self.config.setdefault("mirror", {})
        m["orientation"] = {0: "auto", 1: "landscape", 2: "portrait"}.get(
            self.combo_orientation.currentIndex(), "auto"
        )
        m["portrait_rotation"] = "cw" if self.combo_rotation.currentIndex() == 0 else "ccw"
        m["target_fps"] = self.spin_target_fps.value()
        self.config.setdefault("options", {})["audio_device_index"] = (
            self.combo_audio_device.currentData()
        )
        # Phase 2: 디스플레이 패널
        self.section_color.apply_to_config()
        self.section_mirror.apply_to_config()
        # Phase 3: 오디오 패널
        self.section_audio.apply_to_config()

    def _load_from_config(self):
        """config에서 UI 위젯 복원."""
        m = self.config.get("mirror", {})
        self.combo_orientation.setCurrentIndex(
            {"auto": 0, "landscape": 1, "portrait": 2}.get(m.get("orientation", "auto"), 0)
        )
        self.combo_rotation.setCurrentIndex(
            0 if m.get("portrait_rotation", "cw") == "cw" else 1
        )
        self.spin_target_fps.setValue(m.get("target_fps", 60))
        # master 밝기는 저장/되돌리기 범위 밖이므로 복원 안 함
        # Phase 2: 디스플레이 패널
        self.section_color.load_from_config()
        self.section_mirror.load_from_config()
        # Phase 3: 오디오 패널
        self.section_audio.load_from_config()

    # ══════════════════════════════════════════════════════════════
    #  외부 인터페이스 (MainWindow, 엔진에서 호출)
    # ══════════════════════════════════════════════════════════════

    @property
    def display_enabled(self) -> bool:
        return self._display_on

    @property
    def audio_enabled(self) -> bool:
        return self._audio_on

    @property
    def saved_config(self):
        """💾 저장 버튼으로 확정된 config 스냅샷."""
        return self._applied_snapshot

    def set_engine_ctrl(self, ctrl):
        """EngineController 참조 설정 (MainWindow에서 호출)."""
        self._engine_ctrl = ctrl

    def set_running_state(self, running):
        """엔진 실행 상태에 따라 UI 활성화/비활성화."""
        self._is_running = running
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        # 실행 중엔 공통 설정 비활성화
        self.combo_orientation.setEnabled(not running)
        self.combo_rotation.setEnabled(not running)
        self.spin_target_fps.setEnabled(not running)
        self.combo_audio_device.setEnabled(not running)

    def update_fps(self, fps):
        self.fps_label.setText(f"{fps:.1f} fps")

    def update_status(self, text):
        self.status_label.setText(text)

    def update_preview_colors(self, colors):
        if self.monitor_preview.isVisible():
            self.monitor_preview.set_colors(colors)

    def update_energy(self, bass, mid, high):
        """엔진 → 에너지 레벨 바 갱신."""
        if hasattr(self, 'section_audio'):
            self.section_audio.update_energy(bass, mid, high)

    def update_spectrum(self, spec):
        """엔진 → 스펙트럼 위젯 갱신."""
        if hasattr(self, 'section_audio'):
            self.section_audio.update_spectrum(spec)

    def update_pause_button(self, is_paused):
        self.btn_pause.setText("▶ 재개" if is_paused else "⏸ 일시정지")

    def get_audio_device_index(self):
        return self.combo_audio_device.currentData()

    def collect_engine_init_params(self):
        """엔진 시작 시 초기 파라미터 수집."""
        params = {
            "display_enabled": self._display_on,
            "audio_enabled": self._audio_on,
            "master_brightness": self.slider_master_brightness.value() / 100.0,
        }
        # 디스플레이 패널 파라미터
        if self._display_on:
            params.update(self.section_mirror.collect_params())
        else:
            params.update(self.section_color.collect_params())
        # 오디오 패널 파라미터
        if self._audio_on:
            params.update(self.section_audio.collect_params())
        return params

    # ══════════════════════════════════════════════════════════════
    #  엔진 파라미터 빌드 + 전달
    # ══════════════════════════════════════════════════════════════

    def _build_engine_params(self):
        """현재 UI 상태에서 EngineParams 빌드.

        collect_engine_init_params()의 dict → EngineParams 변환.
        EngineParams 필드에 없는 키는 자동으로 무시됨.
        """
        raw = self.collect_engine_init_params()
        # EngineParams 필드에 있는 키만 필터링
        valid_fields = {f.name for f in EngineParams.__dataclass_fields__.values()}
        filtered = {k: v for k, v in raw.items() if k in valid_fields}
        return EngineParams(**filtered)

    def _push_params_to_engine(self):
        """현재 UI 상태를 EngineParams로 빌드하여 엔진에 직접 전달.

        Phase 5: 과도기 MirrorParams/AudioParams 변환 제거.
        EngineController.set_params() → BaseEngine.update_params() 경로.
        """
        if not self._engine_ctrl:
            return
        ep = self._build_engine_params()
        self._engine_ctrl.set_params(ep)

    def build_init_params_for_start(self):
        """엔진 시작 시 사용할 초기 파라미터 반환.

        Phase 5: EngineParams 직접 반환.

        Returns:
            (mode_str, EngineParams)
        """
        ep = self._build_engine_params()
        mode = self._get_engine_mode_string()
        return mode, ep

    # ══════════════════════════════════════════════════════════════
    #  헬퍼
    # ══════════════════════════════════════════════════════════════

    def _get_engine_mode_string(self):
        """Phase 7: 항상 unified — UnifiedEngine이 토글 플래그로 분기."""
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
        if hasattr(self, 'section_audio'):
            self.section_audio.cleanup()
