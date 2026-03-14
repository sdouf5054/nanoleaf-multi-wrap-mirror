"""하이브리드 모드 패널 — 에너지 레벨 + 화면 연동 + 오디오 파라미터.

[ADR-040] AudioPanel ↔ HybridPanel 공통 섹션 마진/스페이싱 통일.
[ADR-041] 비주얼라이저 모드 + 파라미터를 "오디오 반응" 하나로 통합.
         콤보 항목·힌트 텍스트를 AudioPanel과 동일하게 유지.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QComboBox, QProgressBar, QGridLayout,
)
from PySide6.QtCore import Qt, Signal
from core.engine_utils import COLOR_SOURCE_SCREEN, N_ZONES_PER_LED
from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.spectrum import SpectrumWidget
from ui.widgets.audio_param_widget import AudioParamWidget, AUDIO_DEFAULTS
from core.engine_utils import wave_speed_from_slider

_INDEX_AUDIO_MODE = {0: "pulse", 1: "spectrum", 2: "bass_detail", 3: "wave", 4: "dynamic"}
_MODE_TO_INDEX = {"pulse": 0, "spectrum": 1, "bass_detail": 2, "wave": 3, "dynamic": 4}
_ZONE_OPTIONS = [
    (1, "1구역 (화면 전체 평균)"), (2, "2구역 (상/하)"),
    (4, "4구역 (상하좌우)"), (8, "8구역 (모서리 포함)"),
    (16, "16구역"), (32, "32구역"), (N_ZONES_PER_LED, "LED별 개별 (미러링)"),
]

# ── [ADR-040] 공통 레이아웃 상수 (AudioPanel과 동일) ──
_PANEL_MARGINS = (0, 2, 0, 2)
_PANEL_SPACING = 4
_GROUP_MARGINS = (6, 16, 6, 4)
_GROUP_SPACING = 3

# ── [ADR-041] 비주얼라이저 모드 콤보 항목 (AudioPanel과 동일) ──
_VISUALIZER_MODE_ITEMS = [
    "Bass 반응 — 저음 기반 전체 밝기",
    "Spectrum — 16밴드 주파수 매핑",
    "Bass Detail — 저역 세밀 16밴드",
    "Wave — 베이스 펄스 아래→위",
    "Dynamic — 비트 반응 파원 효과",
]


class HybridPanel(QWidget):
    hybrid_params_changed = Signal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config; self._is_running = False; self._mode_key = "pulse"
        self._build_ui(); self.load_from_config()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*_PANEL_MARGINS)
        layout.setSpacing(_PANEL_SPACING)

        # ── 에너지 레벨 — AudioPanel과 동일한 구성 ──
        eg = QGroupBox("에너지 레벨")
        hel = QVBoxLayout(eg)
        hel.setSpacing(_GROUP_SPACING)
        hel.setContentsMargins(*_GROUP_MARGINS)
        heg = QGridLayout()
        self.bar_bass = self._make_bar(heg, 0, "Bass", "#e74c3c")
        self.bar_mid = self._make_bar(heg, 1, "Mid", "#27ae60")
        self.bar_high = self._make_bar(heg, 2, "High", "#3498db")
        hel.addLayout(heg)
        hel.addWidget(QLabel("스펙트럼 (16밴드)"))
        self.spectrum_widget = SpectrumWidget(16)
        hel.addWidget(self.spectrum_widget)
        layout.addWidget(eg)

        # ── 화면 연동 ──
        sg = QGroupBox("화면 연동")
        scl = QVBoxLayout(sg)
        scl.setSpacing(_GROUP_SPACING)
        scl.setContentsMargins(*_GROUP_MARGINS)
        zcr = QHBoxLayout(); zcr.addWidget(QLabel("구역 수:"))
        self.combo_zone_count = QComboBox()
        for n, label in _ZONE_OPTIONS: self.combo_zone_count.addItem(label, n)
        self.combo_zone_count.currentIndexChanged.connect(self._on_changed); zcr.addWidget(self.combo_zone_count); zcr.addStretch(); scl.addLayout(zcr)
        mbr = QHBoxLayout(); mbr.addWidget(QLabel("최소 밝기:"))
        self.slider_min_brightness = NoScrollSlider(Qt.Orientation.Horizontal); self.slider_min_brightness.setRange(0, 100); self.slider_min_brightness.setValue(5)
        self.slider_min_brightness.valueChanged.connect(self._on_min_brightness); mbr.addWidget(self.slider_min_brightness)
        self.lbl_min_brightness = QLabel("5%"); self.lbl_min_brightness.setMinimumWidth(35)
        self.lbl_min_brightness.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter); mbr.addWidget(self.lbl_min_brightness)
        scl.addLayout(mbr); layout.addWidget(sg)

        # ── 오디오 반응 (비주얼라이저 모드 + 파라미터 통합) ──
        ag = QGroupBox("오디오 반응")
        al = QVBoxLayout(ag)
        al.setSpacing(_GROUP_SPACING)
        al.setContentsMargins(*_GROUP_MARGINS)

        # 비주얼라이저 모드
        mr = QHBoxLayout()
        mr.addWidget(QLabel("모드:"))
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(_VISUALIZER_MODE_ITEMS)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        mr.addWidget(self.combo_mode)
        mr.addStretch()
        al.addLayout(mr)

        # 파라미터
        self.param_widget = AudioParamWidget()
        self.param_widget.params_changed.connect(self._on_changed)
        al.addWidget(self.param_widget)

        # 힌트 (AudioPanel과 동일)
        ht = QLabel("Attack ↑ = 빠르게 반응  |  Release ↑ = 긴 잔향")
        ht.setStyleSheet("color:#888;font-size:10px;")
        ht.setWordWrap(True)
        al.addWidget(ht)

        layout.addWidget(ag)

    @staticmethod
    def _make_bar(grid, row, name, color):
        grid.addWidget(QLabel(name), row, 0)
        bar = QProgressBar(); bar.setRange(0, 100); bar.setTextVisible(False); bar.setFixedHeight(14)
        bar.setStyleSheet(f"QProgressBar{{background:#2b2b2b;border-radius:3px}}QProgressBar::chunk{{background:{color};border-radius:3px}}")
        grid.addWidget(bar, row, 1); return bar

    def _on_mode_changed(self, idx):
        new_key = _INDEX_AUDIO_MODE.get(idx, "pulse")
        if new_key == self._mode_key: return
        self._save_mode_params(self._mode_key); self._load_mode_params(new_key)
        self.param_widget.set_audio_mode(new_key); self._mode_key = new_key
        if self._is_running: self.hybrid_params_changed.emit(self.collect_params())

    def _on_changed(self, _=None):
        if self._is_running: self.hybrid_params_changed.emit(self.collect_params())

    def _on_min_brightness(self, value):
        self.lbl_min_brightness.setText(f"{value}%")
        if self._is_running: self.hybrid_params_changed.emit(self.collect_params())

    def _save_mode_params(self, mode_name):
        d = self._config.setdefault(f"audio_{mode_name}", {}); self.param_widget.save_to_dict(d)

    def _load_mode_params(self, mode_name):
        df = AUDIO_DEFAULTS.get(mode_name, AUDIO_DEFAULTS["pulse"])
        d = self._config.get(f"audio_{mode_name}", df); self.param_widget.set_params(d, defaults=df); self.param_widget.set_audio_mode(mode_name)

    def set_running(self, running): self._is_running = running

    def collect_params(self):
        p = self.param_widget.get_params()
        return {"audio_mode": _INDEX_AUDIO_MODE.get(self.combo_mode.currentIndex(), "pulse"),
                "color_source": COLOR_SOURCE_SCREEN, "n_zones": self.combo_zone_count.currentData() or 4,
                "min_brightness": self.slider_min_brightness.value() / 100.0,
                "brightness": p["brightness"] / 100.0, "bass_sensitivity": p["bass_sens"] / 100.0,
                "mid_sensitivity": p["mid_sens"] / 100.0, "high_sensitivity": p["high_sens"] / 100.0,
                "attack": p["attack"] / 100.0, "release": p["release"] / 100.0,
                "wave_speed": wave_speed_from_slider(p["wave_speed"]),
                "zone_weights": (p["zone_bass"], p["zone_mid"], p["zone_high"])}

    def update_energy(self, bass, mid, high):
        self.bar_bass.setValue(int(bass * 100)); self.bar_mid.setValue(int(mid * 100)); self.bar_high.setValue(int(high * 100))
    def update_spectrum(self, spec): self.spectrum_widget.set_values(spec)

    def apply_to_config(self):
        self._save_mode_params(self._mode_key)
        opts = self._config.setdefault("options", {})
        opts["hybrid_state"] = {"sub_mode": self._mode_key, "zone_count": self.combo_zone_count.currentData() or 4,
                                "min_brightness": self.slider_min_brightness.value()}

    def load_from_config(self):
        state = self._config.get("options", {}).get("hybrid_state", {})
        saved_mode = state.get("sub_mode", "pulse")
        self.combo_mode.blockSignals(True); self.combo_mode.setCurrentIndex(_MODE_TO_INDEX.get(saved_mode, 0)); self.combo_mode.blockSignals(False)
        self._mode_key = saved_mode; self.param_widget.set_audio_mode(saved_mode)
        saved_zone = state.get("zone_count", 4)
        self.combo_zone_count.blockSignals(True)
        for i in range(self.combo_zone_count.count()):
            if self.combo_zone_count.itemData(i) == saved_zone: self.combo_zone_count.setCurrentIndex(i); break
        self.combo_zone_count.blockSignals(False)
        min_b = state.get("min_brightness", 5)
        self.slider_min_brightness.blockSignals(True); self.slider_min_brightness.setValue(min_b); self.slider_min_brightness.blockSignals(False)
        self.lbl_min_brightness.setText(f"{min_b}%"); self._load_mode_params(self._mode_key)

    def cleanup(self): self._save_mode_params(self._mode_key)