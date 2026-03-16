"""오디오 파라미터 위젯 — 감도/밝기/Attack/Release/Wave속도/대역 비율 공용.

[NEW] Wave 모드 전용 wave_speed 슬라이더 추가.
[Phase 4] Flowing 모드 기본값 추가 + set_audio_mode 대응.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
from PySide6.QtCore import Qt, Signal

from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.zone_balance import ZoneBalanceWidget

AUDIO_DEFAULTS = {
    "pulse": {"bass_sens": 100, "mid_sens": 100, "high_sens": 100, "brightness": 100, "attack": 50, "release": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "spectrum": {"bass_sens": 100, "mid_sens": 100, "high_sens": 100, "brightness": 100, "attack": 50, "release": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "bass_detail": {"bass_sens": 100, "mid_sens": 100, "high_sens": 100, "brightness": 100, "attack": 10, "release": 70, "zone_bass": 48, "zone_mid": 26, "zone_high": 26},
    "wave": {"bass_sens": 120, "mid_sens": 100, "high_sens": 100, "brightness": 100, "attack": 60, "release": 40, "wave_speed": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "dynamic": {"bass_sens": 110, "mid_sens": 110, "high_sens": 120, "brightness": 100, "attack": 55, "release": 45, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    # ★ Phase 4: Flowing 기본값
    "flowing": {"bass_sens": 100, "mid_sens": 100, "high_sens": 100, "brightness": 100, "attack": 40, "release": 60, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
}


class AudioParamWidget(QWidget):
    params_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._updating = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        # ── 감도 ──
        self.label_sens = QLabel("감도 (Bass)")
        layout.addWidget(self.label_sens)
        self.slider_bass_sens, self.lbl_bass_sens = self._add_slider(layout, "Bass:", 10, 300, 100)

        self.row_mid_sens = QWidget()
        rm = QHBoxLayout(self.row_mid_sens)
        rm.setContentsMargins(0, 0, 0, 0)
        rm.addWidget(QLabel("Mid:"))
        self.slider_mid_sens = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_mid_sens.setRange(10, 300)
        self.slider_mid_sens.setValue(100)
        self.slider_mid_sens.valueChanged.connect(self._on_changed)
        rm.addWidget(self.slider_mid_sens)
        self.lbl_mid_sens = QLabel("1.00")
        self.lbl_mid_sens.setMinimumWidth(40)
        self.lbl_mid_sens.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rm.addWidget(self.lbl_mid_sens)
        layout.addWidget(self.row_mid_sens)

        self.row_high_sens = QWidget()
        rh = QHBoxLayout(self.row_high_sens)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(QLabel("High:"))
        self.slider_high_sens = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_high_sens.setRange(10, 300)
        self.slider_high_sens.setValue(100)
        self.slider_high_sens.valueChanged.connect(self._on_changed)
        rh.addWidget(self.slider_high_sens)
        self.lbl_high_sens = QLabel("1.00")
        self.lbl_high_sens.setMinimumWidth(40)
        self.lbl_high_sens.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rh.addWidget(self.lbl_high_sens)
        layout.addWidget(self.row_high_sens)

        ln = QFrame(); ln.setFrameShape(QFrame.Shape.HLine); ln.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(ln)

        # ── 밝기 ──
        self.slider_brightness, self.lbl_brightness = self._add_slider(layout, "밝기:", 0, 100, 100, suffix="%")

        # ── 반응 특성 ──
        layout.addWidget(QLabel("반응 특성"))
        self.slider_attack, self.lbl_attack = self._add_slider(layout, "Attack:", 0, 100, 50)
        self.slider_release, self.lbl_release = self._add_slider(layout, "Release:", 0, 100, 50)

        # ── Wave 속도 (wave 모드 전용) ──
        self.wave_speed_line = QFrame()
        self.wave_speed_line.setFrameShape(QFrame.Shape.HLine)
        self.wave_speed_line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(self.wave_speed_line)

        self.row_wave_speed = QWidget()
        ws = QHBoxLayout(self.row_wave_speed)
        ws.setContentsMargins(0, 0, 0, 0)
        ws.addWidget(QLabel("Wave 속도:"))
        self.slider_wave_speed = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_wave_speed.setRange(0, 100)
        self.slider_wave_speed.setValue(50)
        self.slider_wave_speed.valueChanged.connect(self._on_wave_speed_changed)
        ws.addWidget(self.slider_wave_speed)
        self.lbl_wave_speed = QLabel("50%")
        self.lbl_wave_speed.setMinimumWidth(40)
        self.lbl_wave_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        ws.addWidget(self.lbl_wave_speed)
        layout.addWidget(self.row_wave_speed)

        self.wave_speed_hint = QLabel("0% = 느린 연출  |  100% = 빠른 비트")
        self.wave_speed_hint.setStyleSheet("color:#888;font-size:10px;")
        layout.addWidget(self.wave_speed_hint)

        # ── 대역 비율 (spectrum/bass_detail 전용) ──
        self.zone_line = QFrame()
        self.zone_line.setFrameShape(QFrame.Shape.HLine)
        self.zone_line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(self.zone_line)
        self.zone_label = QLabel("대역 비율 (주파수 분배)")
        self.zone_label.setStyleSheet("font-weight:bold;")
        layout.addWidget(self.zone_label)
        self.zone_balance = ZoneBalanceWidget(33, 33, 34)
        self.zone_balance.zone_changed.connect(lambda *_: self._on_changed())
        layout.addWidget(self.zone_balance)

        # ── 모드별 가시성 그룹 ──
        self._spectrum_only = [self.row_mid_sens, self.row_high_sens,
                               self.zone_line, self.zone_label, self.zone_balance]
        self._wave_only = [self.wave_speed_line, self.row_wave_speed, self.wave_speed_hint]

    def _add_slider(self, parent_layout, label_text, min_v, max_v, default, suffix=""):
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        s = NoScrollSlider(Qt.Orientation.Horizontal)
        s.setRange(min_v, max_v)
        s.setValue(default)
        s.valueChanged.connect(self._on_changed)
        row.addWidget(s)
        lbl = QLabel(f"{default}{suffix}" if suffix == "%" else f"{default / 100:.2f}")
        lbl.setMinimumWidth(40)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl)
        parent_layout.addLayout(row)
        return s, lbl

    def _on_changed(self, _=None):
        if self._updating:
            return
        self._update_labels()
        self.params_changed.emit()

    def _on_wave_speed_changed(self, value):
        self.lbl_wave_speed.setText(f"{value}%")
        if not self._updating:
            self.params_changed.emit()

    def _update_labels(self):
        self.lbl_bass_sens.setText(f"{self.slider_bass_sens.value() / 100:.2f}")
        self.lbl_mid_sens.setText(f"{self.slider_mid_sens.value() / 100:.2f}")
        self.lbl_high_sens.setText(f"{self.slider_high_sens.value() / 100:.2f}")
        self.lbl_brightness.setText(f"{self.slider_brightness.value()}%")
        self.lbl_attack.setText(f"{self.slider_attack.value() / 100:.2f}")
        self.lbl_release.setText(f"{self.slider_release.value() / 100:.2f}")
        self.lbl_wave_speed.setText(f"{self.slider_wave_speed.value()}%")

    def set_audio_mode(self, mode_name):
        is_banded = mode_name in ("spectrum", "bass_detail")
        is_wave = mode_name == "wave"

        for w in self._spectrum_only:
            w.setVisible(is_banded)
        for w in self._wave_only:
            w.setVisible(is_wave)

        if mode_name == "bass_detail":
            self.label_sens.setText("감도 (Bass Detail)")
        elif mode_name == "spectrum":
            self.label_sens.setText("감도 (대역별)")
        elif mode_name == "wave":
            self.label_sens.setText("감도 (Bass → Wave)")
        elif mode_name == "dynamic":
            self.label_sens.setText("감도 (Dynamic)")
        elif mode_name == "flowing":
            # ★ Phase 4: flowing에서는 bass 감도가 밝기 반응에 영향
            self.label_sens.setText("감도 (Flowing)")
        else:
            self.label_sens.setText("감도 (Bass)")

    def get_params(self):
        zb, zm, zh = self.zone_balance.get_values()
        return {
            "bass_sens": self.slider_bass_sens.value(),
            "mid_sens": self.slider_mid_sens.value(),
            "high_sens": self.slider_high_sens.value(),
            "brightness": self.slider_brightness.value(),
            "attack": self.slider_attack.value(),
            "release": self.slider_release.value(),
            "wave_speed": self.slider_wave_speed.value(),
            "zone_bass": zb, "zone_mid": zm, "zone_high": zh,
        }

    def set_params(self, d, defaults=None):
        self._updating = True
        df = defaults or AUDIO_DEFAULTS["pulse"]
        self.slider_bass_sens.setValue(d.get("bass_sens", df["bass_sens"]))
        self.slider_mid_sens.setValue(d.get("mid_sens", df["mid_sens"]))
        self.slider_high_sens.setValue(d.get("high_sens", df["high_sens"]))
        self.slider_brightness.setValue(d.get("brightness", df["brightness"]))
        self.slider_attack.setValue(d.get("attack", df["attack"]))
        self.slider_release.setValue(d.get("release", df["release"]))
        self.slider_wave_speed.setValue(d.get("wave_speed", df.get("wave_speed", 50)))
        self.zone_balance.set_values(
            d.get("zone_bass", df["zone_bass"]),
            d.get("zone_mid", df["zone_mid"]),
            d.get("zone_high", df["zone_high"]),
        )
        self._update_labels()
        self._updating = False

    def save_to_dict(self, d):
        d.update(self.get_params())
