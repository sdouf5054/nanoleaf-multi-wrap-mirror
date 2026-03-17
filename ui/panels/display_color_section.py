"""디스플레이 OFF 색상 패널 — 색상 효과 + 프리셋 + 커스텀 (Phase 2)

기존 AudioPanel의 색상 그룹박스에서 추출.
디스플레이 토글 OFF일 때 표시되는 색상 선택 UI.

[기존 대비 변경]
- 최소 밝기 슬라이더 제거 (오디오 패널로 이동)
- 색상 효과 콤보: 정적 / 그라데이션 CW/CCW / 무지개(시간 순회) — 4개
- 그라데이션 선택 시: 효과 속도, 색조/밝기 변동 슬라이더
- 무지개(시간 순회) 선택 시: 효과 속도 슬라이더만
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QComboBox, QFrame, QPushButton, QGridLayout, QColorDialog,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

from ui.widgets.no_scroll_slider import NoScrollSlider
from core.engine_utils import (
    COLOR_EFFECT_STATIC, COLOR_EFFECT_GRADIENT_CW,
    COLOR_EFFECT_GRADIENT_CCW, COLOR_EFFECT_RAINBOW_TIME,
    gradient_speed_from_slider,
)

# ── 색상 프리셋 ──
_COLOR_PRESETS = [
    ("무지개", None, None, None),
    ("핑크/마젠타", 255, 0, 80),
    ("빨강", 255, 30, 0),
    ("주황", 255, 120, 0),
    ("노랑", 255, 220, 0),
    ("초록", 0, 255, 80),
    ("시안", 0, 220, 255),
    ("파랑", 30, 0, 255),
    ("보라", 150, 0, 255),
    ("흰색", 255, 255, 255),
]

# ── 색상 효과 콤보 항목 ──
_COLOR_EFFECT_ITEMS = [
    "정적",
    "그라데이션 (CW)",
    "그라데이션 (CCW)",
    "무지개 (시간 순회)",
]
_INDEX_COLOR_EFFECT = {
    0: COLOR_EFFECT_STATIC,
    1: COLOR_EFFECT_GRADIENT_CW,
    2: COLOR_EFFECT_GRADIENT_CCW,
    3: COLOR_EFFECT_RAINBOW_TIME,
}
_COLOR_EFFECT_TO_INDEX = {v: k for k, v in _INDEX_COLOR_EFFECT.items()}

# ── 레이아웃 상수 ──
_GROUP_MARGINS = (6, 16, 6, 6)
_GROUP_SPACING = 4


class DisplayColorSection(QWidget):
    """디스플레이 OFF 색상 패널.

    Signals:
        params_changed(): 색상/효과 파라미터가 변경되었을 때 emit
    """

    params_changed = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._is_rainbow = True
        self._current_color = (255, 0, 80)
        self._color_effect = COLOR_EFFECT_STATIC
        self._build_ui()
        self.load_from_config()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        grp = QGroupBox("색상")
        gl = QVBoxLayout(grp)
        gl.setSpacing(_GROUP_SPACING)
        gl.setContentsMargins(*_GROUP_MARGINS)

        # ── 색상 효과 콤보 ──
        effect_row = QHBoxLayout()
        effect_row.addWidget(QLabel("색상 효과:"))
        self.combo_color_effect = QComboBox()
        self.combo_color_effect.addItems(_COLOR_EFFECT_ITEMS)
        self.combo_color_effect.currentIndexChanged.connect(self._on_color_effect_changed)
        effect_row.addWidget(self.combo_color_effect)
        effect_row.addStretch()
        gl.addLayout(effect_row)

        # ── 효과 속도 슬라이더 (static 외 모든 효과) ──
        self._row_speed = QWidget()
        rs = QHBoxLayout(self._row_speed)
        rs.setContentsMargins(0, 0, 0, 0)
        rs.addWidget(QLabel("효과 속도:"))
        self.slider_gradient_speed = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_speed.setRange(0, 100)
        self.slider_gradient_speed.setValue(50)
        self.slider_gradient_speed.valueChanged.connect(self._on_slider_changed)
        rs.addWidget(self.slider_gradient_speed)
        self.lbl_gradient_speed = QLabel("50%")
        self.lbl_gradient_speed.setMinimumWidth(35)
        self.lbl_gradient_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rs.addWidget(self.lbl_gradient_speed)
        gl.addWidget(self._row_speed)
        self._row_speed.setVisible(False)

        # ── 색조 변동 슬라이더 (그라데이션 CW/CCW만) ──
        self._row_hue = QWidget()
        rh = QHBoxLayout(self._row_hue)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(QLabel("색조 변동:"))
        self.slider_gradient_hue = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_hue.setRange(0, 100)
        self.slider_gradient_hue.setValue(40)
        self.slider_gradient_hue.valueChanged.connect(self._on_slider_changed)
        rh.addWidget(self.slider_gradient_hue)
        self.lbl_gradient_hue = QLabel("40%")
        self.lbl_gradient_hue.setMinimumWidth(35)
        self.lbl_gradient_hue.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rh.addWidget(self.lbl_gradient_hue)
        gl.addWidget(self._row_hue)
        self._row_hue.setVisible(False)

        # ── 밝기 변동 슬라이더 (그라데이션 CW/CCW만) ──
        self._row_sv = QWidget()
        rv = QHBoxLayout(self._row_sv)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("밝기 변동:"))
        self.slider_gradient_sv = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_sv.setRange(0, 100)
        self.slider_gradient_sv.setValue(50)
        self.slider_gradient_sv.valueChanged.connect(self._on_slider_changed)
        rv.addWidget(self.slider_gradient_sv)
        self.lbl_gradient_sv = QLabel("50%")
        self.lbl_gradient_sv.setMinimumWidth(35)
        self.lbl_gradient_sv.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rv.addWidget(self.lbl_gradient_sv)
        gl.addWidget(self._row_sv)
        self._row_sv.setVisible(False)

        # ── 구분선 ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        gl.addWidget(sep)

        # ── 색상 프리셋 그리드 ──
        gl.addWidget(QLabel("색상 프리셋"))
        pg = QGridLayout()
        self._preset_buttons = []
        for i, (name, r, g, b) in enumerate(_COLOR_PRESETS):
            btn = QPushButton(name)
            btn.setMinimumHeight(26)
            if r is None:
                btn.setStyleSheet(
                    "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                    "stop:0 red,stop:0.17 orange,stop:0.33 yellow,"
                    "stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
                    "color:white;font-weight:bold;border-radius:4px;font-size:11px;"
                )
                btn.clicked.connect(self._set_rainbow)
            else:
                tc = "#000" if (r + g + b) > 380 else "#fff"
                btn.setStyleSheet(
                    f"background:rgb({r},{g},{b});color:{tc};"
                    "font-weight:bold;border-radius:4px;font-size:11px;"
                )
                btn.clicked.connect(lambda _, rgb=(r, g, b): self._set_color(*rgb))
            pg.addWidget(btn, i // 5, i % 5)
            self._preset_buttons.append(btn)
        gl.addLayout(pg)

        # ── 커스텀 + 미리보기 ──
        cr = QHBoxLayout()
        self.btn_custom = QPushButton("커스텀")
        self.btn_custom.clicked.connect(self._pick_custom_color)
        cr.addWidget(self.btn_custom)
        self.color_preview = QFrame()
        self.color_preview.setFixedSize(40, 26)
        cr.addWidget(self.color_preview)
        cr.addStretch()
        gl.addLayout(cr)

        layout.addWidget(grp)
        self._update_color_preview()

    # ── 색상 효과 변경 ──────────────────────────────────────────

    def _on_color_effect_changed(self, idx):
        self._color_effect = _INDEX_COLOR_EFFECT.get(idx, COLOR_EFFECT_STATIC)
        self._update_effect_visibility()
        self._update_preset_enabled()
        self._update_color_preview()
        self.params_changed.emit()

    def _update_effect_visibility(self):
        is_static = self._color_effect == COLOR_EFFECT_STATIC
        is_gradient = self._color_effect in (COLOR_EFFECT_GRADIENT_CW, COLOR_EFFECT_GRADIENT_CCW)
        self._row_speed.setVisible(not is_static)
        self._row_hue.setVisible(is_gradient)
        self._row_sv.setVisible(is_gradient)

    def _update_preset_enabled(self):
        is_rainbow_time = self._color_effect == COLOR_EFFECT_RAINBOW_TIME
        for btn in self._preset_buttons:
            btn.setEnabled(not is_rainbow_time)
        self.btn_custom.setEnabled(not is_rainbow_time)

    def _on_slider_changed(self, _=None):
        self.lbl_gradient_speed.setText(f"{self.slider_gradient_speed.value()}%")
        self.lbl_gradient_hue.setText(f"{self.slider_gradient_hue.value()}%")
        self.lbl_gradient_sv.setText(f"{self.slider_gradient_sv.value()}%")
        self.params_changed.emit()

    # ── 색상 선택 ────────────────────────────────────────────────

    def _set_color(self, r, g, b):
        self._current_color = (r, g, b)
        self._is_rainbow = False
        self._update_color_preview()
        self.params_changed.emit()

    def _set_rainbow(self):
        self._is_rainbow = True
        self._update_color_preview()
        self.params_changed.emit()

    def _pick_custom_color(self):
        r, g, b = self._current_color
        c = QColorDialog.getColor(QColor(r, g, b), self, "기본 색상")
        if c.isValid():
            self._set_color(c.red(), c.green(), c.blue())

    def _update_color_preview(self):
        if self._color_effect == COLOR_EFFECT_RAINBOW_TIME:
            self.color_preview.setStyleSheet(
                "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                "stop:0 red,stop:0.17 orange,stop:0.33 yellow,"
                "stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
                "border:1px solid #555;border-radius:4px;"
            )
        elif self._is_rainbow:
            self.color_preview.setStyleSheet(
                "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                "stop:0 red,stop:0.17 orange,stop:0.33 yellow,"
                "stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
                "border:1px solid #555;border-radius:4px;"
            )
        else:
            r, g, b = self._current_color
            self.color_preview.setStyleSheet(
                f"background:rgb({r},{g},{b});border:1px solid #555;border-radius:4px;"
            )

    # ── collect / apply / load ───────────────────────────────────

    def collect_params(self):
        """현재 색상 파라미터를 dict로 반환."""
        return {
            "rainbow": self._is_rainbow,
            "base_color": self._current_color,
            "color_effect": self._color_effect,
            "gradient_speed": gradient_speed_from_slider(self.slider_gradient_speed.value()),
            "gradient_hue_range": self.slider_gradient_hue.value() / 100.0 * 0.20,
            "gradient_sv_range": self.slider_gradient_sv.value() / 100.0,
        }

    def apply_to_config(self):
        """현재 상태를 config에 반영."""
        state = self._config.setdefault("options", {}).setdefault("audio_state", {})
        state["color_rainbow"] = self._is_rainbow
        state["color_rgb"] = list(self._current_color)
        state["color_effect"] = self._color_effect
        state["gradient_speed"] = self.slider_gradient_speed.value()
        state["gradient_hue"] = self.slider_gradient_hue.value()
        state["gradient_sv"] = self.slider_gradient_sv.value()

    def load_from_config(self):
        """config에서 상태 복원."""
        state = self._config.get("options", {}).get("audio_state", {})
        self._is_rainbow = state.get("color_rainbow", True)
        rgb = state.get("color_rgb", [255, 0, 80])
        self._current_color = tuple(rgb) if isinstance(rgb, list) else (255, 0, 80)

        self._color_effect = state.get("color_effect", COLOR_EFFECT_STATIC)
        effect_idx = _COLOR_EFFECT_TO_INDEX.get(self._color_effect, 0)
        self.combo_color_effect.blockSignals(True)
        self.combo_color_effect.setCurrentIndex(effect_idx)
        self.combo_color_effect.blockSignals(False)

        self.slider_gradient_speed.blockSignals(True)
        self.slider_gradient_speed.setValue(state.get("gradient_speed", 50))
        self.slider_gradient_speed.blockSignals(False)
        self.lbl_gradient_speed.setText(f"{self.slider_gradient_speed.value()}%")

        self.slider_gradient_hue.blockSignals(True)
        self.slider_gradient_hue.setValue(state.get("gradient_hue", 40))
        self.slider_gradient_hue.blockSignals(False)
        self.lbl_gradient_hue.setText(f"{self.slider_gradient_hue.value()}%")

        self.slider_gradient_sv.blockSignals(True)
        self.slider_gradient_sv.setValue(state.get("gradient_sv", 50))
        self.slider_gradient_sv.blockSignals(False)
        self.lbl_gradient_sv.setText(f"{self.slider_gradient_sv.value()}%")

        self._update_effect_visibility()
        self._update_preset_enabled()
        self._update_color_preview()
