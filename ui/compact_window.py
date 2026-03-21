"""CompactWindow — 미니 컨트롤러 v3

일반 윈도우 (최대화 비활성). Always-on-top.
ControlTab을 Single Source of Truth로 두고, CompactBridge가 양방향 동기화.

[v3 변경]
- 일반 윈도우로 전환 (프레임리스/Tool 제거) → 닫기/최소화 OS 제공, 태스크바 표시
- 최대화 비활성 (WindowMaximizeButtonHint 제거)
- 가로폭 롤백: 340~420
- CPU/RAM: objectName = cpuLabel/ramLabel (기존 ControlTab QSS 셀렉터 재사용)
- 미디어 소스 문구/색상: 기존 DisplayMirrorSection의 palette 기반 그대로 사용
- 인라인 setStyleSheet 최소화 — objectName + 기존 QSS 재사용
- 동적 세로 크기: sizePolicy Fixed→Preferred, _update_conditional_sections에서 resize hint
- 색상 섹션을 오디오 섹션 위로 배치 (D=OFF 시 색상이 먼저)
- 새로고침 버튼: clicked → media_refresh 시그널 연결 확인
- 소스 전환 버튼: fix=ON일 때만 동작 확인
- Unicode 문자 그대로 사용 (escape 아님)
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
    media_fix_changed = Signal(bool)
    media_source_swap = Signal()
    media_refresh = Signal()
    close_requested = Signal()
    expand_requested = Signal()      # ★ 상태 라벨 더블클릭 → 메인 GUI 열기

    def __init__(self, parent=None):
        super().__init__(parent)
        # ★ 일반 윈도우 + Always-on-top + 최대화 비활성
        # ★ 최대화 비활성: CustomizeWindowHint + 필요한 것만 명시
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

        # ★ 색상 섹션을 오디오 위에 배치
        self._build_static_section(root)
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

        # ★ 작은 확장 힌트
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
        self.btn_start.setObjectName("compactStart")  # theme.qss에서 btnStart 기반 + 작은 폰트
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
        self.btn_preset_default.setObjectName("btnPresetDefault")  # 기존과 동일
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

    # ── ⑤ 정적 LED 색상 섹션 (D=OFF 시 항상, 오디오 위에) ──

    def _build_static_section(self, parent):
        self._static_container = QWidget()
        self._static_container.setVisible(False)
        lay = QVBoxLayout(self._static_container)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)

        # ★ 색상 줄 — 모드 줄과 동일 구조 (라벨 28px + 콤보 stretch)
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

        # ★ 효과 줄 — 동일 구조
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

        # ★ 속도 줄 — 동일 구조
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

    # ── ⑥ 오디오 섹션 ──

    def _build_audio_section(self, parent):
        self._audio_container = QWidget()
        self._audio_container.setVisible(False)
        lay = QVBoxLayout(self._audio_container)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)

        # ★ 모드 줄 — 라벨 폭을 색상/효과와 통일
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

        # ★ 에너지 바 — 여백 없이 전체 폭 사용
        self.energy_bar = CompactEnergyBar()
        lay.addWidget(self.energy_bar)
        parent.addWidget(self._audio_container)

    # ── ⑦ 미디어 섹션 ──

    def _build_media_section(self, parent):
        self._media_container = QWidget()
        self._media_container.setVisible(False)
        self._media_container.setMinimumHeight(56)
        lay = QHBoxLayout(self._media_container)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(8)

        self.lbl_media_thumb = QLabel("♪")
        self.lbl_media_thumb.setObjectName("lblMediaThumb")  # 기존 DisplayMirrorSection과 동일
        self.lbl_media_thumb.setFixedSize(44, 44)
        self.lbl_media_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_media_thumb.setScaledContents(False)
        lay.addWidget(self.lbl_media_thumb)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        source_row = QHBoxLayout()
        source_row.setSpacing(4)

        # ★ 소스 상태 — QPushButton(flat), 클릭=전환 (큰 GUI의 ⇄ 전환 버튼과 동일)
        self.btn_media_source = QPushButton("미디어 연동 활성")
        self.btn_media_source.setObjectName("compactMediaSourceBtn")  # theme.qss
        self.btn_media_source.setFlat(True)
        self.btn_media_source.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_media_source.setToolTip(
            "자동 판별 결과가 틀렸을 때, 이번 곡에 한해 소스를 반대로 뒤집습니다.\n"
            "다음 곡이 재생되면 다시 자동 판별이 시작됩니다."
        )
        self.btn_media_source.clicked.connect(self._on_source_btn_clicked)
        source_row.addWidget(self.btn_media_source)
        source_row.addStretch()

        self.chk_media_fix = QCheckBox("fix")
        self.chk_media_fix.setToolTip("체크: 현재 소스를 고정\n해제: 자동 판별")
        self.chk_media_fix.toggled.connect(self._on_media_fix_toggled)
        source_row.addWidget(self.chk_media_fix)

        # ★ 새로고침 버튼 — 기존 btnRefreshThumb objectName 재사용
        self.btn_media_refresh = QPushButton("↻")
        self.btn_media_refresh.setObjectName("btnRefreshThumb")
        self.btn_media_refresh.setFixedSize(28, 28)
        self.btn_media_refresh.setToolTip("앨범아트 새로고침")
        self.btn_media_refresh.clicked.connect(self._on_media_refresh_clicked)
        source_row.addWidget(self.btn_media_refresh)

        text_col.addLayout(source_row)

        self.lbl_media_song = QLabel("")
        self.lbl_media_song.setObjectName("lblMediaSong")  # 기존과 동일
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
        self._static_container.setVisible(not self._display_on)
        self.toggle_media.setEnabled(self._display_on)
        self._update_flowing_availability()
        # ★ 동적 세로 크기 — 콘텐츠에 맞게 축소/확대
        QTimer.singleShot(0, self._fit_height)

    def _fit_height(self):
        """콘텐츠에 맞게 윈도우 높이를 조절 — geometry 경고 방지."""
        hint = self.sizeHint()
        target_h = hint.height()
        # ★ 현재와 동일하면 resize 호출 안 함 (불필요한 setGeometry 방지)
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

    def _on_media_fix_toggled(self, checked):
        self.media_fix_changed.emit(checked)

    def _on_source_btn_clicked(self):
        """소스 버튼 클릭 → fix 상태에 따라 소스 전환.
        fix=ON: media↔mirror 전환
        fix=OFF: fix를 ON으로 바꾸고 현재 반대 소스로 고정
        """
        self.media_source_swap.emit()

    def _on_media_refresh_clicked(self):
        """새로고침 버튼 클릭."""
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
        """★ 컴팩트 짧은 포맷 — "0.4%" / "112 MB" + QSS property 색상."""
        self.lbl_cpu.setText(f"{cpu:.1f}%")
        level = "danger" if cpu >= 20 else "warning" if cpu >= 10 else "normal"
        _set_property(self.lbl_cpu, "level", level)
        self.lbl_ram.setText(f"{ram:.0f}MB")

    def sync_energy(self, bass, mid, high):
        if self._audio_container.isVisible():
            self.energy_bar.set_values(bass, mid, high)

    def sync_media_info(self, source_text, source_color, song_text, thumb_pixmap=None):
        """★ 미디어 카드 갱신 — source_color만 동적, 나머지는 theme.qss."""
        self.btn_media_source.setText(source_text)
        # ★ color만 동적 오버라이드 (나머지 속성은 theme.qss #compactMediaSourceBtn)
        self.btn_media_source.setStyleSheet(f"color: {source_color};")
        self.lbl_media_song.setText(song_text)
        if thumb_pixmap is not None:
            self.lbl_media_thumb.setPixmap(thumb_pixmap)
        else:
            # ★ 미디어 없으면 썸네일 비우기
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
        """닫기 → 숨기기 (파괴하지 않음)."""
        event.ignore()
        self.hide()
        self.close_requested.emit()