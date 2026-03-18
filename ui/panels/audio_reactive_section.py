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
   - Flowing: palette 프리뷰 + 갱신 주기 + 흐름 속도

[변경]
- ★ min_brightness를 모드별로 독립 저장/로드
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QComboBox, QFrame, QProgressBar, QGridLayout, QPushButton,
)
from PySide6.QtCore import Qt, Signal, QTimer

from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.spectrum import SpectrumWidget
from ui.widgets.zone_balance import ZoneBalanceWidget
from ui.widgets.flow_palette_preview import FlowPalettePreview
from core.engine_utils import wave_speed_from_slider

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

# bass-only 모드: mid/high 감도 슬라이더 비활성화
_BASS_ONLY_MODES = {"pulse", "wave", "dynamic"}

# 대역 비율이 의미 있는 모드
_BANDED_MODES = {"spectrum", "bass_detail"}

# ── 모드별 기본값 (★ min_brightness 포함) ──
_AUDIO_DEFAULTS = {
    "pulse":       {"min_brightness": 5, "bass_sens": 100, "mid_sens": 100, "high_sens": 100, "attack": 50, "release": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "spectrum":    {"min_brightness": 5, "bass_sens": 100, "mid_sens": 100, "high_sens": 100, "attack": 50, "release": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "bass_detail": {"min_brightness": 5, "bass_sens": 100, "mid_sens": 100, "high_sens": 100, "attack": 10, "release": 70, "zone_bass": 48, "zone_mid": 26, "zone_high": 26},
    "wave":        {"min_brightness": 5, "bass_sens": 120, "mid_sens": 100, "high_sens": 100, "attack": 60, "release": 40, "wave_speed": 50, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "dynamic":     {"min_brightness": 5, "bass_sens": 110, "mid_sens": 110, "high_sens": 120, "attack": 55, "release": 45, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "flowing":     {"min_brightness": 5, "bass_sens": 100, "mid_sens": 100, "high_sens": 100, "attack": 40, "release": 60, "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
}

# ── 레이아웃 상수 ──
_GROUP_MARGINS = (6, 16, 6, 6)
_GROUP_SPACING = 4


class AudioReactiveSection(QWidget):
    """오디오 반응 설정 패널.

    Signals:
        params_changed(): 오디오 파라미터가 변경되었을 때 emit
    """

    params_changed = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._mode_key = "pulse"
        self._default_mode = config.get("options", {}).get("audio_state", {}).get("default_audio_mode", "pulse")
        self._display_enabled = False
        self._updating = False  # 재귀 시그널 방지
        self._build_ui()
        self.load_from_config()

    # ══════════════════════════════════════════════════════════════
    #  UI 빌드
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ── 에너지 레벨 ──
        self._build_energy_section(layout)

        # ── 오디오 반응 설정 ──
        self._build_audio_settings(layout)

    def _build_energy_section(self, parent_layout):
        """에너지 레벨 바 + 스펙트럼 — 한 벌."""
        grp = QGroupBox("에너지 레벨")
        gl = QVBoxLayout(grp)
        gl.setSpacing(_GROUP_SPACING)
        gl.setContentsMargins(*_GROUP_MARGINS)

        grid = QGridLayout()
        self.bar_bass = self._make_bar(grid, 0, "Bass", "#e74c3c")
        self.bar_mid = self._make_bar(grid, 1, "Mid", "#27ae60")
        self.bar_high = self._make_bar(grid, 2, "High", "#3498db")
        gl.addLayout(grid)

        gl.addWidget(QLabel("스펙트럼 (16밴드)"))
        self.spectrum_widget = SpectrumWidget(n_bands=16)
        gl.addWidget(self.spectrum_widget)

        parent_layout.addWidget(grp)

    def _build_audio_settings(self, parent_layout):
        """오디오 반응 설정 — 모드 콤보 + 공통 설정 + 모드별 세부 설정."""
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

        # ── 기본 모드 설정 버튼 + 힌트 (같은 행) ──
        default_row = QHBoxLayout()
        self.btn_set_default_mode = QPushButton("현재 모드를 기본으로 설정")
        self.btn_set_default_mode.setFixedHeight(24)
        self.btn_set_default_mode.setStyleSheet(
            "QPushButton{background:#444;color:#bbb;font-size:11px;"
            "border-radius:4px;padding:2px 10px;}"
            "QPushButton:hover{background:#555;color:#eee;}"
        )
        self.btn_set_default_mode.clicked.connect(self._on_set_default_mode)
        default_row.addWidget(self.btn_set_default_mode)

        self.lbl_default_mode_hint = QLabel("")
        self.lbl_default_mode_hint.setStyleSheet(
            "color:#6a6a74;font-size:10px;font-style:italic;"
        )
        default_row.addWidget(self.lbl_default_mode_hint)
        default_row.addStretch()
        gl.addLayout(default_row)
        self._update_default_mode_hint()

        # ── 구분선 ──
        gl.addWidget(self._make_sep())

        # ── 공통 설정 헤더 ──
        lbl_common = QLabel("오디오 공통 설정")
        lbl_common.setStyleSheet("font-weight:bold;font-size:11px;")
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

        # 감도 — mid (bass-only 모드에서 비활성)
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
        hint.setStyleSheet("color:#6a6a74;font-size:10px;font-style:italic;")
        gl.addWidget(hint)

        # ── 구분선 ──
        gl.addWidget(self._make_sep())

        # ── 모드 세부 설정 ──
        self._build_mode_specific(gl)

        parent_layout.addWidget(grp)

    def _build_mode_specific(self, parent_layout):
        """모드별 조건부 세부 설정 위젯들."""

        # ── Wave 속도 ──
        self._wave_container = QWidget()
        wl = QVBoxLayout(self._wave_container)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(3)
        lbl_wave = QLabel("Wave 설정")
        lbl_wave.setStyleSheet("font-weight:bold;font-size:11px;")
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
        hint_w.setStyleSheet("color:#6a6a74;font-size:10px;font-style:italic;")
        wl.addWidget(hint_w)
        parent_layout.addWidget(self._wave_container)
        self._wave_container.setVisible(False)

        # ── 대역 비율 (spectrum / bass_detail) ──
        self._zone_container = QWidget()
        zl = QVBoxLayout(self._zone_container)
        zl.setContentsMargins(0, 0, 0, 0)
        zl.setSpacing(3)
        lbl_zone = QLabel("대역 비율")
        lbl_zone.setStyleSheet("font-weight:bold;font-size:11px;")
        zl.addWidget(lbl_zone)
        self.zone_balance = ZoneBalanceWidget(33, 33, 34)
        self.zone_balance.zone_changed.connect(lambda *_: self._on_param_changed())
        zl.addWidget(self.zone_balance)
        parent_layout.addWidget(self._zone_container)
        self._zone_container.setVisible(False)

        # ── Flowing 설정 ──
        self._flowing_container = QWidget()
        fl = QVBoxLayout(self._flowing_container)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(3)
        lbl_flow = QLabel("Flowing 설정")
        lbl_flow.setStyleSheet("font-weight:bold;font-size:11px;")
        fl.addWidget(lbl_flow)

        # palette 프리뷰
        pal_row = QHBoxLayout()
        pal_row.addWidget(QLabel("현재 팔레트:"))
        self.flow_palette_preview = FlowPalettePreview(n_swatches=5)
        pal_row.addWidget(self.flow_palette_preview, 1)
        fl.addLayout(pal_row)

        # 갱신 주기
        fi_row = QHBoxLayout()
        fi_row.addWidget(QLabel("갱신 주기:"))
        self.slider_flowing_interval = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_flowing_interval.setRange(10, 100)  # 1.0~10.0초 (×0.1)
        self.slider_flowing_interval.setValue(30)
        self.slider_flowing_interval.valueChanged.connect(self._on_param_changed)
        fi_row.addWidget(self.slider_flowing_interval)
        self.lbl_flowing_interval = QLabel("3.0초")
        self.lbl_flowing_interval.setMinimumWidth(40)
        self.lbl_flowing_interval.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fi_row.addWidget(self.lbl_flowing_interval)
        fl.addLayout(fi_row)

        # 흐름 속도
        fs_row = QHBoxLayout()
        fs_row.addWidget(QLabel("흐름 속도:"))
        self.slider_flowing_speed = NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider_flowing_speed.setRange(0, 100)
        self.slider_flowing_speed.setValue(50)
        self.slider_flowing_speed.valueChanged.connect(self._on_param_changed)
        fs_row.addWidget(self.slider_flowing_speed)
        self.lbl_flowing_speed = QLabel("50%")
        self.lbl_flowing_speed.setMinimumWidth(40)
        self.lbl_flowing_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fs_row.addWidget(self.lbl_flowing_speed)
        fl.addLayout(fs_row)

        hint_f = QLabel("갱신 주기 ↑ = 안정적 · 속도 ↑ = 빠른 회전")
        hint_f.setStyleSheet("color:#6a6a74;font-size:10px;font-style:italic;")
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
            self.params_changed.emit()

    def _on_param_changed(self, _=None):
        if self._updating:
            return
        self._update_labels()
        self.params_changed.emit()

    def _on_set_default_mode(self):
        """현재 선택된 모드를 기본 오디오 모드로 저장."""
        self._default_mode = self._mode_key
        state = self._config.setdefault("options", {}).setdefault("audio_state", {})
        state["default_audio_mode"] = self._mode_key
        self._update_default_mode_hint()
        # 버튼 피드백
        self.btn_set_default_mode.setText("✅ 저장됨")
        QTimer.singleShot(2000, lambda: self.btn_set_default_mode.setText(
            "현재 모드를 기본으로 설정"
        ))

    def _update_default_mode_hint(self):
        """기본 모드 힌트 라벨 갱신."""
        mode_name = dict(_AUDIO_MODE_ITEMS).get(self._default_mode, self._default_mode)
        # 짧은 이름으로 표시 (설명 부분 제거)
        short_name = mode_name.split("—")[0].strip() if "—" in mode_name else mode_name
        hint = f"기본 모드: {short_name}"
        if self._default_mode == "flowing":
            hint += "  (디스플레이 OFF 시 Pulse로 대체)"
        self.lbl_default_mode_hint.setText(hint)

    def _update_labels(self):
        """모든 슬라이더 라벨 갱신."""
        self.lbl_min_brightness.setText(f"{self.slider_min_brightness.value()}%")
        self.lbl_bass_sens.setText(f"{self.slider_bass_sens.value() / 100:.2f}")
        self.lbl_mid_sens.setText(f"{self.slider_mid_sens.value() / 100:.2f}")
        self.lbl_high_sens.setText(f"{self.slider_high_sens.value() / 100:.2f}")
        self.lbl_attack.setText(f"{self.slider_attack.value() / 100:.2f}")
        self.lbl_release.setText(f"{self.slider_release.value() / 100:.2f}")
        self.lbl_wave_speed.setText(f"{self.slider_wave_speed.value()}%")
        self.lbl_flowing_interval.setText(f"{self.slider_flowing_interval.value() / 10:.1f}초")
        self.lbl_flowing_speed.setText(f"{self.slider_flowing_speed.value()}%")

    def _update_mode_visibility(self):
        """모드에 따라 세부 설정 위젯 표시/숨김."""
        mode = self._mode_key
        is_bass_only = mode in _BASS_ONLY_MODES
        is_banded = mode in _BANDED_MODES
        is_wave = mode == "wave"
        is_flowing = mode == "flowing"

        # mid/high 감도 비활성화 (bass-only 모드)
        self._row_mid_sens.setEnabled(not is_bass_only)
        self._row_high_sens.setEnabled(not is_bass_only)

        # 감도 헤더 텍스트
        header_map = {
            "pulse": "감도 (Bass)", "wave": "감도 (Bass → Wave)",
            "dynamic": "감도 (Dynamic)", "spectrum": "감도 (대역별)",
            "bass_detail": "감도 (Bass Detail)", "flowing": "감도 (Flowing)",
        }
        self.lbl_sens_header.setText(header_map.get(mode, "감도"))

        # 세부 설정 가시성
        self._wave_container.setVisible(is_wave)
        self._zone_container.setVisible(is_banded)
        self._flowing_container.setVisible(is_flowing)

    # ══════════════════════════════════════════════════════════════
    #  디스플레이 토글 연동
    # ══════════════════════════════════════════════════════════════

    def set_display_enabled(self, enabled):
        """디스플레이 토글 상태를 전달. flowing 모드 활성/비활성 제어."""
        self._display_enabled = enabled
        # flowing 항목 활성/비활성
        flowing_idx = _MODE_TO_INDEX.get("flowing", 5)
        model = self.combo_audio_mode.model()
        item = model.item(flowing_idx)
        if item:
            if enabled:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
                # ★ 디스플레이 ON + flowing이 기본 모드 → 자동 전환
                if (self._default_mode == "flowing"
                        and self._mode_key != "flowing"):
                    self.combo_audio_mode.setCurrentIndex(flowing_idx)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                # 현재 flowing 선택 상태인데 디스플레이 꺼지면 pulse로 복귀
                if self._mode_key == "flowing":
                    self.combo_audio_mode.setCurrentIndex(0)
        self._update_default_mode_hint()

    # ══════════════════════════════════════════════════════════════
    #  에너지 / 스펙트럼 갱신 (엔진에서 호출)
    # ══════════════════════════════════════════════════════════════

    def update_energy(self, bass, mid, high):
        self.bar_bass.setValue(int(bass * 100))
        self.bar_mid.setValue(int(mid * 100))
        self.bar_high.setValue(int(high * 100))

    def update_spectrum(self, spec):
        self.spectrum_widget.set_values(spec)

    def update_flow_palette(self, colors, ratios=None):
        if self.flow_palette_preview.isVisible():
            self.flow_palette_preview.set_colors(colors, ratios)

    # ══════════════════════════════════════════════════════════════
    #  모드별 파라미터 저장/로드
    # ══════════════════════════════════════════════════════════════

    def _save_mode_params(self, mode_name):
        """현재 공통 슬라이더 값을 config의 해당 모드 키에 저장."""
        d = self._config.setdefault(f"audio_{mode_name}", {})
        d["min_brightness"] = self.slider_min_brightness.value()  # ★ 모드별 저장
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
        """config의 해당 모드 키에서 슬라이더 값을 복원."""
        self._updating = True
        df = _AUDIO_DEFAULTS.get(mode_name, _AUDIO_DEFAULTS["pulse"])
        d = self._config.get(f"audio_{mode_name}", df)

        self.slider_min_brightness.setValue(d.get("min_brightness", df["min_brightness"]))  # ★ 모드별 로드
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
    #  collect / apply / load
    # ══════════════════════════════════════════════════════════════

    def collect_params(self):
        """현재 오디오 파라미터를 dict로 반환.

        EngineParams 필드명에 맞춘 키를 사용.
        """
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
            "flowing_interval": self.slider_flowing_interval.value() / 10.0,
            "flowing_speed": self._flowing_speed_from_slider(),
        }

    def _flowing_speed_from_slider(self):
        """슬라이더(0~100) → 실제 회전 속도."""
        t = self.slider_flowing_speed.value() / 100.0
        return 0.02 + t * 0.18

    def apply_to_config(self):
        """현재 상태를 config에 반영."""
        self._save_mode_params(self._mode_key)
        state = self._config.setdefault("options", {}).setdefault("audio_state", {})
        state["sub_mode"] = self._mode_key
        state["default_audio_mode"] = self._default_mode  # ★ 기본 모드 저장
        state["min_brightness"] = self.slider_min_brightness.value()
        state["flowing_interval"] = self.slider_flowing_interval.value()
        state["flowing_speed"] = self.slider_flowing_speed.value()

    def load_from_config(self):
        """config에서 상태 복원.

        ★ 기본 모드(default_audio_mode) 기반으로 초기 모드 결정.
        sub_mode는 마지막 사용 모드(세션 복원용)이고,
        default_audio_mode는 사용자가 명시적으로 설정한 기본 모드.
        기본 모드가 있으면 기본 모드를, 없으면 sub_mode를 사용.

        flowing이 기본인데 디스플레이 OFF면 pulse로 폴백.
        """
        state = self._config.get("options", {}).get("audio_state", {})

        # ★ 기본 모드 복원
        self._default_mode = state.get("default_audio_mode", "pulse")
        if self._default_mode not in _MODE_KEYS:
            self._default_mode = "pulse"

        # 초기 모드 결정: 기본 모드 우선, flowing 폴백 처리
        initial_mode = self._default_mode
        if initial_mode == "flowing" and not self._display_enabled:
            initial_mode = "pulse"

        self._mode_key = initial_mode
        self.combo_audio_mode.blockSignals(True)
        self.combo_audio_mode.setCurrentIndex(_MODE_TO_INDEX.get(initial_mode, 0))
        self.combo_audio_mode.blockSignals(False)

        # flowing 슬라이더
        self.slider_flowing_interval.blockSignals(True)
        self.slider_flowing_interval.setValue(state.get("flowing_interval", 30))
        self.slider_flowing_interval.blockSignals(False)
        self.slider_flowing_speed.blockSignals(True)
        self.slider_flowing_speed.setValue(state.get("flowing_speed", 50))
        self.slider_flowing_speed.blockSignals(False)

        # ★ 모드별 파라미터 로드 (min_brightness 포함)
        self._load_mode_params(initial_mode)
        self._update_mode_visibility()
        self._update_labels()
        self._update_default_mode_hint()

    def cleanup(self):
        """종료 시 현재 모드 파라미터 저장."""
        self._save_mode_params(self._mode_key)

    # ══════════════════════════════════════════════════════════════
    #  헬퍼
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _make_bar(grid, row, name, color):
        grid.addWidget(QLabel(name), row, 0)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(14)
        bar.setStyleSheet(
            f"QProgressBar{{background:#2b2b2b;border-radius:3px}}"
            f"QProgressBar::chunk{{background:{color};border-radius:3px}}"
        )
        grid.addWidget(bar, row, 1)
        return bar

    @staticmethod
    def _make_sep():
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        return sep