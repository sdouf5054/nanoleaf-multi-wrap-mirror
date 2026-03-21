"""오디오 반응 설정 패널 — 에너지 레벨 + 모드 + 파라미터 통합 (Phase 3)

기존 AudioPanel + HybridPanel의 오디오 관련 부분을 하나로 통합.
오디오 토글 ON일 때 표시.

[구성]
1. 에너지 레벨: bass/mid/high 바 + 16밴드 스펙트럼
2. 오디오 모드 콤보: pulse/spectrum/bass_detail/wave/dynamic/flowing
   - flowing은 디스플레이 ON일 때만 활성
3. 오디오 공통 설정 (모드별 독립 저장/로드):
   - 최소 밝기
   - 감도 (bass / mid / high — bass-only 모드에서 mid/high 비활성)
   - Attack / Release
4. 모드 세부 설정 (모드별 조건부 표시):
   - Wave: wave 속도
   - Spectrum/BassDetail: 대역 비율
   - Flowing: 팔레트 프리뷰만 (갱신 주기/흐름 속도는 미러링 패널로 이동)

[★ Mirror Flowing 변경]
- Flowing 설정(갱신 주기, 흐름 속도)을 DisplayMirrorSection으로 이동
  → 미러 flowing + 하이브리드 오디오 flowing 공용
- 이 패널에는 팔레트 프리뷰만 유지
- collect_params()에서 flowing_interval/flowing_speed 제거
  → EngineParams 빌드 시 미러링 섹션의 display_params에서 가져옴
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QComboBox, QFrame, QProgressBar, QGridLayout, QPushButton,
)
from PySide6.QtCore import Qt, Signal, QTimer

from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.spectrum import SpectrumWidget
from ui.widgets.zone_balance import ZoneBalanceWidget
from core.engine_utils import wave_speed_from_slider
from core.config import save_config

# ── 오디오 모드 ──
_AUDIO_MODE_ITEMS = [
    ("pulse",       "Bass 반응 — 전체 밝기"),
    ("spectrum",    "Spectrum — 16밴드 주파수 매핑"),
    ("bass_detail", "Bass Detail — 저역 세밀 16밴드"),
    ("wave",        "Wave — 베이스 펄스 아래→위"),
    ("dynamic",     "Dynamic — 비트 반응 파원 효과"),
    ("flowing",     "Flowing — 화면 색 흐름"),
]
_MODE_KEYS = [item[0] for item in _AUDIO_MODE_ITEMS]
_INDEX_TO_MODE = {i: k for i, (k, _) in enumerate(_AUDIO_MODE_ITEMS)}
_MODE_TO_INDEX = {k: i for i, (k, _) in enumerate(_AUDIO_MODE_ITEMS)}

_BASS_ONLY_MODES = {"pulse", "wave", "dynamic"}
_BANDED_MODES = {"spectrum", "bass_detail"}

# ── 모드별 기본값 ──
_AUDIO_DEFAULTS = {
    "pulse":       {"min_brightness": 5, "bass_sens": 100, "mid_sens": 100, "high_sens": 100, "attack": 50, "release": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "spectrum":    {"min_brightness": 5, "bass_sens": 100, "mid_sens": 100, "high_sens": 100, "attack": 50, "release": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "bass_detail": {"min_brightness": 5, "bass_sens": 100, "mid_sens": 100, "high_sens": 100, "attack": 10, "release": 70, "zone_bass": 48, "zone_mid": 26, "zone_high": 26},
    "wave":        {"min_brightness": 5, "bass_sens": 120, "mid_sens": 100, "high_sens": 100, "attack": 60, "release": 40, "wave_speed": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "dynamic":     {"min_brightness": 5, "bass_sens": 110, "mid_sens": 110, "high_sens": 120, "attack": 55, "release": 45, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "flowing":     {"min_brightness": 5, "bass_sens": 100, "mid_sens": 100, "high_sens": 100, "attack": 40, "release": 60, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
}

_GROUP_MARGINS = (6, 6, 6, 8)
_GROUP_SPACING = 6


class AudioReactiveSection(QWidget):
    """오디오 반응 설정 패널."""

    params_changed = Signal()
    audio_mode_changed = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._mode_key = "pulse"
        self._display_enabled = False
        self._updating = False
        self._build_ui()
        self.load_from_config()

    # ══════════════════════════════════════════════════════════════
    #  UI 빌드
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._build_energy_section(layout)
        self._build_audio_settings(layout)

    def _build_energy_section(self, parent_layout):
        grp = QGroupBox("에너지 레벨")
        gl = QVBoxLayout(grp)
        gl.setSpacing(_GROUP_SPACING)
        gl.setContentsMargins(*_GROUP_MARGINS)

        grid = QGridLayout()
        self.bar_bass = self._make_bar(grid, 0, "Bass", "barBass")
        self.bar_mid = self._make_bar(grid, 1, "Mid", "barMid")
        self.bar_high = self._make_bar(grid, 2, "High", "barHigh")
        gl.addLayout(grid)

        gl.addWidget(QLabel("스펙트럼 (16밴드)"))
        self.spectrum_widget = SpectrumWidget(n_bands=16)
        gl.addWidget(self.spectrum_widget)

        parent_layout.addWidget(grp)

    def _build_audio_settings(self, parent_layout):
        grp = QGroupBox("오디오 반응 설정")
        gl = QVBoxLayout(grp)
        gl.setSpacing(_GROUP_SPACING)
        gl.setContentsMargins(*_GROUP_MARGINS)

        # ── 모드 콤보 ──
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("모드:"))
        self.combo_audio_mode = QComboBox()
        for key, label in _AUDIO_MODE_ITEMS:
            self.combo_audio_mode.addItem(label, key)
        self.combo_audio_mode.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.combo_audio_mode, 1)
        gl.addLayout(mode_row)

        gl.addWidget(self._make_sep())

        # ── 공통 설정 헤더 ──
        lbl_common = QLabel("오디오 공통 설정")
        lbl_common.setProperty("role", "sectionHeader")
        gl.addWidget(lbl_common)

        # 최소 밝기
        min_b_row = QHBoxLayout()
        min_b_row.addWidget(QLabel("최소 밝기:"))
        self.slider_min_brightness = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_min_brightness.setRange(0, 100)
        self.slider_min_brightness.setValue(5)
        self.slider_min_brightness.valueChanged.connect(self._on_param_changed)
        min_b_row.addWidget(self.slider_min_brightness)
        self.lbl_min_brightness = QLabel("5%")
        self.lbl_min_brightness.setMinimumWidth(35)
        self.lbl_min_brightness.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        min_b_row.addWidget(self.lbl_min_brightness)
        gl.addLayout(min_b_row)

        # 감도 — bass
        self.lbl_sens_header = QLabel("감도 (Bass)")
        gl.addWidget(self.lbl_sens_header)
        bass_row = QHBoxLayout()
        bass_row.addWidget(QLabel("Bass:"))
        self.slider_bass_sens = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_bass_sens.setRange(10, 300)
        self.slider_bass_sens.setValue(100)
        self.slider_bass_sens.valueChanged.connect(self._on_param_changed)
        bass_row.addWidget(self.slider_bass_sens)
        self.lbl_bass_sens = QLabel("1.00")
        self.lbl_bass_sens.setMinimumWidth(40)
        self.lbl_bass_sens.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bass_row.addWidget(self.lbl_bass_sens)
        gl.addLayout(bass_row)

        # 감도 — mid
        self._row_mid_sens = QWidget()
        rm = QHBoxLayout(self._row_mid_sens)
        rm.setContentsMargins(0, 0, 0, 0)
        rm.addWidget(QLabel("Mid:"))
        self.slider_mid_sens = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_mid_sens.setRange(10, 300)
        self.slider_mid_sens.setValue(100)
        self.slider_mid_sens.valueChanged.connect(self._on_param_changed)
        rm.addWidget(self.slider_mid_sens)
        self.lbl_mid_sens = QLabel("1.00")
        self.lbl_mid_sens.setMinimumWidth(40)
        self.lbl_mid_sens.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rm.addWidget(self.lbl_mid_sens)
        gl.addWidget(self._row_mid_sens)

        # 감도 — high
        self._row_high_sens = QWidget()
        rh = QHBoxLayout(self._row_high_sens)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(QLabel("High:"))
        self.slider_high_sens = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_high_sens.setRange(10, 300)
        self.slider_high_sens.setValue(100)
        self.slider_high_sens.valueChanged.connect(self._on_param_changed)
        rh.addWidget(self.slider_high_sens)
        self.lbl_high_sens = QLabel("1.00")
        self.lbl_high_sens.setMinimumWidth(40)
        self.lbl_high_sens.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rh.addWidget(self.lbl_high_sens)
        gl.addWidget(self._row_high_sens)

        # Attack / Release
        atk_row = QHBoxLayout()
        atk_row.addWidget(QLabel("Attack:"))
        self.slider_attack = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_attack.setRange(0, 100)
        self.slider_attack.setValue(50)
        self.slider_attack.valueChanged.connect(self._on_param_changed)
        atk_row.addWidget(self.slider_attack)
        self.lbl_attack = QLabel("0.50")
        self.lbl_attack.setMinimumWidth(40)
        self.lbl_attack.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        atk_row.addWidget(self.lbl_attack)
        gl.addLayout(atk_row)

        rel_row = QHBoxLayout()
        rel_row.addWidget(QLabel("Release:"))
        self.slider_release = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_release.setRange(0, 100)
        self.slider_release.setValue(50)
        self.slider_release.valueChanged.connect(self._on_param_changed)
        rel_row.addWidget(self.slider_release)
        self.lbl_release = QLabel("0.50")
        self.lbl_release.setMinimumWidth(40)
        self.lbl_release.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rel_row.addWidget(self.lbl_release)
        gl.addLayout(rel_row)

        hint = QLabel("Attack ↑ = 빠르게 반응 · Release ↑ = 긴 잔향")
        hint.setProperty("role", "hint")
        gl.addWidget(hint)

        gl.addWidget(self._make_sep())

        self._build_mode_specific(gl)

        parent_layout.addWidget(grp)

    def _build_mode_specific(self, parent_layout):
        # ── Wave 속도 ──
        self._wave_container = QWidget()
        wl = QVBoxLayout(self._wave_container)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(3)
        lbl_wave = QLabel("Wave 설정")
        lbl_wave.setProperty("role", "sectionHeader")
        wl.addWidget(lbl_wave)
        ws_row = QHBoxLayout()
        ws_row.addWidget(QLabel("Wave 속도:"))
        self.slider_wave_speed = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_wave_speed.setRange(0, 100)
        self.slider_wave_speed.setValue(50)
        self.slider_wave_speed.valueChanged.connect(self._on_param_changed)
        ws_row.addWidget(self.slider_wave_speed)
        self.lbl_wave_speed = QLabel("50%")
        self.lbl_wave_speed.setMinimumWidth(40)
        self.lbl_wave_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        ws_row.addWidget(self.lbl_wave_speed)
        wl.addLayout(ws_row)
        hint_w = QLabel("0% = 느린 연출 · 100% = 빠른 비트")
        hint_w.setProperty("role", "hint")
        wl.addWidget(hint_w)
        parent_layout.addWidget(self._wave_container)
        self._wave_container.setVisible(False)

        # ── 대역 비율 ──
        self._zone_container = QWidget()
        zl = QVBoxLayout(self._zone_container)
        zl.setContentsMargins(0, 0, 0, 0)
        zl.setSpacing(3)
        lbl_zone = QLabel("대역 비율")
        lbl_zone.setProperty("role", "sectionHeader")
        zl.addWidget(lbl_zone)
        self.zone_balance = ZoneBalanceWidget(33, 33, 34)
        self.zone_balance.zone_changed.connect(lambda *_: self._on_param_changed())
        zl.addWidget(self.zone_balance)
        parent_layout.addWidget(self._zone_container)
        self._zone_container.setVisible(False)

        # ── ★ Flowing — 힌트만 (설정+팔레트는 미러링 패널로 이동) ──
        self._flowing_container = QWidget()
        fl = QVBoxLayout(self._flowing_container)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(3)

        hint_f = QLabel("Flowing 설정 · 팔레트 프리뷰는 미러링 패널에 있습니다")
        hint_f.setProperty("role", "hint")
        fl.addWidget(hint_f)
        parent_layout.addWidget(self._flowing_container)
        self._flowing_container.setVisible(False)

    # ══════════════════════════════════════════════════════════════
    #  이벤트
    # ══════════════════════════════════════════════════════════════

    def _on_mode_changed(self, idx):
        new_key = _INDEX_TO_MODE.get(idx, "pulse")
        if new_key == self._mode_key:
            return
        self._save_mode_params(self._mode_key)
        self._mode_key = new_key
        self._load_mode_params(new_key)
        self._update_mode_visibility()
        if not self._updating:
            self.audio_mode_changed.emit(new_key)
            self.params_changed.emit()

    def _on_param_changed(self, _=None):
        if self._updating:
            return
        self._update_labels()
        self.params_changed.emit()

    def _update_labels(self):
        self.lbl_min_brightness.setText(f"{self.slider_min_brightness.value()}%")
        self.lbl_bass_sens.setText(f"{self.slider_bass_sens.value() / 100:.2f}")
        self.lbl_mid_sens.setText(f"{self.slider_mid_sens.value() / 100:.2f}")
        self.lbl_high_sens.setText(f"{self.slider_high_sens.value() / 100:.2f}")
        self.lbl_attack.setText(f"{self.slider_attack.value() / 100:.2f}")
        self.lbl_release.setText(f"{self.slider_release.value() / 100:.2f}")
        self.lbl_wave_speed.setText(f"{self.slider_wave_speed.value()}%")

    def _update_mode_visibility(self):
        mode = self._mode_key
        is_bass_only = mode in _BASS_ONLY_MODES
        is_banded = mode in _BANDED_MODES
        is_wave = mode == "wave"
        is_flowing = mode == "flowing"

        self._row_mid_sens.setEnabled(not is_bass_only)
        self._row_high_sens.setEnabled(not is_bass_only)

        header_map = {
            "pulse": "감도 (Bass)", "wave": "감도 (Bass → Wave)",
            "dynamic": "감도 (Dynamic)", "spectrum": "감도 (대역별)",
            "bass_detail": "감도 (Bass Detail)", "flowing": "감도 (Flowing)",
        }
        self.lbl_sens_header.setText(header_map.get(mode, "감도"))

        self._wave_container.setVisible(is_wave)
        self._zone_container.setVisible(is_banded)
        self._flowing_container.setVisible(is_flowing)

    # ══════════════════════════════════════════════════════════════
    #  디스플레이 토글 연동
    # ══════════════════════════════════════════════════════════════

    def set_display_enabled(self, enabled):
        self._display_enabled = enabled
        flowing_idx = _MODE_TO_INDEX.get("flowing", 5)
        model = self.combo_audio_mode.model()
        item = model.item(flowing_idx)
        if item:
            if enabled:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                if self._mode_key == "flowing":
                    self.combo_audio_mode.setCurrentIndex(0)

    # ══════════════════════════════════════════════════════════════
    #  에너지 / 스펙트럼 갱신
    # ══════════════════════════════════════════════════════════════

    def update_energy(self, bass, mid, high):
        self.bar_bass.setValue(int(bass * 100))
        self.bar_mid.setValue(int(mid * 100))
        self.bar_high.setValue(int(high * 100))

    def update_spectrum(self, spec):
        self.spectrum_widget.set_values(spec)

    # ══════════════════════════════════════════════════════════════
    #  모드별 파라미터 저장/로드
    # ══════════════════════════════════════════════════════════════

    def _save_mode_params(self, mode_name):
        d = self._config.setdefault(f"audio_{mode_name}", {})
        d["min_brightness"] = self.slider_min_brightness.value()
        d["bass_sens"] = self.slider_bass_sens.value()
        d["mid_sens"] = self.slider_mid_sens.value()
        d["high_sens"] = self.slider_high_sens.value()
        d["attack"] = self.slider_attack.value()
        d["release"] = self.slider_release.value()
        zb, zm, zh = self.zone_balance.get_values()
        d["zone_bass"] = zb
        d["zone_mid"] = zm
        d["zone_high"] = zh
        if mode_name == "wave":
            d["wave_speed"] = self.slider_wave_speed.value()

    def _load_mode_params(self, mode_name):
        self._updating = True
        df = _AUDIO_DEFAULTS.get(mode_name, _AUDIO_DEFAULTS["pulse"])
        d = self._config.get(f"audio_{mode_name}", df)

        self.slider_min_brightness.setValue(d.get("min_brightness", df["min_brightness"]))
        self.slider_bass_sens.setValue(d.get("bass_sens", df["bass_sens"]))
        self.slider_mid_sens.setValue(d.get("mid_sens", df["mid_sens"]))
        self.slider_high_sens.setValue(d.get("high_sens", df["high_sens"]))
        self.slider_attack.setValue(d.get("attack", df["attack"]))
        self.slider_release.setValue(d.get("release", df["release"]))
        self.zone_balance.set_values(
            d.get("zone_bass", df["zone_bass"]),
            d.get("zone_mid", df["zone_mid"]),
            d.get("zone_high", df["zone_high"]),
        )
        if mode_name == "wave":
            self.slider_wave_speed.setValue(d.get("wave_speed", df.get("wave_speed", 50)))

        self._update_labels()
        self._updating = False

    # ══════════════════════════════════════════════════════════════
    #  프리셋 수집/적용
    # ══════════════════════════════════════════════════════════════

    def collect_for_preset(self):
        """★ flowing_interval/flowing_speed 제거 — 미러링 섹션에서 수집."""
        zb, zm, zh = self.zone_balance.get_values()
        return {
            "audio_mode": self._mode_key,
            "min_brightness": self.slider_min_brightness.value(),
            "bass_sensitivity": self.slider_bass_sens.value(),
            "mid_sensitivity": self.slider_mid_sens.value(),
            "high_sensitivity": self.slider_high_sens.value(),
            "attack": self.slider_attack.value(),
            "release": self.slider_release.value(),
            "zone_weights": [zb, zm, zh],
            "wave_speed": self.slider_wave_speed.value(),
        }

    def apply_from_preset(self, data):
        """★ flowing_interval/flowing_speed 제거 — 미러링 섹션에서 적용."""
        self._updating = True

        if "audio_mode" in data:
            new_mode = data["audio_mode"]
            self._save_mode_params(self._mode_key)
            self._mode_key = new_mode
            self.combo_audio_mode.blockSignals(True)
            self.combo_audio_mode.setCurrentIndex(_MODE_TO_INDEX.get(new_mode, 0))
            self.combo_audio_mode.blockSignals(False)

        if "min_brightness" in data:
            self.slider_min_brightness.blockSignals(True)
            self.slider_min_brightness.setValue(int(data["min_brightness"]))
            self.slider_min_brightness.blockSignals(False)

        if "bass_sensitivity" in data:
            self.slider_bass_sens.blockSignals(True)
            self.slider_bass_sens.setValue(int(data["bass_sensitivity"]))
            self.slider_bass_sens.blockSignals(False)

        if "mid_sensitivity" in data:
            self.slider_mid_sens.blockSignals(True)
            self.slider_mid_sens.setValue(int(data["mid_sensitivity"]))
            self.slider_mid_sens.blockSignals(False)

        if "high_sensitivity" in data:
            self.slider_high_sens.blockSignals(True)
            self.slider_high_sens.setValue(int(data["high_sensitivity"]))
            self.slider_high_sens.blockSignals(False)

        if "attack" in data:
            self.slider_attack.blockSignals(True)
            self.slider_attack.setValue(int(data["attack"]))
            self.slider_attack.blockSignals(False)

        if "release" in data:
            self.slider_release.blockSignals(True)
            self.slider_release.setValue(int(data["release"]))
            self.slider_release.blockSignals(False)

        if "zone_weights" in data:
            zw = data["zone_weights"]
            if isinstance(zw, (list, tuple)) and len(zw) == 3:
                self.zone_balance.set_values(int(zw[0]), int(zw[1]), int(zw[2]))

        if "wave_speed" in data:
            self.slider_wave_speed.blockSignals(True)
            self.slider_wave_speed.setValue(int(data["wave_speed"]))
            self.slider_wave_speed.blockSignals(False)

        self._update_labels()
        self._update_mode_visibility()

        self._updating = False

    # ══════════════════════════════════════════════════════════════
    #  collect / apply / load
    # ══════════════════════════════════════════════════════════════

    def collect_params(self):
        """★ flowing_interval/flowing_speed 제거 — 미러링 섹션의 collect_params()에서 제공."""
        zb, zm, zh = self.zone_balance.get_values()
        return {
            "audio_mode": self._mode_key,
            "min_brightness": self.slider_min_brightness.value() / 100.0,
            "bass_sensitivity": self.slider_bass_sens.value() / 100.0,
            "mid_sensitivity": self.slider_mid_sens.value() / 100.0,
            "high_sensitivity": self.slider_high_sens.value() / 100.0,
            "attack": self.slider_attack.value() / 100.0,
            "release": self.slider_release.value() / 100.0,
            "zone_weights": (zb, zm, zh),
            "wave_speed": wave_speed_from_slider(self.slider_wave_speed.value()),
        }

    def apply_to_config(self):
        self._save_mode_params(self._mode_key)
        state = self._config.setdefault("options", {}).setdefault("audio_state", {})
        state["sub_mode"] = self._mode_key
        state["min_brightness"] = self.slider_min_brightness.value()

    def load_from_config(self):
        state = self._config.get("options", {}).get("audio_state", {})

        initial_mode = state.get("sub_mode", "pulse")
        if initial_mode not in _MODE_KEYS:
            initial_mode = "pulse"
        if initial_mode == "flowing" and not self._display_enabled:
            initial_mode = "pulse"

        self._mode_key = initial_mode
        self.combo_audio_mode.blockSignals(True)
        self.combo_audio_mode.setCurrentIndex(_MODE_TO_INDEX.get(initial_mode, 0))
        self.combo_audio_mode.blockSignals(False)

        self._load_mode_params(initial_mode)
        self._update_mode_visibility()
        self._update_labels()

    def cleanup(self):
        self._save_mode_params(self._mode_key)

    # ══════════════════════════════════════════════════════════════
    #  헬퍼
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _make_bar(grid, row, name, object_name):
        grid.addWidget(QLabel(name), row, 0)
        bar = QProgressBar()
        bar.setObjectName(object_name)
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(14)
        grid.addWidget(bar, row, 1)
        return bar

    @staticmethod
    def _make_sep():
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        return sep