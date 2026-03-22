"""CompactWindow — 미니 컨트롤러 v3

일반 윈도우 (최대화 비활성). Always-on-top.
ControlTab을 Single Source of Truth로 두고, CompactBridge가 양방향 동기화.

[★ Mirror Flowing 추가]
- D=ON 전용 미러 효과 콤보: 정적/CW/CCW/Flowing
- D=OFF 색상 효과 콤보와 동일한 디자인/레이아웃
- mirror_effect_changed(str) 시그널 추가
- _update_conditional_sections: D=ON → 미러 효과 표시, D=OFF → 색상 효과 표시
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QFrame, QCheckBox, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QCursor, QPixmap

from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.toggle_switch import ToggleSwitch
from ui.widgets.compact_energy_bar import CompactEnergyBar


# ── 상수 ──
_COLOR_PRESETS = [
    ("무지개",       None),
    ("핑크/마젠타",  (255, 0, 80)),
    ("빨강",        (255, 30, 0)),
    ("주황",        (255, 120, 0)),
    ("노랑",        (255, 220, 0)),
    ("초록",        (0, 255, 80)),
    ("시안",        (0, 220, 255)),
    ("파랑",        (30, 0, 255)),
    ("보라",        (150, 0, 255)),
    ("흰색",        (255, 255, 255)),
    ("커스텀...",    "custom"),
]
_COLOR_EFFECTS = [
    ("정적",           "static"),
    ("그라데이션 CW",   "gradient_cw"),
    ("그라데이션 CCW",  "gradient_ccw"),
    ("무지개 (시간)",   "rainbow_time"),
]

# ★ 미러링 색상 효과 (D=ON 전용)
_MIRROR_EFFECTS = [
    ("정적",              "static"),
    ("그라데이션 (CW)",    "gradient_cw"),
    ("그라데이션 (CCW)",   "gradient_ccw"),
    ("Flowing",           "flowing"),
]

_AUDIO_MODES = [
    ("Pulse",       "pulse"),
    ("Spectrum",    "spectrum"),
    ("Bass Detail", "bass_detail"),
    ("Wave",        "wave"),
    ("Dynamic",     "dynamic"),
    ("Flowing",     "flowing"),
]
_PRESET_NONE_TEXT = "(프리셋 없음)"


def _set_property(widget, name, value):
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class CompactWindow(QWidget):
    """미니 컨트롤러 — 일반 윈도우 (최대화 비활성, Always-on-top)."""

    # ── 시그널 ──
    request_start = Signal()
    request_stop = Signal()
    toggle_display_changed = Signal(bool)
    toggle_audio_changed = Signal(bool)
    toggle_media_changed = Signal(bool)
    master_brightness_changed = Signal(int)
    audio_mode_changed = Signal(str)
    preset_selected = Signal(str)
    preset_set_default = Signal(str)
    color_preset_changed = Signal(str, object)
    color_effect_changed = Signal(str)
    effect_speed_changed = Signal(int)
    mirror_effect_changed = Signal(str)    # ★ D=ON 미러 효과 변경
    media_fix_changed = Signal(bool)
    media_source_swap = Signal()
    media_refresh = Signal()
    close_requested = Signal()
    expand_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowTitle("Nanoleaf Compact")

        self._display_on = False
        self._audio_on = False
        self._media_on = False
        self._is_running = False
        self._build_ui()
        self._update_conditional_sections()

    # ══════════════════════════════════════════════════════════════
    #  UI 빌드
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(5)

        self._build_status_row(root)
        self._build_sep(root)
        self._build_start_preset_row(root)
        self._build_toggle_row(root)
        self._build_brightness_row(root)
        self._build_sep(root)

        # ★ D=ON 미러 효과 (색상 섹션 위에 배치)
        self._build_mirror_effect_section(root)
        # D=OFF 색상 섹션
        self._build_static_section(root)
        # 오디오 섹션
        self._build_audio_section(root)
        self._build_media_section(root)

        root.addStretch()

        self.setMinimumWidth(380)
        self.setMaximumWidth(460)

    # ── ① 상태 행 ──

    def _build_status_row(self, parent):
        row = QHBoxLayout()
        row.setSpacing(4)

        self.lbl_status = QLabel("대기 중")
        self.lbl_status.setObjectName("statusLabel")
        self.lbl_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_status.setToolTip("더블클릭하여 메인 GUI 열기")
        self.lbl_status.mouseDoubleClickEvent = lambda e: self.expand_requested.emit()
        row.addWidget(self.lbl_status)

        lbl_expand = QLabel("+")
        lbl_expand.setObjectName("compactExpandHint")
        lbl_expand.setCursor(Qt.CursorShape.PointingHandCursor)
        lbl_expand.setToolTip("더블클릭하여 메인 GUI 열기")
        lbl_expand.mouseDoubleClickEvent = lambda e: self.expand_requested.emit()
        row.addWidget(lbl_expand)

        row.addStretch()

        self.lbl_cpu = QLabel("—%")
        self.lbl_cpu.setObjectName("compactCpu")
        row.addWidget(self.lbl_cpu)

        self.lbl_ram = QLabel("—MB")
        self.lbl_ram.setObjectName("compactRam")
        row.addWidget(self.lbl_ram)

        self.lbl_fps = QLabel("—fps")
        self.lbl_fps.setObjectName("compactFps")
        row.addWidget(self.lbl_fps)

        parent.addLayout(row)

    # ── ② 시작 + 프리셋 행 ──

    def _build_start_preset_row(self, parent):
        row = QHBoxLayout()
        row.setSpacing(4)

        self.btn_start = QPushButton("▶ 시작")
        self.btn_start.setObjectName("compactStart")
        self.btn_start.setFixedSize(68, 26)
        self.btn_start.clicked.connect(self._on_start_clicked)
        row.addWidget(self.btn_start)

        self.combo_preset = QComboBox()
        self.combo_preset.setFixedHeight(28)
        self.combo_preset.setMinimumWidth(120)
        self.combo_preset.addItem(_PRESET_NONE_TEXT, None)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_selected)
        self.combo_preset.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.combo_preset, 1)

        self.btn_preset_default = QPushButton("★")
        self.btn_preset_default.setObjectName("btnPresetDefault")
        self.btn_preset_default.setFixedSize(28, 28)
        self.btn_preset_default.setToolTip("기본 프리셋 설정/해제")
        self.btn_preset_default.clicked.connect(self._on_preset_default_clicked)
        row.addWidget(self.btn_preset_default)

        parent.addLayout(row)

    # ── ③ 토글 행 ──

    def _build_toggle_row(self, parent):
        row = QHBoxLayout()
        row.setSpacing(12)
        self.toggle_display = ToggleSwitch("디스플레이")
        self.toggle_display.toggled.connect(self._on_display_toggled)
        row.addWidget(self.toggle_display)
        self.toggle_audio = ToggleSwitch("오디오")
        self.toggle_audio.toggled.connect(self._on_audio_toggled)
        row.addWidget(self.toggle_audio)
        self.toggle_media = ToggleSwitch("미디어")
        self.toggle_media.toggled.connect(self._on_media_toggled)
        row.addWidget(self.toggle_media)
        row.addStretch()
        parent.addLayout(row)

    # ── ④ 밝기 행 ──

    def _build_brightness_row(self, parent):
        row = QHBoxLayout()
        row.setSpacing(4)
        lbl_b = QLabel("밝기")
        lbl_b.setFixedWidth(28)
        row.addWidget(lbl_b)
        self.slider_brightness = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_brightness.setRange(0, 100)
        self.slider_brightness.setValue(100)
        self.slider_brightness.valueChanged.connect(self._on_brightness_changed)
        row.addWidget(self.slider_brightness)
        self.lbl_brightness = QLabel("100%")
        self.lbl_brightness.setMinimumWidth(32)
        self.lbl_brightness.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.lbl_brightness)
        parent.addLayout(row)

    # ── ⑤ ★ D=ON 미러 효과 섹션 ────────────────────────────────

    def _build_mirror_effect_section(self, parent):
        """D=ON일 때 표시되는 미러링 색상 효과 콤보."""
        self._mirror_effect_container = QWidget()
        self._mirror_effect_container.setVisible(False)
        lay = QVBoxLayout(self._mirror_effect_container)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)

        effect_row = QHBoxLayout()
        effect_row.setSpacing(4)
        lbl_e = QLabel("효과")
        lbl_e.setFixedWidth(28)
        effect_row.addWidget(lbl_e)
        self.combo_mirror_effect = QComboBox()
        self.combo_mirror_effect.setFixedHeight(24)
        for name, key in _MIRROR_EFFECTS:
            self.combo_mirror_effect.addItem(name, key)
        self.combo_mirror_effect.currentIndexChanged.connect(
            self._on_mirror_effect_changed
        )
        effect_row.addWidget(self.combo_mirror_effect, 1)
        lay.addLayout(effect_row)

        parent.addWidget(self._mirror_effect_container)

    # ── ⑥ D=OFF 색상 섹션 ──────────────────────────────────────

    def _build_static_section(self, parent):
        self._static_container = QWidget()
        self._static_container.setVisible(False)
        lay = QVBoxLayout(self._static_container)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)

        # ★ 색상 줄
        color_row = QHBoxLayout()
        color_row.setSpacing(4)
        lbl_c = QLabel("색상")
        lbl_c.setFixedWidth(28)
        color_row.addWidget(lbl_c)
        self.combo_color = QComboBox()
        self.combo_color.setFixedHeight(24)
        for name, rgb in _COLOR_PRESETS:
            self.combo_color.addItem(name, rgb)
        self.combo_color.currentIndexChanged.connect(self._on_color_preset_changed)
        color_row.addWidget(self.combo_color, 1)
        lay.addLayout(color_row)

        # ★ 효과 줄
        effect_row = QHBoxLayout()
        effect_row.setSpacing(4)
        lbl_e = QLabel("효과")
        lbl_e.setFixedWidth(28)
        effect_row.addWidget(lbl_e)
        self.combo_effect = QComboBox()
        self.combo_effect.setFixedHeight(24)
        for name, key in _COLOR_EFFECTS:
            self.combo_effect.addItem(name, key)
        self.combo_effect.currentIndexChanged.connect(self._on_color_effect_changed)
        effect_row.addWidget(self.combo_effect, 1)
        lay.addLayout(effect_row)

        # ★ 속도 줄
        self._speed_row = QWidget()
        speed_lay = QHBoxLayout(self._speed_row)
        speed_lay.setContentsMargins(0, 0, 0, 0)
        speed_lay.setSpacing(4)
        lbl_s = QLabel("속도")
        lbl_s.setFixedWidth(28)
        speed_lay.addWidget(lbl_s)
        self.slider_effect_speed = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_effect_speed.setRange(0, 100)
        self.slider_effect_speed.setValue(50)
        self.slider_effect_speed.valueChanged.connect(self._on_effect_speed_changed)
        speed_lay.addWidget(self.slider_effect_speed)
        self.lbl_effect_speed = QLabel("50%")
        self.lbl_effect_speed.setMinimumWidth(32)
        self.lbl_effect_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        speed_lay.addWidget(self.lbl_effect_speed)
        lay.addWidget(self._speed_row)
        self._speed_row.setVisible(False)

        parent.addWidget(self._static_container)

    # ── ⑦ 오디오 섹션 ──

    def _build_audio_section(self, parent):
        self._audio_container = QWidget()
        self._audio_container.setVisible(False)
        lay = QVBoxLayout(self._audio_container)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        lbl_m = QLabel("모드")
        lbl_m.setFixedWidth(28)
        mode_row.addWidget(lbl_m)
        self.combo_audio_mode = QComboBox()
        self.combo_audio_mode.setFixedHeight(24)
        for display, key in _AUDIO_MODES:
            self.combo_audio_mode.addItem(display, key)
        self.combo_audio_mode.currentIndexChanged.connect(self._on_audio_mode_changed)
        mode_row.addWidget(self.combo_audio_mode, 1)
        lay.addLayout(mode_row)

        self.energy_bar = CompactEnergyBar()
        lay.addWidget(self.energy_bar)
        parent.addWidget(self._audio_container)

    # ── ⑧ 미디어 섹션 ──

    def _build_media_section(self, parent):
        self._media_container = QWidget()
        self._media_container.setVisible(False)
        self._media_container.setMinimumHeight(56)
        lay = QHBoxLayout(self._media_container)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(8)

        self.lbl_media_thumb = QLabel("♪")
        self.lbl_media_thumb.setObjectName("lblMediaThumb")
        self.lbl_media_thumb.setFixedSize(44, 44)
        self.lbl_media_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_media_thumb.setScaledContents(False)
        lay.addWidget(self.lbl_media_thumb)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        source_row = QHBoxLayout()
        source_row.setSpacing(4)

        self.btn_media_source = QPushButton("미디어 연동 활성")
        self.btn_media_source.setObjectName("compactMediaSourceBtn")
        self.btn_media_source.setFlat(True)
        self.btn_media_source.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_media_source.setToolTip(
            "자동 판별 결과가 틀렸을 때, 이번 곡에 한해 소스를 반대로 뒤집습니다.\n"
            "다음 곡이 재생되면 다시 자동 판별이 시작됩니다."
        )
        self.btn_media_source.clicked.connect(self._on_source_btn_clicked)
        _set_property(self.btn_media_source, "sourceState", "active")
        source_row.addWidget(self.btn_media_source)
        source_row.addStretch()

        self.chk_media_fix = QCheckBox("fix")
        self.chk_media_fix.setToolTip("체크: 현재 소스를 고정\n해제: 자동 판별")
        self.chk_media_fix.toggled.connect(self._on_media_fix_toggled)
        source_row.addWidget(self.chk_media_fix)

        self.btn_media_refresh = QPushButton("↻")
        self.btn_media_refresh.setObjectName("btnRefreshThumb")
        self.btn_media_refresh.setFixedSize(28, 28)
        self.btn_media_refresh.setToolTip("미디어 새로고침")
        self.btn_media_refresh.clicked.connect(self._on_media_refresh_clicked)
        source_row.addWidget(self.btn_media_refresh)

        text_col.addLayout(source_row)

        self.lbl_media_song = QLabel("")
        self.lbl_media_song.setObjectName("lblMediaSong")
        self.lbl_media_song.setWordWrap(True)
        self.lbl_media_song.setMinimumHeight(16)
        text_col.addWidget(self.lbl_media_song)

        lay.addLayout(text_col, 1)
        parent.addWidget(self._media_container)

    # ══════════════════════════════════════════════════════════════
    #  조건부 영역 표시/숨김 + 동적 크기
    # ══════════════════════════════════════════════════════════════

    def _update_conditional_sections(self):
        self._audio_container.setVisible(self._audio_on)
        self._media_container.setVisible(self._media_on and self._display_on)
        # ★ D=ON → 미러 효과 표시, D=OFF → 색상 효과 표시 (상호 배타)
        self._mirror_effect_container.setVisible(self._display_on)
        self._static_container.setVisible(not self._display_on)
        self.toggle_media.setEnabled(self._display_on)
        self._update_flowing_availability()
        QTimer.singleShot(0, self._fit_height)

    def _fit_height(self):
        hint = self.sizeHint()
        target_h = hint.height()
        if abs(self.height() - target_h) > 2:
            self.resize(self.width(), target_h)

    def _update_flowing_availability(self):
        for i in range(self.combo_audio_mode.count()):
            if self.combo_audio_mode.itemData(i) == "flowing":
                model = self.combo_audio_mode.model()
                item = model.item(i)
                if item:
                    if self._display_on:
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                break

    # ══════════════════════════════════════════════════════════════
    #  이벤트 핸들러 → 시그널
    # ══════════════════════════════════════════════════════════════

    def _on_start_clicked(self):
        (self.request_stop if self._is_running else self.request_start).emit()

    def _on_preset_selected(self, index):
        name = self.combo_preset.itemData(index)
        if name is not None:
            self.preset_selected.emit(name)

    def _on_preset_default_clicked(self):
        name = self.combo_preset.currentData()
        if name is not None:
            self.preset_set_default.emit(name)

    def _on_display_toggled(self, checked):
        self._display_on = checked
        self._update_conditional_sections()
        self.toggle_display_changed.emit(checked)

    def _on_audio_toggled(self, checked):
        self._audio_on = checked
        self._update_conditional_sections()
        self.toggle_audio_changed.emit(checked)

    def _on_media_toggled(self, checked):
        self._media_on = checked
        self._update_conditional_sections()
        self.toggle_media_changed.emit(checked)

    def _on_brightness_changed(self, value):
        self.lbl_brightness.setText(f"{value}%")
        self.master_brightness_changed.emit(value)

    def _on_audio_mode_changed(self, index):
        key = self.combo_audio_mode.itemData(index)
        if key:
            self.audio_mode_changed.emit(key)

    def _on_color_preset_changed(self, index):
        data = self.combo_color.itemData(index)
        name = self.combo_color.itemText(index)
        if data == "custom":
            self.color_preset_changed.emit("custom", None)
        else:
            self.color_preset_changed.emit(name, data)

    def _on_color_effect_changed(self, index):
        key = self.combo_effect.itemData(index)
        if key:
            self._speed_row.setVisible(key != "static")
            QTimer.singleShot(0, self._fit_height)
            self.color_effect_changed.emit(key)

    def _on_effect_speed_changed(self, value):
        self.lbl_effect_speed.setText(f"{value}%")
        self.effect_speed_changed.emit(value)

    # ★ D=ON 미러 효과 변경
    def _on_mirror_effect_changed(self, index):
        key = self.combo_mirror_effect.itemData(index)
        if key:
            self.mirror_effect_changed.emit(key)

    def _on_media_fix_toggled(self, checked):
        self.media_fix_changed.emit(checked)

    def _on_source_btn_clicked(self):
        self.media_source_swap.emit()

    def _on_media_refresh_clicked(self):
        self.media_refresh.emit()

    # ══════════════════════════════════════════════════════════════
    #  동기화 API
    # ══════════════════════════════════════════════════════════════

    def sync_state(self, display_on, audio_on, media_on, is_running,
                   brightness, audio_mode=None, preset_name=None):
        self._display_on = display_on
        self._audio_on = audio_on
        self._media_on = media_on
        self._is_running = is_running
        for toggle, val in [(self.toggle_display, display_on),
                            (self.toggle_audio, audio_on),
                            (self.toggle_media, media_on)]:
            toggle.blockSignals(True)
            toggle.setChecked(val)
            toggle.blockSignals(False)
        self.slider_brightness.blockSignals(True)
        self.slider_brightness.setValue(brightness)
        self.slider_brightness.blockSignals(False)
        self.lbl_brightness.setText(f"{brightness}%")
        self._update_start_button()
        if audio_mode:
            self.combo_audio_mode.blockSignals(True)
            for i in range(self.combo_audio_mode.count()):
                if self.combo_audio_mode.itemData(i) == audio_mode:
                    self.combo_audio_mode.setCurrentIndex(i)
                    break
            self.combo_audio_mode.blockSignals(False)
        self._update_conditional_sections()

    def sync_running_state(self, is_running):
        self._is_running = is_running
        self._update_start_button()

    def sync_status(self, text):
        self.lbl_status.setText(text)

    def sync_fps(self, fps):
        self.lbl_fps.setText(f"{fps:.0f}fps")

    def sync_resource(self, cpu, ram):
        self.lbl_cpu.setText(f"{cpu:.1f}%")
        level = "danger" if cpu >= 20 else "warning" if cpu >= 10 else "normal"
        _set_property(self.lbl_cpu, "level", level)
        self.lbl_ram.setText(f"{ram:.0f}MB")

    def sync_energy(self, bass, mid, high):
        if self._audio_container.isVisible():
            self.energy_bar.set_values(bass, mid, high)

    def sync_media_info(self, source_text, source_state, song_text, thumb_pixmap=None):
        self.btn_media_source.setText(source_text)
        _set_property(self.btn_media_source, "sourceState", source_state)
        self.lbl_media_song.setText(song_text)
        if thumb_pixmap is not None:
            self.lbl_media_thumb.setPixmap(thumb_pixmap)
        else:
            self.lbl_media_thumb.clear()
            self.lbl_media_thumb.setText("♪")

    def sync_preset_list(self, names, current_name=None, default_name=None):
        self.combo_preset.blockSignals(True)
        self.combo_preset.clear()
        self.combo_preset.addItem(_PRESET_NONE_TEXT, None)
        for name in names:
            display = f"★ {name}" if name == default_name else name
            self.combo_preset.addItem(display, name)
        if current_name:
            for i in range(self.combo_preset.count()):
                if self.combo_preset.itemData(i) == current_name:
                    self.combo_preset.setCurrentIndex(i)
                    break
        self.combo_preset.blockSignals(False)
        is_default = (current_name == default_name) if current_name else False
        self.btn_preset_default.setText("★" if is_default else "★")
        _set_property(self.btn_preset_default, "isDefault", "true" if is_default else "false")

    def sync_color_state(self, rainbow, base_color, effect, speed):
        """D=OFF 색상 상태 동기화."""
        self.combo_color.blockSignals(True)
        if rainbow:
            self.combo_color.setCurrentIndex(0)
        else:
            found = False
            for i, (name, rgb) in enumerate(_COLOR_PRESETS):
                if rgb is not None and rgb != "custom" and tuple(base_color) == tuple(rgb):
                    self.combo_color.setCurrentIndex(i)
                    found = True
                    break
            if not found:
                self.combo_color.setCurrentIndex(len(_COLOR_PRESETS) - 1)
        self.combo_color.blockSignals(False)
        self.combo_effect.blockSignals(True)
        for i, (name, key) in enumerate(_COLOR_EFFECTS):
            if key == effect:
                self.combo_effect.setCurrentIndex(i)
                break
        self.combo_effect.blockSignals(False)
        self.slider_effect_speed.blockSignals(True)
        self.slider_effect_speed.setValue(speed)
        self.slider_effect_speed.blockSignals(False)
        self.lbl_effect_speed.setText(f"{speed}%")
        self._speed_row.setVisible(effect != "static")

    def sync_mirror_effect(self, effect_key):
        """★ D=ON 미러 효과 상태 동기화."""
        self.combo_mirror_effect.blockSignals(True)
        for i in range(self.combo_mirror_effect.count()):
            if self.combo_mirror_effect.itemData(i) == effect_key:
                self.combo_mirror_effect.setCurrentIndex(i)
                break
        self.combo_mirror_effect.blockSignals(False)

    # ══════════════════════════════════════════════════════════════
    #  헬퍼
    # ══════════════════════════════════════════════════════════════

    def _update_start_button(self):
        self.btn_start.setText("■ 중지" if self._is_running else "▶ 시작")

    @staticmethod
    def _build_sep(parent):
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setFixedHeight(1)
        parent.addWidget(sep)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.close_requested.emit()