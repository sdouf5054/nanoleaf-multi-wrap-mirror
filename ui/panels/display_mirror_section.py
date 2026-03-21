"""디스플레이 ON 미러링 설정 패널 — 구역/추출/스무딩/고급옵션 (Phase 2)

기존 MirrorPanel에서 추출. 디스플레이 토글 ON일 때 표시.

[기존 대비 변경]
- 밝기 슬라이더 제거 (master 밝기가 대체)
- 스무딩: 체크박스+스핀 → 슬라이더 하나 (0=off)
- 고급 옵션(감쇠/페널티/변별값): 기본 접힌 상태, 클릭 시 펼침
- 색상 효과: 정적 / 그라데이션 CW/CCW — 3개 (무지개 없음, 화면 색 연동)

[Phase 7 변경]
- per-LED 모드에서도 Distinctive 추출 허용

[미디어 연동 v2 추가]
- lbl_media_source: 현재 소스 상태 표시 라벨
  → "소스: 화면 캡처" 또는 "소스: 미디어 (앨범아트)"
- set_media_active(): tab_control에서 호출하여 상태 갱신

[미디어 소스 오버라이드 v3 추가]
- combo_media_source: 자동/앨범아트 강제/미러링 강제 선택
  → 미디어 연동 ON 상태에서만 표시

[v6 추가]
- btn_refresh_thumbnail: 썸네일 새로고침 버튼
  → 미디어 연동 ON 상태에서 썸네일 옆에 표시
  → 클릭 시 refresh_thumbnail_requested 시그널 emit
  → tab_control에서 MediaFrameProvider 캐시 리셋 + 재폴링

[Hotfix] flowing 모드 활성 시 미러링 설정 비활성화

[QSS 테마] 인라인 setStyleSheet → objectName + QSS 기반으로 전환.
  - 동적 색상 (lbl_media_source, lbl_media_thumbnail 플레이스홀더)은
    palette.py 참조로 변경.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QComboBox, QFrame, QDoubleSpinBox, QCheckBox, QGridLayout,
    QPushButton,
)
from PySide6.QtCore import Qt, Signal

from ui.widgets.no_scroll_slider import NoScrollSlider
from core.engine_utils import (
    N_ZONES_PER_LED,
    COLOR_EFFECT_STATIC, COLOR_EFFECT_GRADIENT_CW,
    COLOR_EFFECT_GRADIENT_CCW,
    gradient_speed_from_slider,
)
from styles.palette import current as _pal_current

# ── 구역 수 옵션 ──
_ZONE_OPTIONS = [
    (N_ZONES_PER_LED, "LED별 개별 (기본)"),
    (1, "1구역 (화면 전체 평균)"),
    (2, "2구역 (상/하)"),
    (4, "4구역 (상하좌우)"),
    (8, "8구역 (모서리 포함)"),
    (16, "16구역"),
    (32, "32구역"),
]

# ── 미러링 색상 효과 (무지개 없음 — 화면 색 연동) ──
_MIRROR_EFFECT_ITEMS = [
    "정적",
    "그라데이션 (CW)",
    "그라데이션 (CCW)",
]
_INDEX_MIRROR_EFFECT = {
    0: COLOR_EFFECT_STATIC,
    1: COLOR_EFFECT_GRADIENT_CW,
    2: COLOR_EFFECT_GRADIENT_CCW,
}
_MIRROR_EFFECT_TO_INDEX = {v: k for k, v in _INDEX_MIRROR_EFFECT.items()}

# ── ★ 미디어 소스 오버라이드 ──
_MEDIA_SOURCE_ITEMS = [
    ("auto",   "자동 판별"),
    ("media",  "앨범아트 강제"),
    ("mirror", "미러링 강제"),
]
_MEDIA_SOURCE_KEYS = [k for k, _ in _MEDIA_SOURCE_ITEMS]

# ── 레이아웃 상수 ──
_GROUP_MARGINS = (6, 6, 6, 8)
_GROUP_SPACING = 6


class DisplayMirrorSection(QWidget):
    """디스플레이 ON 미러링 설정 패널.

    Signals:
        params_changed(): 파라미터가 변경되었을 때 emit
        layout_params_changed(): 감쇠/페널티가 변경되었을 때 emit (디바운스 필요)
        zone_count_changed(int): 구역 수 변경 시 emit
        refresh_thumbnail_requested(): ★ 썸네일 새로고침 버튼 클릭 시 emit
    """

    params_changed = Signal()
    layout_params_changed = Signal()
    zone_count_changed = Signal(int)
    refresh_thumbnail_requested = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._color_effect = COLOR_EFFECT_STATIC
        self._adv_open = False
        self._flowing_active = False
        self._media_active = False
        self._media_toggle_count = 0
        self._build_ui()
        self.load_from_config()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        grp = QGroupBox("디스플레이 미러링 설정")
        gl = QVBoxLayout(grp)
        gl.setSpacing(_GROUP_SPACING)
        gl.setContentsMargins(*_GROUP_MARGINS)

        # ── ★ 미디어 소스 카드 (미디어 ON 시에만 표시) ──
        self._media_card = QFrame()
        self._media_card.setObjectName("mediaCard")
        self._media_card.setVisible(False)
        card_lay = QHBoxLayout(self._media_card)
        card_lay.setContentsMargins(10, 8, 10, 8)
        card_lay.setSpacing(10)

        # 썸네일 (56×56)
        self.lbl_media_thumbnail = QLabel()
        self.lbl_media_thumbnail.setObjectName("lblMediaThumb")
        self.lbl_media_thumbnail.setFixedSize(56, 56)
        self.lbl_media_thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addWidget(self.lbl_media_thumbnail)

        # 텍스트 영역 (소스 상태 + 곡 정보)
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.lbl_media_source = QLabel("미디어 연동 활성")
        self.lbl_media_source.setObjectName("lblMediaSource")
        self.lbl_media_source.setStyleSheet(
            f"color:{_pal_current()['media_active']};font-size:11px;font-weight:bold;"
            "border:none;background:transparent;"
        )
        text_col.addWidget(self.lbl_media_source)

        self.lbl_media_song = QLabel("")
        self.lbl_media_song.setObjectName("lblMediaSong")
        self.lbl_media_song.setWordWrap(True)
        text_col.addWidget(self.lbl_media_song)

        card_lay.addLayout(text_col, 1)

        # 새로고침 버튼
        self.btn_refresh_thumbnail = QPushButton("↻")
        self.btn_refresh_thumbnail.setObjectName("btnRefreshThumb")
        self.btn_refresh_thumbnail.setFixedSize(32, 32)
        self.btn_refresh_thumbnail.setToolTip(
            "앨범아트를 수동으로 다시 가져옵니다"
        )
        self.btn_refresh_thumbnail.clicked.connect(
            self.refresh_thumbnail_requested.emit
        )
        card_lay.addWidget(self.btn_refresh_thumbnail)

        gl.addWidget(self._media_card)

        # ── 미디어 OFF 시 보이는 소스 라벨 (카드 대신) ──
        self._lbl_source_off = QLabel("소스: 화면 캡처")
        self._lbl_source_off.setObjectName("lblSourceOff")
        gl.addWidget(self._lbl_source_off)

        # ── ★ 미디어 소스 오버라이드 콤보 (미디어 ON 시에만 표시) ──
        self._media_source_row = QWidget()
        msr = QHBoxLayout(self._media_source_row)
        msr.setContentsMargins(0, 4, 0, 4)
        msr.addWidget(QLabel("소스 선택:"))
        self.combo_media_source = QComboBox()
        for key, label in _MEDIA_SOURCE_ITEMS:
            self.combo_media_source.addItem(label, key)
        self.combo_media_source.currentIndexChanged.connect(self._on_param_changed)
        msr.addWidget(self.combo_media_source)

        # ── ★ 판별 결과 수동 반전 버튼 (자동 모드일 때만 활성) ──
        self.btn_toggle_source = QPushButton("⇄ 전환")
        self.btn_toggle_source.setObjectName("btnToggleSource")
        self.btn_toggle_source.setToolTip(
            "자동 판별 결과가 틀렸을 때, 이번 곡에 한해 소스를 반대로 뒤집습니다.\n"
            "다음 곡이 재생되면 다시 자동 판별이 시작됩니다."
        )
        self.btn_toggle_source.setEnabled(False)
        self.btn_toggle_source.clicked.connect(self._on_toggle_source_clicked)
        msr.addWidget(self.btn_toggle_source)

        self.lbl_media_source_hint = QLabel(
            "자동: 영상→미러링, 음원→앨범아트"
        )
        self.lbl_media_source_hint.setProperty("role", "hint")
        msr.addWidget(self.lbl_media_source_hint)
        msr.addStretch()
        gl.addSpacing(4)
        gl.addWidget(self._media_source_row)
        self._media_source_row.setVisible(False)

        # ── 색상 효과 ──
        effect_row = QHBoxLayout()
        effect_row.addWidget(QLabel("색상 효과:"))
        self.combo_color_effect = QComboBox()
        self.combo_color_effect.addItems(_MIRROR_EFFECT_ITEMS)
        self.combo_color_effect.currentIndexChanged.connect(self._on_color_effect_changed)
        effect_row.addWidget(self.combo_color_effect)
        effect_row.addStretch()
        gl.addLayout(effect_row)

        # 효과 슬라이더들
        self._row_speed = QWidget()
        rs = QHBoxLayout(self._row_speed)
        rs.setContentsMargins(0, 0, 0, 0)
        rs.addWidget(QLabel("효과 속도:"))
        self.slider_gradient_speed = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_speed.setRange(0, 100)
        self.slider_gradient_speed.setValue(50)
        self.slider_gradient_speed.valueChanged.connect(self._on_param_changed)
        rs.addWidget(self.slider_gradient_speed)
        self.lbl_gradient_speed = QLabel("50%")
        self.lbl_gradient_speed.setMinimumWidth(35)
        self.lbl_gradient_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rs.addWidget(self.lbl_gradient_speed)
        gl.addWidget(self._row_speed)
        self._row_speed.setVisible(False)

        self._row_hue = QWidget()
        rh = QHBoxLayout(self._row_hue)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(QLabel("색조 변동:"))
        self.slider_gradient_hue = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_hue.setRange(0, 100)
        self.slider_gradient_hue.setValue(40)
        self.slider_gradient_hue.valueChanged.connect(self._on_param_changed)
        rh.addWidget(self.slider_gradient_hue)
        self.lbl_gradient_hue = QLabel("40%")
        self.lbl_gradient_hue.setMinimumWidth(35)
        self.lbl_gradient_hue.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rh.addWidget(self.lbl_gradient_hue)
        gl.addWidget(self._row_hue)
        self._row_hue.setVisible(False)

        self._row_sv = QWidget()
        rv = QHBoxLayout(self._row_sv)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("밝기 변동:"))
        self.slider_gradient_sv = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_sv.setRange(0, 100)
        self.slider_gradient_sv.setValue(50)
        self.slider_gradient_sv.valueChanged.connect(self._on_param_changed)
        rv.addWidget(self.slider_gradient_sv)
        self.lbl_gradient_sv = QLabel("50%")
        self.lbl_gradient_sv.setMinimumWidth(35)
        self.lbl_gradient_sv.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rv.addWidget(self.lbl_gradient_sv)
        gl.addWidget(self._row_sv)
        self._row_sv.setVisible(False)

        # ── 구역 수 + 추출 방식 ──
        zone_row = QHBoxLayout()
        zone_row.addWidget(QLabel("구역 수:"))
        self.combo_zone_count = QComboBox()
        for n, label in _ZONE_OPTIONS:
            self.combo_zone_count.addItem(label, n)
        self.combo_zone_count.currentIndexChanged.connect(self._on_zone_changed)
        zone_row.addWidget(self.combo_zone_count)

        zone_row.addWidget(QLabel("추출:"))
        self.combo_extract_mode = QComboBox()
        self.combo_extract_mode.addItem("평균", "average")
        self.combo_extract_mode.addItem("Distinctive", "distinctive")
        self.combo_extract_mode.currentIndexChanged.connect(self._on_param_changed)
        zone_row.addWidget(self.combo_extract_mode)
        zone_row.addStretch()
        gl.addLayout(zone_row)

        # ★ flowing 비활성 힌트 라벨
        self.lbl_flowing_hint = QLabel("Flowing 모드에서는 자체 색 추출을 사용합니다")
        self.lbl_flowing_hint.setObjectName("lblFlowingHint")
        self.lbl_flowing_hint.setVisible(False)
        gl.addWidget(self.lbl_flowing_hint)

        # ── 스무딩 (슬라이더 하나, 0=off) ──
        smooth_row = QHBoxLayout()
        smooth_row.addWidget(QLabel("스무딩:"))
        self.slider_smoothing = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_smoothing.setRange(0, 95)
        self.slider_smoothing.setValue(50)
        self.slider_smoothing.valueChanged.connect(self._on_param_changed)
        smooth_row.addWidget(self.slider_smoothing)
        self.lbl_smoothing = QLabel("0.50")
        self.lbl_smoothing.setMinimumWidth(35)
        self.lbl_smoothing.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        smooth_row.addWidget(self.lbl_smoothing)
        gl.addLayout(smooth_row)

        self._smoothing_row_widgets = [self.slider_smoothing, self.lbl_smoothing]

        # ── 구분선 ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        gl.addWidget(sep)

        # ── 고급 옵션 (접기/펼치기) ──
        self.btn_advanced = QPushButton("▶ 고급 옵션")
        self.btn_advanced.setObjectName("btnAdvanced")
        self.btn_advanced.clicked.connect(self._toggle_advanced)
        gl.addWidget(self.btn_advanced)

        # 고급 옵션 컨테이너
        self._adv_container = QWidget()
        self._adv_container.setVisible(False)
        adv_lay = QVBoxLayout(self._adv_container)
        adv_lay.setContentsMargins(0, 4, 0, 0)
        adv_lay.setSpacing(4)

        # 감쇠 반경
        decay_row = QHBoxLayout()
        decay_row.addWidget(QLabel("감쇠 반경:"))
        self.spin_decay = QDoubleSpinBox()
        self.spin_decay.setRange(0.05, 1.0)
        self.spin_decay.setSingleStep(0.05)
        self.spin_decay.valueChanged.connect(lambda _: self.layout_params_changed.emit())
        decay_row.addWidget(self.spin_decay)
        decay_row.addWidget(QLabel("타원 페널티:"))
        self.spin_penalty = QDoubleSpinBox()
        self.spin_penalty.setRange(1.0, 10.0)
        self.spin_penalty.setSingleStep(0.5)
        self.spin_penalty.valueChanged.connect(lambda _: self.layout_params_changed.emit())
        decay_row.addWidget(self.spin_penalty)
        decay_row.addStretch()
        adv_lay.addLayout(decay_row)

        # 변별 값 사용
        self.chk_per_side = QCheckBox("변별 값 사용 (면별 개별 설정)")
        self.chk_per_side.stateChanged.connect(self._on_per_side_toggled)
        adv_lay.addWidget(self.chk_per_side)

        # 면별 값 그리드
        self._per_side_widget = QWidget()
        self._per_side_widget.setVisible(False)
        per_grid = QGridLayout(self._per_side_widget)
        per_grid.setSpacing(2)
        sides = ["top", "bottom", "left", "right"]
        side_labels = {"top": "상단", "bottom": "하단", "left": "좌측", "right": "우측"}
        per_grid.addWidget(QLabel(""), 0, 0)
        per_grid.addWidget(QLabel("감쇠 반경"), 0, 1)
        per_grid.addWidget(QLabel("타원 페널티"), 0, 2)

        self.spin_decay_per = {}
        self.spin_penalty_per = {}
        for row_i, side in enumerate(sides, 1):
            per_grid.addWidget(QLabel(side_labels[side]), row_i, 0)
            sp_d = QDoubleSpinBox()
            sp_d.setRange(0.05, 1.0)
            sp_d.setSingleStep(0.05)
            sp_d.valueChanged.connect(lambda _: self.layout_params_changed.emit())
            self.spin_decay_per[side] = sp_d
            per_grid.addWidget(sp_d, row_i, 1)
            sp_p = QDoubleSpinBox()
            sp_p.setRange(1.0, 10.0)
            sp_p.setSingleStep(0.5)
            sp_p.valueChanged.connect(lambda _: self.layout_params_changed.emit())
            self.spin_penalty_per[side] = sp_p
            per_grid.addWidget(sp_p, row_i, 2)

        adv_lay.addWidget(self._per_side_widget)
        gl.addWidget(self._adv_container)

        layout.addWidget(grp)

    # ── 이벤트 ───────────────────────────────────────────────────

    def _on_color_effect_changed(self, idx):
        self._color_effect = _INDEX_MIRROR_EFFECT.get(idx, COLOR_EFFECT_STATIC)
        is_static = self._color_effect == COLOR_EFFECT_STATIC
        self._row_speed.setVisible(not is_static)
        self._row_hue.setVisible(not is_static)
        self._row_sv.setVisible(not is_static)
        self.params_changed.emit()

    def _on_zone_changed(self, _=None):
        n = self.combo_zone_count.currentData()
        if n is not None:
            self.zone_count_changed.emit(n)
        self.params_changed.emit()

    def _on_param_changed(self, _=None):
        self.lbl_gradient_speed.setText(f"{self.slider_gradient_speed.value()}%")
        self.lbl_gradient_hue.setText(f"{self.slider_gradient_hue.value()}%")
        self.lbl_gradient_sv.setText(f"{self.slider_gradient_sv.value()}%")
        self.lbl_smoothing.setText(f"{self.slider_smoothing.value() / 100:.2f}")
        if hasattr(self, "btn_toggle_source"):
            is_auto = self.combo_media_source.currentData() == "auto"
            self.btn_toggle_source.setEnabled(is_auto)
        self.params_changed.emit()

    def _on_toggle_source_clicked(self):
        self._media_toggle_count += 1
        self.params_changed.emit()

    def _on_per_side_toggled(self, state):
        self._per_side_widget.setVisible(bool(state))
        self.layout_params_changed.emit()

    def _toggle_advanced(self):
        self._adv_open = not self._adv_open
        self._adv_container.setVisible(self._adv_open)
        self.btn_advanced.setText("▼ 고급 옵션" if self._adv_open else "▶ 고급 옵션")

    # ── ★ flowing 모드 연동 ──────────────────────────────────────

    def set_flowing_active(self, active):
        self._flowing_active = active
        self.combo_zone_count.setEnabled(not active)
        self.combo_extract_mode.setEnabled(not active)
        self.slider_smoothing.setEnabled(not active)
        self.lbl_flowing_hint.setVisible(active)

    # ── ★ 미디어 연동 소스 상태 ──────────────────────────────────

    def set_media_active(self, active):
        self._media_active = active
        if active:
            self._media_card.setVisible(True)
            self._lbl_source_off.setVisible(False)
            self.lbl_media_source.setText("미디어 연동 활성")
            self.lbl_media_source.setStyleSheet(
                f"color:{_pal_current()['media_active']};font-size:11px;font-weight:bold;"
                "border:none;background:transparent;"
            )
            self.lbl_media_song.setText("미디어 정보 대기 중...")
            self.lbl_media_thumbnail.setText("♪")
            self._media_source_row.setVisible(True)
            self.btn_toggle_source.setEnabled(
                self.combo_media_source.currentData() == "auto"
            )
        else:
            self._media_card.setVisible(False)
            self._lbl_source_off.setVisible(True)
            self.lbl_media_song.setText("")
            self._media_source_row.setVisible(False)

    def update_current_source(self, decision, state):
        """★ 현재 실제 소스 판별 결과를 라벨에 실시간 표시."""
        if not self._media_active:
            return

        if decision == "media":
            if state == "phase1":
                text = "미디어 (판별 중...)"
                color = _pal_current()["media_phase1"]
            else:
                text = "미디어 사용 중"
                color = _pal_current()["media_active"]
        else:
            if state == "audio_idle":
                text = "미러링 (오디오 무음)"
                color = _pal_current()["media_idle"]
            elif state == "phase1":
                text = "미러링 (판별 중...)"
                color = _pal_current()["media_phase1"]
            else:
                text = "미러링 사용 중"
                color = _pal_current()["media_mirror"]

        self.lbl_media_source.setText(text)
        self.lbl_media_source.setStyleSheet(
            f"color:{color};font-size:11px;font-weight:bold;"
            "border:none;background:transparent;"
        )

    def update_media_thumbnail(self, frame):
        """앨범아트 프레임을 썸네일로 표시."""
        if frame is None or not self._media_active:
            return
        try:
            from PySide6.QtGui import QImage, QPixmap
            h, w = frame.shape[:2]
            bytes_per_line = 3 * w
            qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qimg)
            scaled = pixmap.scaled(
                54, 54,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.lbl_media_thumbnail.setPixmap(scaled)
        except Exception:
            pass

    def set_media_thumbnail_placeholder(self):
        """썸네일을 플레이스홀더로 리셋."""
        self.lbl_media_thumbnail.clear()
        self.lbl_media_thumbnail.setText("♪")

    # ══════════════════════════════════════════════════════════════
    #  프리셋 수집/적용 (Step 2a)
    # ══════════════════════════════════════════════════════════════

    def collect_for_preset(self):
        return {
            "smoothing_factor": self.slider_smoothing.value(),
            "mirror_n_zones": self.combo_zone_count.currentData() or -1,
            "color_extract_mode": self.combo_extract_mode.currentData() or "average",
            "mirror_color_effect": self._color_effect,
            "mirror_gradient_speed": self.slider_gradient_speed.value(),
            "mirror_gradient_hue": self.slider_gradient_hue.value(),
            "mirror_gradient_sv": self.slider_gradient_sv.value(),
            "media_source_override": self.combo_media_source.currentData() or "auto",
        }

    def apply_from_preset(self, data):
        if "smoothing_factor" in data:
            self.slider_smoothing.blockSignals(True)
            self.slider_smoothing.setValue(int(data["smoothing_factor"]))
            self.slider_smoothing.blockSignals(False)
            self.lbl_smoothing.setText(f"{data['smoothing_factor'] / 100:.2f}")

        if "mirror_n_zones" in data:
            self.combo_zone_count.blockSignals(True)
            target = data["mirror_n_zones"]
            for i in range(self.combo_zone_count.count()):
                if self.combo_zone_count.itemData(i) == target:
                    self.combo_zone_count.setCurrentIndex(i)
                    break
            self.combo_zone_count.blockSignals(False)

        if "color_extract_mode" in data:
            self.combo_extract_mode.blockSignals(True)
            target = data["color_extract_mode"]
            for i in range(self.combo_extract_mode.count()):
                if self.combo_extract_mode.itemData(i) == target:
                    self.combo_extract_mode.setCurrentIndex(i)
                    break
            self.combo_extract_mode.blockSignals(False)

        if "mirror_color_effect" in data:
            effect_to_idx = {
                COLOR_EFFECT_STATIC: 0,
                COLOR_EFFECT_GRADIENT_CW: 1,
                COLOR_EFFECT_GRADIENT_CCW: 2,
            }
            idx = effect_to_idx.get(data["mirror_color_effect"], 0)
            self.combo_color_effect.blockSignals(True)
            self.combo_color_effect.setCurrentIndex(idx)
            self.combo_color_effect.blockSignals(False)
            self._color_effect = data["mirror_color_effect"]
            is_static = self._color_effect == COLOR_EFFECT_STATIC
            self._row_speed.setVisible(not is_static)
            self._row_hue.setVisible(not is_static)
            self._row_sv.setVisible(not is_static)

        if "mirror_gradient_speed" in data:
            self.slider_gradient_speed.blockSignals(True)
            self.slider_gradient_speed.setValue(int(data["mirror_gradient_speed"]))
            self.slider_gradient_speed.blockSignals(False)
            self.lbl_gradient_speed.setText(f"{data['mirror_gradient_speed']}%")

        if "mirror_gradient_hue" in data:
            self.slider_gradient_hue.blockSignals(True)
            self.slider_gradient_hue.setValue(int(data["mirror_gradient_hue"]))
            self.slider_gradient_hue.blockSignals(False)
            self.lbl_gradient_hue.setText(f"{data['mirror_gradient_hue']}%")

        if "mirror_gradient_sv" in data:
            self.slider_gradient_sv.blockSignals(True)
            self.slider_gradient_sv.setValue(int(data["mirror_gradient_sv"]))
            self.slider_gradient_sv.blockSignals(False)
            self.lbl_gradient_sv.setText(f"{data['mirror_gradient_sv']}%")

        if "media_source_override" in data:
            self.combo_media_source.blockSignals(True)
            target = data["media_source_override"]
            for i in range(self.combo_media_source.count()):
                if self.combo_media_source.itemData(i) == target:
                    self.combo_media_source.setCurrentIndex(i)
                    break
            self.combo_media_source.blockSignals(False)
            if hasattr(self, "btn_toggle_source"):
                self.btn_toggle_source.setEnabled(target == "auto")

    # ── collect / apply / load ───────────────────────────────────

    def collect_params(self):
        params = {
            "smoothing_factor": self.slider_smoothing.value() / 100.0,
            "mirror_n_zones": self.combo_zone_count.currentData() or N_ZONES_PER_LED,
            "color_extract_mode": self.combo_extract_mode.currentData() or "average",
            "color_effect": self._color_effect,
            "gradient_speed": gradient_speed_from_slider(self.slider_gradient_speed.value()),
            "gradient_hue_range": self.slider_gradient_hue.value() / 100.0 * 0.20,
            "gradient_sv_range": self.slider_gradient_sv.value() / 100.0,
        }
        if self._media_active:
            params["media_source_override"] = (
                self.combo_media_source.currentData() or "auto"
            )
            params["media_decision_toggle_count"] = self._media_toggle_count
        return params

    def get_layout_params(self):
        params = {
            "decay_radius": self.spin_decay.value(),
            "parallel_penalty": self.spin_penalty.value(),
        }
        if self.chk_per_side.isChecked():
            params["decay_per_side"] = {
                s: self.spin_decay_per[s].value() for s in self.spin_decay_per
            }
            params["penalty_per_side"] = {
                s: self.spin_penalty_per[s].value() for s in self.spin_penalty_per
            }
        else:
            params["decay_per_side"] = {}
            params["penalty_per_side"] = {}
        return params

    def apply_to_config(self):
        m = self._config.setdefault("mirror", {})
        m["smoothing_factor"] = self.slider_smoothing.value() / 100.0
        m["zone_count"] = self.combo_zone_count.currentData() or N_ZONES_PER_LED
        m["color_extract_mode"] = self.combo_extract_mode.currentData() or "average"
        m["decay_radius"] = self.spin_decay.value()
        m["parallel_penalty"] = self.spin_penalty.value()
        if self.chk_per_side.isChecked():
            m["decay_radius_per_side"] = {
                s: self.spin_decay_per[s].value() for s in self.spin_decay_per
            }
            m["parallel_penalty_per_side"] = {
                s: self.spin_penalty_per[s].value() for s in self.spin_penalty_per
            }
        else:
            m["decay_radius_per_side"] = {}
            m["parallel_penalty_per_side"] = {}
        m["color_effect"] = self._color_effect
        m["gradient_speed"] = self.slider_gradient_speed.value()
        m["gradient_hue"] = self.slider_gradient_hue.value()
        m["gradient_sv"] = self.slider_gradient_sv.value()
        m["media_source_override"] = self.combo_media_source.currentData() or "auto"

    def load_from_config(self):
        m = self._config.get("mirror", {})

        sf = m.get("smoothing_factor", 0.5)
        self.slider_smoothing.blockSignals(True)
        self.slider_smoothing.setValue(int(sf * 100))
        self.slider_smoothing.blockSignals(False)
        self.lbl_smoothing.setText(f"{sf:.2f}")

        saved_zone = m.get("zone_count", N_ZONES_PER_LED)
        self.combo_zone_count.blockSignals(True)
        for i in range(self.combo_zone_count.count()):
            if self.combo_zone_count.itemData(i) == saved_zone:
                self.combo_zone_count.setCurrentIndex(i)
                break
        self.combo_zone_count.blockSignals(False)

        saved_extract = m.get("color_extract_mode", "average")
        self.combo_extract_mode.blockSignals(True)
        for i in range(self.combo_extract_mode.count()):
            if self.combo_extract_mode.itemData(i) == saved_extract:
                self.combo_extract_mode.setCurrentIndex(i)
                break
        self.combo_extract_mode.blockSignals(False)

        self.spin_decay.setValue(m.get("decay_radius", 0.3))
        self.spin_penalty.setValue(m.get("parallel_penalty", 5.0))

        per_decay = m.get("decay_radius_per_side", {})
        per_penalty = m.get("parallel_penalty_per_side", {})
        has_per_side = bool(per_decay or per_penalty)
        self.chk_per_side.setChecked(has_per_side)
        self._per_side_widget.setVisible(has_per_side)
        for side in self.spin_decay_per:
            self.spin_decay_per[side].setValue(
                per_decay.get(side, m.get("decay_radius", 0.3))
            )
        for side in self.spin_penalty_per:
            self.spin_penalty_per[side].setValue(
                per_penalty.get(side, m.get("parallel_penalty", 5.0))
            )

        saved_effect = m.get("color_effect", None)
        if saved_effect is None:
            state = self._config.get("options", {}).get("audio_state", {})
            saved_effect = state.get("color_effect", COLOR_EFFECT_STATIC)
        if saved_effect == "rainbow_time":
            saved_effect = COLOR_EFFECT_STATIC
        effect_idx = _MIRROR_EFFECT_TO_INDEX.get(saved_effect, 0)
        self.combo_color_effect.blockSignals(True)
        self.combo_color_effect.setCurrentIndex(effect_idx)
        self.combo_color_effect.blockSignals(False)
        self._on_color_effect_changed(effect_idx)

        state = self._config.get("options", {}).get("audio_state", {})
        self.slider_gradient_speed.blockSignals(True)
        self.slider_gradient_speed.setValue(
            m.get("gradient_speed", state.get("gradient_speed", 50)))
        self.slider_gradient_speed.blockSignals(False)
        self.lbl_gradient_speed.setText(f"{self.slider_gradient_speed.value()}%")

        self.slider_gradient_hue.blockSignals(True)
        self.slider_gradient_hue.setValue(
            m.get("gradient_hue", state.get("gradient_hue", 40)))
        self.slider_gradient_hue.blockSignals(False)
        self.lbl_gradient_hue.setText(f"{self.slider_gradient_hue.value()}%")

        self.slider_gradient_sv.blockSignals(True)
        self.slider_gradient_sv.setValue(
            m.get("gradient_sv", state.get("gradient_sv", 50)))
        self.slider_gradient_sv.blockSignals(False)
        self.lbl_gradient_sv.setText(f"{self.slider_gradient_sv.value()}%")

        saved_override = m.get("media_source_override", "auto")
        self.combo_media_source.blockSignals(True)
        for i in range(self.combo_media_source.count()):
            if self.combo_media_source.itemData(i) == saved_override:
                self.combo_media_source.setCurrentIndex(i)
                break
        self.combo_media_source.blockSignals(False)

        if hasattr(self, "btn_toggle_source"):
            self.btn_toggle_source.setEnabled(
                self.combo_media_source.currentData() == "auto"
            )