"""하이브리드 모드 패널 — 에너지 레벨 + 화면 연동 + 오디오 파라미터.

[ADR-040] AudioPanel ↔ HybridPanel 공통 섹션 마진/스페이싱 통일.
[ADR-041] 비주얼라이저 모드 + 파라미터를 "오디오 반응" 하나로 통합.
         콤보 항목·힌트 텍스트를 AudioPanel과 동일하게 유지.
[Phase 2] 색상 효과 콤보 추가 — 화면 연동 시에는 비활성화.
[Phase 3] 추출 방식 콤보 추가 — 구역 수 옆에 평균/Distinctive 선택.
         per-LED 모드에서는 Distinctive 비활성화.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QComboBox, QProgressBar, QGridLayout,
)
from PySide6.QtCore import Qt, Signal
from core.engine_utils import (
    COLOR_SOURCE_SCREEN, N_ZONES_PER_LED,
    COLOR_EFFECT_STATIC, COLOR_EFFECT_GRADIENT_CW,
    COLOR_EFFECT_GRADIENT_CCW, COLOR_EFFECT_RAINBOW_TIME,
)
from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.spectrum import SpectrumWidget
from ui.widgets.audio_param_widget import AudioParamWidget, AUDIO_DEFAULTS
from core.engine_utils import wave_speed_from_slider, gradient_speed_from_slider

_INDEX_AUDIO_MODE = {0: "pulse", 1: "spectrum", 2: "bass_detail", 3: "wave", 4: "dynamic", 5: "flowing"}
_MODE_TO_INDEX = {"pulse": 0, "spectrum": 1, "bass_detail": 2, "wave": 3, "dynamic": 4, "flowing": 5}
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
    "Flowing — 화면 색 흐름",          # ★ Phase 4
]

# ★ Phase 2: 색상 효과 콤보 (AudioPanel과 동일)
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


class HybridPanel(QWidget):
    hybrid_params_changed = Signal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config; self._is_running = False; self._mode_key = "pulse"
        self._color_effect = COLOR_EFFECT_STATIC  # ★ Phase 2
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
        self.combo_zone_count.currentIndexChanged.connect(self._on_zone_or_extract_changed); zcr.addWidget(self.combo_zone_count)
        # ★ Phase 3: 추출 방식 콤보
        zcr.addWidget(QLabel("  추출:"))
        self.combo_extract_mode = QComboBox()
        self.combo_extract_mode.addItem("평균", "average")
        self.combo_extract_mode.addItem("Distinctive", "distinctive")
        self.combo_extract_mode.currentIndexChanged.connect(self._on_zone_or_extract_changed)
        zcr.addWidget(self.combo_extract_mode)
        zcr.addStretch(); scl.addLayout(zcr)

        # ★ Phase 2: 색상 효과 콤보 (하이브리드에서는 화면 색 사용 시 비활성화 안내)
        effect_row = QHBoxLayout()
        effect_row.addWidget(QLabel("색상 효과:"))
        self.combo_color_effect = QComboBox()
        self.combo_color_effect.addItems(_COLOR_EFFECT_ITEMS)
        self.combo_color_effect.currentIndexChanged.connect(self._on_color_effect_changed)
        effect_row.addWidget(self.combo_color_effect)
        effect_row.addStretch()
        scl.addLayout(effect_row)

        # ★ 효과 속도 슬라이더
        self._effect_speed_row = QWidget()
        esr = QHBoxLayout(self._effect_speed_row)
        esr.setContentsMargins(0, 0, 0, 0)
        esr.addWidget(QLabel("효과 속도:"))
        self.slider_gradient_speed = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_speed.setRange(0, 100)
        self.slider_gradient_speed.setValue(50)
        self.slider_gradient_speed.valueChanged.connect(self._on_gradient_param_changed)
        esr.addWidget(self.slider_gradient_speed)
        self.lbl_gradient_speed = QLabel("50%")
        self.lbl_gradient_speed.setMinimumWidth(35)
        self.lbl_gradient_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        esr.addWidget(self.lbl_gradient_speed)
        scl.addWidget(self._effect_speed_row)
        self._effect_speed_row.setVisible(False)

        # ★ Hue shift 슬라이더
        self._effect_hue_row = QWidget()
        ehr = QHBoxLayout(self._effect_hue_row)
        ehr.setContentsMargins(0, 0, 0, 0)
        ehr.addWidget(QLabel("색조 변동:"))
        self.slider_gradient_hue = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_hue.setRange(0, 100)
        self.slider_gradient_hue.setValue(40)
        self.slider_gradient_hue.valueChanged.connect(self._on_gradient_param_changed)
        ehr.addWidget(self.slider_gradient_hue)
        self.lbl_gradient_hue = QLabel("40%")
        self.lbl_gradient_hue.setMinimumWidth(35)
        self.lbl_gradient_hue.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        ehr.addWidget(self.lbl_gradient_hue)
        scl.addWidget(self._effect_hue_row)
        self._effect_hue_row.setVisible(False)

        # ★ S/V 변동 범위 슬라이더
        self._effect_sv_row = QWidget()
        svr = QHBoxLayout(self._effect_sv_row)
        svr.setContentsMargins(0, 0, 0, 0)
        svr.addWidget(QLabel("밝기 변동:"))
        self.slider_gradient_sv = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_gradient_sv.setRange(0, 100)
        self.slider_gradient_sv.setValue(50)
        self.slider_gradient_sv.valueChanged.connect(self._on_gradient_param_changed)
        svr.addWidget(self.slider_gradient_sv)
        self.lbl_gradient_sv = QLabel("50%")
        self.lbl_gradient_sv.setMinimumWidth(35)
        self.lbl_gradient_sv.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        svr.addWidget(self.lbl_gradient_sv)
        scl.addWidget(self._effect_sv_row)
        self._effect_sv_row.setVisible(False)

        self.lbl_effect_note = QLabel("화면 색 사용 시 색상 효과가 무시됩니다")
        self.lbl_effect_note.setStyleSheet("color:#888;font-size:10px;font-style:italic;")
        scl.addWidget(self.lbl_effect_note)

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

        # ★ Phase 4: Flowing 전용 슬라이더
        from PySide6.QtWidgets import QFrame as _QFrame
        self._flowing_sep = _QFrame()
        self._flowing_sep.setFrameShape(_QFrame.Shape.HLine)
        self._flowing_sep.setFrameShadow(_QFrame.Shadow.Sunken)
        al.addWidget(self._flowing_sep)

        self._flowing_lbl = QLabel("Flowing 설정")
        self._flowing_lbl.setStyleSheet("font-weight:bold;")
        al.addWidget(self._flowing_lbl)

        # ★ Palette 프리뷰
        from ui.widgets.flow_palette_preview import FlowPalettePreview
        palette_row = QHBoxLayout()
        palette_row.addWidget(QLabel("현재 팔레트:"))
        self.flow_palette_preview = FlowPalettePreview(n_swatches=5)
        palette_row.addWidget(self.flow_palette_preview)
        al.addLayout(palette_row)

        # palette 갱신 주기
        fi_row = QHBoxLayout()
        fi_row.addWidget(QLabel("색상 갱신 주기:"))
        self.slider_flowing_interval = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_flowing_interval.setRange(10, 100)  # 1.0~10.0초 (×0.1)
        self.slider_flowing_interval.setValue(30)  # 3.0초
        self.slider_flowing_interval.valueChanged.connect(self._on_changed)
        fi_row.addWidget(self.slider_flowing_interval)
        self.lbl_flowing_interval = QLabel("3.0초")
        self.lbl_flowing_interval.setMinimumWidth(40)
        self.lbl_flowing_interval.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fi_row.addWidget(self.lbl_flowing_interval)
        al.addLayout(fi_row)

        # 흐름 속도
        fs_row = QHBoxLayout()
        fs_row.addWidget(QLabel("흐름 속도:"))
        self.slider_flowing_speed = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_flowing_speed.setRange(0, 100)
        self.slider_flowing_speed.setValue(50)
        self.slider_flowing_speed.valueChanged.connect(self._on_changed)
        fs_row.addWidget(self.slider_flowing_speed)
        self.lbl_flowing_speed = QLabel("50%")
        self.lbl_flowing_speed.setMinimumWidth(40)
        self.lbl_flowing_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fs_row.addWidget(self.lbl_flowing_speed)
        al.addLayout(fs_row)

        self._flowing_hint = QLabel("갱신 주기 ↑ = 안정적  |  속도 ↑ = 빠른 회전")
        self._flowing_hint.setStyleSheet("color:#888;font-size:10px;")
        al.addWidget(self._flowing_hint)

        # flowing 위젯 목록 (모드별 가시성 제어용)
        self._flowing_widgets = [
            self._flowing_sep, self._flowing_lbl,
            self._flowing_hint,
            self.flow_palette_preview,
        ]
        # 슬라이더 행은 layout에 포함되어 개별 hide 불가 → 위젯 단위 관리
        # 대신 전체 flowing 영역을 컨테이너로 감쌈
        self._flowing_container = QWidget()
        _fc_layout = QVBoxLayout(self._flowing_container)
        _fc_layout.setContentsMargins(0, 0, 0, 0)
        _fc_layout.setSpacing(3)
        # 이미 al에 추가된 위젯들을 직접 숨기는 방식 사용
        self._flowing_slider_widgets = [
            self.slider_flowing_interval, self.lbl_flowing_interval,
            self.slider_flowing_speed, self.lbl_flowing_speed,
        ]

        # 초기 가시성 설정 (flowing이 아니면 숨김)
        self._update_flowing_visibility(self._mode_key)

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

    # ★ Phase 2
    def _on_color_effect_changed(self, idx):
        self._color_effect = _INDEX_COLOR_EFFECT.get(idx, COLOR_EFFECT_STATIC)
        self._update_effect_sliders_visibility()
        if self._is_running: self.hybrid_params_changed.emit(self.collect_params())

    def _update_effect_sliders_visibility(self):
        is_static = self._color_effect == COLOR_EFFECT_STATIC
        is_gradient = self._color_effect in (COLOR_EFFECT_GRADIENT_CW, COLOR_EFFECT_GRADIENT_CCW)
        self._effect_speed_row.setVisible(not is_static)
        self._effect_hue_row.setVisible(is_gradient)
        self._effect_sv_row.setVisible(is_gradient)

    def _on_gradient_param_changed(self, value=None):
        self.lbl_gradient_speed.setText(f"{self.slider_gradient_speed.value()}%")
        self.lbl_gradient_hue.setText(f"{self.slider_gradient_hue.value()}%")
        self.lbl_gradient_sv.setText(f"{self.slider_gradient_sv.value()}%")
        if self._is_running: self.hybrid_params_changed.emit(self.collect_params())

    # ★ Phase 3: 구역 수 또는 추출 방식 변경
    def _on_zone_or_extract_changed(self, _=None):
        self._update_extract_mode_enabled()
        if self._is_running: self.hybrid_params_changed.emit(self.collect_params())

    def _update_extract_mode_enabled(self):
        """per-LED 모드에서는 distinctive가 의미 없으므로 콤보 비활성화."""
        n_zones = self.combo_zone_count.currentData()
        is_per_led = (n_zones == N_ZONES_PER_LED)
        self.combo_extract_mode.setEnabled(not is_per_led)
        if is_per_led:
            self.combo_extract_mode.blockSignals(True)
            self.combo_extract_mode.setCurrentIndex(0)  # 평균으로 리셋
            self.combo_extract_mode.blockSignals(False)

    def _on_mode_changed(self, idx):
        new_key = _INDEX_AUDIO_MODE.get(idx, "pulse")
        if new_key == self._mode_key: return
        self._save_mode_params(self._mode_key); self._load_mode_params(new_key)
        self.param_widget.set_audio_mode(new_key); self._mode_key = new_key
        self._update_flowing_visibility(new_key)  # ★ Phase 4
        if self._is_running: self.hybrid_params_changed.emit(self.collect_params())

    # ★ Phase 4: flowing 가시성 + 파라미터
    def _update_flowing_visibility(self, mode_key):
        """flowing 모드일 때만 전용 슬라이더 표시."""
        is_flowing = (mode_key == "flowing")
        for w in self._flowing_widgets:
            w.setVisible(is_flowing)
        for w in self._flowing_slider_widgets:
            w.setVisible(is_flowing)
        # flowing 모드에서 슬라이더 라벨 갱신
        if is_flowing:
            self._update_flowing_labels()

    def _update_flowing_labels(self):
        interval = self.slider_flowing_interval.value() / 10.0
        self.lbl_flowing_interval.setText(f"{interval:.1f}초")
        speed_pct = self.slider_flowing_speed.value()
        self.lbl_flowing_speed.setText(f"{speed_pct}%")

    def _get_flowing_interval(self):
        """슬라이더 값 → 실제 갱신 주기 (초)."""
        return self.slider_flowing_interval.value() / 10.0

    def _get_flowing_speed(self):
        """슬라이더 값(0~100) → 실제 회전 속도.

        0% → 0.02 (매우 느림)
        50% → 0.08 (기본)
        100% → 0.20 (빠름)
        """
        t = self.slider_flowing_speed.value() / 100.0
        return 0.02 + t * 0.18

    def _on_changed(self, _=None):
        if self._mode_key == "flowing":
            self._update_flowing_labels()  # ★ Phase 4
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
        return {
            "audio_mode": _INDEX_AUDIO_MODE.get(self.combo_mode.currentIndex(), "pulse"),
            "color_source": COLOR_SOURCE_SCREEN,
            "n_zones": self.combo_zone_count.currentData() or 4,
            "min_brightness": self.slider_min_brightness.value() / 100.0,
            "brightness": p["brightness"] / 100.0,
            "bass_sensitivity": p["bass_sens"] / 100.0,
            "mid_sensitivity": p["mid_sens"] / 100.0,
            "high_sensitivity": p["high_sens"] / 100.0,
            "attack": p["attack"] / 100.0,
            "release": p["release"] / 100.0,
            "wave_speed": wave_speed_from_slider(p["wave_speed"]),
            "zone_weights": (p["zone_bass"], p["zone_mid"], p["zone_high"]),
            # ★ Phase 2
            "color_effect": self._color_effect,
            "gradient_speed": gradient_speed_from_slider(self.slider_gradient_speed.value()),
            "gradient_hue_range": self.slider_gradient_hue.value() / 100.0 * 0.20,
            "gradient_sv_range": self.slider_gradient_sv.value() / 100.0,
            # ★ Phase 3
            "color_extract_mode": self.combo_extract_mode.currentData() or "average",
            # ★ Phase 4: flowing 파라미터
            "flowing_interval": self._get_flowing_interval(),
            "flowing_speed": self._get_flowing_speed(),
        }

    def update_energy(self, bass, mid, high):
        self.bar_bass.setValue(int(bass * 100)); self.bar_mid.setValue(int(mid * 100)); self.bar_high.setValue(int(high * 100))
    def update_spectrum(self, spec): self.spectrum_widget.set_values(spec)

    def update_flow_palette(self, colors, ratios=None):
        """Flowing palette 프리뷰 갱신 — 엔진에서 호출.

        Args:
            colors: list/array of (R, G, B) — 현재 blob 색상들
            ratios: list of float — 면적 비율 (선택)
        """
        if self.flow_palette_preview.isVisible():
            self.flow_palette_preview.set_colors(colors, ratios)

    def apply_to_config(self):
        self._save_mode_params(self._mode_key)
        opts = self._config.setdefault("options", {})
        opts["hybrid_state"] = {
            "sub_mode": self._mode_key,
            "zone_count": self.combo_zone_count.currentData() or 4,
            "min_brightness": self.slider_min_brightness.value(),
            "color_effect": self._color_effect,  # ★ Phase 2
            "gradient_speed": self.slider_gradient_speed.value(),  # ★ 속도 슬라이더
            "gradient_hue": self.slider_gradient_hue.value(),      # ★ hue shift
            "gradient_sv": self.slider_gradient_sv.value(),        # ★ S/V 범위
            "color_extract_mode": self.combo_extract_mode.currentData() or "average",  # ★ Phase 3
            "flowing_interval": self.slider_flowing_interval.value(),  # ★ Phase 4
            "flowing_speed": self.slider_flowing_speed.value(),        # ★ Phase 4
        }

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

        # ★ Phase 2: 색상 효과 복원
        self._color_effect = state.get("color_effect", COLOR_EFFECT_STATIC)
        effect_idx = _COLOR_EFFECT_TO_INDEX.get(self._color_effect, 0)
        self.combo_color_effect.blockSignals(True)
        self.combo_color_effect.setCurrentIndex(effect_idx)
        self.combo_color_effect.blockSignals(False)

        # ★ 속도 슬라이더 복원
        saved_speed = state.get("gradient_speed", 50)
        self.slider_gradient_speed.blockSignals(True)
        self.slider_gradient_speed.setValue(saved_speed)
        self.slider_gradient_speed.blockSignals(False)
        self.lbl_gradient_speed.setText(f"{saved_speed}%")

        # ★ hue/sv 슬라이더 복원
        saved_hue = state.get("gradient_hue", 40)
        self.slider_gradient_hue.blockSignals(True)
        self.slider_gradient_hue.setValue(saved_hue)
        self.slider_gradient_hue.blockSignals(False)
        self.lbl_gradient_hue.setText(f"{saved_hue}%")

        saved_sv = state.get("gradient_sv", 50)
        self.slider_gradient_sv.blockSignals(True)
        self.slider_gradient_sv.setValue(saved_sv)
        self.slider_gradient_sv.blockSignals(False)
        self.lbl_gradient_sv.setText(f"{saved_sv}%")

        self._update_effect_sliders_visibility()

        # ★ Phase 3: 추출 방식 복원
        saved_extract = state.get("color_extract_mode", "average")
        self.combo_extract_mode.blockSignals(True)
        for i in range(self.combo_extract_mode.count()):
            if self.combo_extract_mode.itemData(i) == saved_extract:
                self.combo_extract_mode.setCurrentIndex(i); break
        self.combo_extract_mode.blockSignals(False)
        self._update_extract_mode_enabled()

        # ★ Phase 4: flowing 슬라이더 복원
        self.slider_flowing_interval.blockSignals(True)
        self.slider_flowing_interval.setValue(state.get("flowing_interval", 30))
        self.slider_flowing_interval.blockSignals(False)
        self.slider_flowing_speed.blockSignals(True)
        self.slider_flowing_speed.setValue(state.get("flowing_speed", 50))
        self.slider_flowing_speed.blockSignals(False)
        self._update_flowing_visibility(self._mode_key)

    def cleanup(self): self._save_mode_params(self._mode_key)