"""미러링 모드 패널 — 구역 수, 밝기/스무딩, 감쇠/페널티."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QComboBox, QSlider, QDoubleSpinBox, QCheckBox, QGridLayout,
)
from PySide6.QtCore import Qt, Signal
from core.engine_utils import N_ZONES_PER_LED

_ZONE_OPTIONS = [
    (1, "1구역 (화면 전체 평균)"), (2, "2구역 (상/하)"),
    (4, "4구역 (상하좌우)"), (8, "8구역 (모서리 포함)"),
    (16, "16구역"), (32, "32구역"),
    (N_ZONES_PER_LED, "LED별 개별 (미러링)"),
]


class MirrorPanel(QWidget):
    brightness_changed = Signal(int)
    smoothing_changed = Signal(bool)
    smoothing_factor_changed = Signal(float)
    layout_params_changed = Signal()
    zone_count_changed = Signal(int)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._build_ui()
        self.load_from_config(config)

    def _build_ui(self):
        mirror_cfg = self._config.get("mirror", {})
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(2)

        # 구역 수
        zone_row = QHBoxLayout()
        zone_row.addWidget(QLabel("구역 수:"))
        self.combo_zone_count = QComboBox()
        self.combo_zone_count.addItem("LED별 개별 (기본)", N_ZONES_PER_LED)
        for n, label in _ZONE_OPTIONS:
            if n != N_ZONES_PER_LED:
                self.combo_zone_count.addItem(label, n)
        self.combo_zone_count.currentIndexChanged.connect(self._on_zone_count_changed)
        zone_row.addWidget(self.combo_zone_count)
        zone_row.addStretch()
        layout.addLayout(zone_row)

        # 밝기 + 스무딩
        ctrl_group = QGroupBox("밝기 / 스무딩")
        cl = QVBoxLayout(ctrl_group)
        cl.setSpacing(2)
        cl.setContentsMargins(6, 14, 6, 2)

        bright_row = QHBoxLayout()
        bright_row.addWidget(QLabel("밝기:"))
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(0, 100)
        self.brightness_slider.setValue(int(mirror_cfg.get("brightness", 1.0) * 100))
        self.brightness_slider.valueChanged.connect(self._on_brightness_changed)
        bright_row.addWidget(self.brightness_slider)
        self.brightness_label = QLabel(f'{int(mirror_cfg.get("brightness", 1.0) * 100)}%')
        self.brightness_label.setMinimumWidth(35)
        self.brightness_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bright_row.addWidget(self.brightness_label)
        cl.addLayout(bright_row)

        smooth_row = QHBoxLayout()
        self.chk_smoothing = QCheckBox("스무딩")
        self.chk_smoothing.setChecked(True)
        self.chk_smoothing.stateChanged.connect(lambda s: self.smoothing_changed.emit(bool(s)))
        smooth_row.addWidget(self.chk_smoothing)
        smooth_row.addWidget(QLabel("계수:"))
        self.spin_smoothing = QDoubleSpinBox()
        self.spin_smoothing.setRange(0.0, 0.95)
        self.spin_smoothing.setSingleStep(0.05)
        self.spin_smoothing.setValue(mirror_cfg.get("smoothing_factor", 0.5))
        self.spin_smoothing.valueChanged.connect(self.smoothing_factor_changed.emit)
        smooth_row.addWidget(self.spin_smoothing)
        smooth_row.addStretch()
        cl.addLayout(smooth_row)
        layout.addWidget(ctrl_group)

        # 감쇠 / 페널티
        decay_group = QGroupBox("감쇠 / 타원 페널티")
        dl = QVBoxLayout(decay_group)
        dl.setSpacing(3)
        dl.setContentsMargins(6, 16, 6, 4)

        global_row = QHBoxLayout()
        global_row.addWidget(QLabel("감쇠 반경:"))
        self.spin_decay = QDoubleSpinBox()
        self.spin_decay.setRange(0.05, 1.0); self.spin_decay.setSingleStep(0.05)
        self.spin_decay.setValue(mirror_cfg.get("decay_radius", 0.3))
        self.spin_decay.valueChanged.connect(lambda _: self.layout_params_changed.emit())
        global_row.addWidget(self.spin_decay)
        global_row.addWidget(QLabel("타원 페널티:"))
        self.spin_penalty = QDoubleSpinBox()
        self.spin_penalty.setRange(1.0, 10.0); self.spin_penalty.setSingleStep(0.5)
        self.spin_penalty.setValue(mirror_cfg.get("parallel_penalty", 5.0))
        self.spin_penalty.valueChanged.connect(lambda _: self.layout_params_changed.emit())
        global_row.addWidget(self.spin_penalty)
        global_row.addStretch()
        dl.addLayout(global_row)

        per_decay = mirror_cfg.get("decay_radius_per_side", {})
        per_penalty = mirror_cfg.get("parallel_penalty_per_side", {})
        has_per_side = bool(per_decay or per_penalty)
        self.chk_per_side = QCheckBox("변별 값 사용")
        self.chk_per_side.setChecked(has_per_side)
        self.chk_per_side.stateChanged.connect(lambda _: self.layout_params_changed.emit())
        dl.addWidget(self.chk_per_side)

        per_side_grid = QGridLayout(); per_side_grid.setSpacing(2)
        sides = ["top", "bottom", "left", "right"]
        side_labels = {"top": "상단", "bottom": "하단", "left": "좌측", "right": "우측"}
        per_side_grid.addWidget(QLabel(""), 0, 0)
        per_side_grid.addWidget(QLabel("감쇠 반경"), 0, 1)
        per_side_grid.addWidget(QLabel("타원 페널티"), 0, 2)
        self.spin_decay_per = {}; self.spin_penalty_per = {}
        for row_i, side in enumerate(sides, 1):
            per_side_grid.addWidget(QLabel(side_labels[side]), row_i, 0)
            sp_d = QDoubleSpinBox(); sp_d.setRange(0.05, 1.0); sp_d.setSingleStep(0.05)
            sp_d.setValue(per_decay.get(side, mirror_cfg.get("decay_radius", 0.3)))
            sp_d.valueChanged.connect(lambda _: self.layout_params_changed.emit())
            self.spin_decay_per[side] = sp_d; per_side_grid.addWidget(sp_d, row_i, 1)
            sp_p = QDoubleSpinBox(); sp_p.setRange(1.0, 10.0); sp_p.setSingleStep(0.5)
            sp_p.setValue(per_penalty.get(side, mirror_cfg.get("parallel_penalty", 5.0)))
            sp_p.valueChanged.connect(lambda _: self.layout_params_changed.emit())
            self.spin_penalty_per[side] = sp_p; per_side_grid.addWidget(sp_p, row_i, 2)
        self.per_side_widget = QWidget(); self.per_side_widget.setLayout(per_side_grid)
        self.per_side_widget.setVisible(has_per_side)
        self.chk_per_side.stateChanged.connect(lambda s: self.per_side_widget.setVisible(bool(s)))
        dl.addWidget(self.per_side_widget)
        layout.addWidget(decay_group)

    def _on_brightness_changed(self, value):
        self.brightness_label.setText(f"{value}%")
        self.brightness_changed.emit(value)

    def _on_zone_count_changed(self, _idx):
        n = self.combo_zone_count.currentData()
        if n is not None: self.zone_count_changed.emit(n)

    def get_layout_params(self):
        params = {"decay_radius": self.spin_decay.value(), "parallel_penalty": self.spin_penalty.value()}
        if self.chk_per_side.isChecked():
            params["decay_per_side"] = {s: self.spin_decay_per[s].value() for s in self.spin_decay_per}
            params["penalty_per_side"] = {s: self.spin_penalty_per[s].value() for s in self.spin_penalty_per}
        else:
            params["decay_per_side"] = {}; params["penalty_per_side"] = {}
        return params

    def apply_to_config(self, config):
        m = config.setdefault("mirror", {})
        m["brightness"] = self.brightness_slider.value() / 100.0
        m["smoothing_factor"] = self.spin_smoothing.value()
        m["decay_radius"] = self.spin_decay.value()
        m["parallel_penalty"] = self.spin_penalty.value()
        m["zone_count"] = self.combo_zone_count.currentData() or -1
        if self.chk_per_side.isChecked():
            m["decay_radius_per_side"] = {s: self.spin_decay_per[s].value() for s in self.spin_decay_per}
            m["parallel_penalty_per_side"] = {s: self.spin_penalty_per[s].value() for s in self.spin_penalty_per}
        else:
            m["decay_radius_per_side"] = {}; m["parallel_penalty_per_side"] = {}

    def load_from_config(self, config):
        m = config.get("mirror", {})
        self.brightness_slider.setValue(int(m.get("brightness", 1.0) * 100))
        self.spin_smoothing.setValue(m.get("smoothing_factor", 0.5))
        self.spin_decay.setValue(m.get("decay_radius", 0.3))
        self.spin_penalty.setValue(m.get("parallel_penalty", 5.0))
        per_decay = m.get("decay_radius_per_side", {}); per_penalty = m.get("parallel_penalty_per_side", {})
        has_per_side = bool(per_decay or per_penalty)
        self.chk_per_side.setChecked(has_per_side); self.per_side_widget.setVisible(has_per_side)
        for side in self.spin_decay_per:
            self.spin_decay_per[side].setValue(per_decay.get(side, m.get("decay_radius", 0.3)))
        for side in self.spin_penalty_per:
            self.spin_penalty_per[side].setValue(per_penalty.get(side, m.get("parallel_penalty", 5.0)))
        saved_zone = m.get("zone_count", -1)
        self.combo_zone_count.blockSignals(True)
        for i in range(self.combo_zone_count.count()):
            if self.combo_zone_count.itemData(i) == saved_zone:
                self.combo_zone_count.setCurrentIndex(i); break
        self.combo_zone_count.blockSignals(False)
