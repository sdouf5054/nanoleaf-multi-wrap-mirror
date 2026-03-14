"""오디오 모드 패널 — 에너지 레벨, 색상 팔레트, 비주얼라이저 모드, 파라미터."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QComboBox, QFrame, QPushButton, QProgressBar, QGridLayout, QColorDialog,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.spectrum import SpectrumWidget
from ui.widgets.audio_param_widget import AudioParamWidget, AUDIO_DEFAULTS

_INDEX_AUDIO_MODE = {0: "pulse", 1: "spectrum", 2: "bass_detail"}
_MODE_TO_INDEX = {"pulse": 0, "spectrum": 1, "bass_detail": 2}
_COLOR_PRESETS = [
    ("무지개", None, None, None), ("핑크/마젠타", 255, 0, 80), ("빨강", 255, 30, 0),
    ("주황", 255, 120, 0), ("노랑", 255, 220, 0), ("초록", 0, 255, 80),
    ("시안", 0, 220, 255), ("파랑", 30, 0, 255), ("보라", 150, 0, 255), ("흰색", 255, 255, 255),
]


class AudioPanel(QWidget):
    audio_params_changed = Signal(dict)
    audio_min_brightness_changed = Signal(float)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._is_running = False
        self._current_color = (255, 0, 80)
        self._is_rainbow = True
        self._mode_key = "pulse"
        self._build_ui()
        self.load_from_config()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4); layout.setSpacing(6)

        # 에너지 레벨
        eg = QGroupBox("에너지 레벨"); el = QVBoxLayout(eg); gg = QGridLayout()
        self.bar_bass = self._make_bar(gg, 0, "Bass", "#e74c3c")
        self.bar_mid = self._make_bar(gg, 1, "Mid", "#27ae60")
        self.bar_high = self._make_bar(gg, 2, "High", "#3498db")
        el.addLayout(gg); el.addWidget(QLabel("스펙트럼 (16밴드)"))
        self.spectrum_widget = SpectrumWidget(n_bands=16); el.addWidget(self.spectrum_widget)
        layout.addWidget(eg)

        # 색상 팔레트
        cg = QGroupBox("색상"); cl = QVBoxLayout(cg); pg = QGridLayout()
        for i, (name, r, g, b) in enumerate(_COLOR_PRESETS):
            btn = QPushButton(name); btn.setMinimumHeight(26)
            if r is None:
                btn.setStyleSheet("background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);color:white;font-weight:bold;border-radius:4px;font-size:11px;")
                btn.clicked.connect(lambda _: self._set_rainbow())
            else:
                tc = "#000" if (r + g + b) > 380 else "#fff"
                btn.setStyleSheet(f"background:rgb({r},{g},{b});color:{tc};font-weight:bold;border-radius:4px;font-size:11px;")
                btn.clicked.connect(lambda _, rgb=(r, g, b): self._set_color(*rgb))
            pg.addWidget(btn, i // 5, i % 5)
        cl.addLayout(pg)
        cr = QHBoxLayout()
        btn_custom = QPushButton("커스텀"); btn_custom.clicked.connect(self._pick_custom_color); cr.addWidget(btn_custom)
        self.color_preview = QFrame(); self.color_preview.setFixedSize(40, 26); self._update_color_preview(); cr.addWidget(self.color_preview); cr.addStretch()
        cl.addLayout(cr)
        ambr = QHBoxLayout(); ambr.addWidget(QLabel("최소 밝기:"))
        self.slider_min_brightness = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_min_brightness.setRange(0, 100); self.slider_min_brightness.setValue(2)
        self.slider_min_brightness.valueChanged.connect(self._on_min_brightness); ambr.addWidget(self.slider_min_brightness)
        self.lbl_min_brightness = QLabel("2%"); self.lbl_min_brightness.setMinimumWidth(35)
        self.lbl_min_brightness.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter); ambr.addWidget(self.lbl_min_brightness)
        cl.addLayout(ambr); layout.addWidget(cg)

        # 비주얼라이저 모드
        mg = QGroupBox("비주얼라이저 모드"); ml = QVBoxLayout(mg)
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["Bass 반응 — 저음 기반 전체 밝기", "Spectrum — 16밴드 주파수 매핑", "Bass Detail — 저역 세밀 16밴드"])
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed); ml.addWidget(self.combo_mode); layout.addWidget(mg)

        # 파라미터
        prg = QGroupBox("파라미터"); pl = QVBoxLayout(prg)
        self.param_widget = AudioParamWidget(); self.param_widget.params_changed.connect(self._on_params_changed); pl.addWidget(self.param_widget)
        ht = QLabel("Attack ↑ = 빠르게 반응  |  Release ↑ = 긴 잔향"); ht.setStyleSheet("color:#888;font-size:10px;"); ht.setWordWrap(True); pl.addWidget(ht)
        layout.addWidget(prg)

    @staticmethod
    def _make_bar(grid, row, name, color):
        grid.addWidget(QLabel(name), row, 0)
        bar = QProgressBar(); bar.setRange(0, 100); bar.setTextVisible(False); bar.setFixedHeight(14)
        bar.setStyleSheet(f"QProgressBar{{background:#2b2b2b;border-radius:3px}}QProgressBar::chunk{{background:{color};border-radius:3px}}")
        grid.addWidget(bar, row, 1); return bar

    def _set_color(self, r, g, b):
        self._current_color = (r, g, b); self._is_rainbow = False; self._update_color_preview()
        if self._is_running: self.audio_params_changed.emit(self.collect_params())

    def _set_rainbow(self):
        self._is_rainbow = True; self._update_color_preview()
        if self._is_running: self.audio_params_changed.emit(self.collect_params())

    def _pick_custom_color(self):
        r, g, b = self._current_color
        c = QColorDialog.getColor(QColor(r, g, b), self, "기본 색상")
        if c.isValid(): self._set_color(c.red(), c.green(), c.blue())

    def _update_color_preview(self):
        if self._is_rainbow:
            self.color_preview.setStyleSheet("background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);border:1px solid #555;border-radius:4px;")
        else:
            r, g, b = self._current_color
            self.color_preview.setStyleSheet(f"background:rgb({r},{g},{b});border:1px solid #555;border-radius:4px;")

    def _on_mode_changed(self, idx):
        new_key = _INDEX_AUDIO_MODE.get(idx, "pulse")
        if new_key == self._mode_key: return
        self._save_mode_params(self._mode_key); self._load_mode_params(new_key)
        self.param_widget.set_audio_mode(new_key); self._mode_key = new_key
        if self._is_running: self.audio_params_changed.emit(self.collect_params())

    def _on_params_changed(self):
        if self._is_running: self.audio_params_changed.emit(self.collect_params())

    def _on_min_brightness(self, value):
        self.lbl_min_brightness.setText(f"{value}%")
        if self._is_running: self.audio_min_brightness_changed.emit(value / 100.0)

    def _save_mode_params(self, mode_name):
        d = self._config.setdefault(f"audio_{mode_name}", {}); self.param_widget.save_to_dict(d)

    def _load_mode_params(self, mode_name):
        df = AUDIO_DEFAULTS.get(mode_name, AUDIO_DEFAULTS["pulse"])
        d = self._config.get(f"audio_{mode_name}", df); self.param_widget.set_params(d, defaults=df)
        self.param_widget.set_audio_mode(mode_name)

    def set_running(self, running): self._is_running = running

    def collect_params(self):
        p = self.param_widget.get_params()
        return {"audio_mode": _INDEX_AUDIO_MODE.get(self.combo_mode.currentIndex(), "pulse"),
                "brightness": p["brightness"] / 100.0, "min_brightness": self.slider_min_brightness.value() / 100.0,
                "bass_sensitivity": p["bass_sens"] / 100.0, "mid_sensitivity": p["mid_sens"] / 100.0,
                "high_sensitivity": p["high_sens"] / 100.0, "attack": p["attack"] / 100.0, "release": p["release"] / 100.0,
                "zone_weights": (p["zone_bass"], p["zone_mid"], p["zone_high"]),
                "rainbow": self._is_rainbow, "base_color": self._current_color}

    def update_energy(self, bass, mid, high):
        self.bar_bass.setValue(int(bass * 100)); self.bar_mid.setValue(int(mid * 100)); self.bar_high.setValue(int(high * 100))

    def update_spectrum(self, spec): self.spectrum_widget.set_values(spec)

    def apply_to_config(self):
        self._save_mode_params(self._mode_key)
        opts = self._config.setdefault("options", {})
        opts["audio_state"] = {"sub_mode": self._mode_key, "color_rainbow": self._is_rainbow,
                               "color_rgb": list(self._current_color), "min_brightness": self.slider_min_brightness.value()}

    def load_from_config(self):
        state = self._config.get("options", {}).get("audio_state", {})
        saved_mode = state.get("sub_mode", "pulse")
        self.combo_mode.blockSignals(True); self.combo_mode.setCurrentIndex(_MODE_TO_INDEX.get(saved_mode, 0)); self.combo_mode.blockSignals(False)
        self._mode_key = saved_mode; self.param_widget.set_audio_mode(saved_mode)
        self._is_rainbow = state.get("color_rainbow", True)
        rgb = state.get("color_rgb", [255, 0, 80]); self._current_color = tuple(rgb) if isinstance(rgb, list) else (255, 0, 80)
        self._update_color_preview()
        min_b = state.get("min_brightness", 2)
        self.slider_min_brightness.blockSignals(True); self.slider_min_brightness.setValue(min_b); self.slider_min_brightness.blockSignals(False)
        self.lbl_min_brightness.setText(f"{min_b}%"); self._load_mode_params(self._mode_key)

    def cleanup(self): self._save_mode_params(self._mode_key)
