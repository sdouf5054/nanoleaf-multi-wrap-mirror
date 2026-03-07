"""오디오 비주얼라이저 탭 v8

[변경 v8 — bass_detail 모드 복원]
- ★ 콤보박스 3개 모드: Pulse / Spectrum / Bass Detail
- ★ MODE_BASS_DETAIL import 추가
- ★ MODE_DEFAULTS에 bass_detail 섹션 추가
- ★ _on_mode_changed / _get_current_mode / _update_mode_ui 3모드 대응

[변경 v7 — HybridVisualizer 통합 + 색상 소스 UI]
- ★ AudioVisualizer → HybridVisualizer 전환
  color_source == "solid"이면 기존과 100% 동일 동작
- ★ 색상 소스 선택 UI: 단색/무지개, 화면 구역, 화면 전체
- ★ 구역 수 선택 (4/8/16/32) — 화면 구역 모드 전용
- ★ 화면 색상 프리뷰 위젯 — 현재 구역 색상 실시간 표시
- ★ 모드별 파라미터 프리셋 유지 (v6 호환)
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider,
    QLabel, QGroupBox, QComboBox, QColorDialog, QProgressBar,
    QFrame, QGridLayout, QMessageBox, QScrollArea
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QPainter, QBrush
import numpy as np
import os
import psutil

from core.audio_engine import list_loopback_devices, HAS_PYAUDIO
from core.audio_visualizer import MODE_PULSE, MODE_SPECTRUM, MODE_BASS_DETAIL, _remap_t
from core.hybrid_visualizer import (
    HybridVisualizer,
    COLOR_SOURCE_SOLID, COLOR_SOURCE_SCREEN,
    N_ZONES_PER_LED,
    _build_led_zone_map_by_side,
)
from core.layout import get_led_positions
from core.config import save_config

COLOR_PRESETS = [
    ("🌈 무지개", None, None, None),
    ("핑크/마젠타", 255, 0,   80),
    ("빨강",       255, 30,  0),
    ("주황",       255, 120, 0),
    ("노랑",       255, 220, 0),
    ("초록",       0,   255, 80),
    ("시안",       0,   220, 255),
    ("파랑",       30,  0,   255),
    ("보라",       150, 0,   255),
    ("흰색",       255, 255, 255),
]

# 모드별 기본값
MODE_DEFAULTS = {
    "pulse": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100,
        "attack": 50, "release": 50,
        "zone_bass": 33, "zone_mid": 33, "zone_high": 34,
    },
    "spectrum": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100,
        "attack": 50, "release": 50,
        "zone_bass": 33, "zone_mid": 33, "zone_high": 34,
    },
    "bass_detail": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100,
        "attack": 10, "release": 70,
        "zone_bass": 48, "zone_mid": 26, "zone_high": 26,
    },
}

# 공통 무지개 키포인트
RAINBOW_KEYPOINTS = [
    (0.000, 255,   0,   0),
    (0.130, 255, 127,   0),
    (0.260, 255, 255,   0),
    (0.400,   0, 255,   0),
    (0.540,   0, 180, 255),
    (0.680,   0,  50, 255),
    (0.820,  80,   0, 255),
    (1.000, 160,   0, 220),
]

# ★ 구역 수 선택지
ZONE_OPTIONS = [
    (1,  "1구역 (화면 전체 평균)"),
    (2,  "2구역 (상/하)"),
    (4,  "4구역 (상하좌우)"),
    (8,  "8구역 (모서리 포함)"),
    (16, "16구역"),
    (32, "32구역"),
    (N_ZONES_PER_LED, "LED별 개별 (미러링)"),
]


def rainbow_color_at(t):
    t = max(0.0, min(1.0, t))
    kp = RAINBOW_KEYPOINTS
    for i in range(len(kp) - 1):
        t0, r0, g0, b0 = kp[i]
        t1, r1, g1, b1 = kp[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return (int(r0 + (r1 - r0) * f),
                    int(g0 + (g1 - g0) * f),
                    int(b0 + (b1 - b0) * f))
    return (160, 0, 220)


def _ensure_audio_config(config):
    """config에 audio_pulse/audio_spectrum/audio_bass_detail 섹션이 없으면 기본값으로 생성."""
    for mode_key in ("audio_pulse", "audio_spectrum", "audio_bass_detail"):
        if mode_key not in config:
            mode_name = mode_key.replace("audio_", "")
            config[mode_key] = dict(MODE_DEFAULTS.get(mode_name, MODE_DEFAULTS["pulse"]))


class NoScrollSlider(QSlider):
    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
        else:
            super().wheelEvent(event)


class GradientPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._zone_weights = (33, 33, 34)
        self.setFixedHeight(20)
        self.setMinimumWidth(100)

    def set_zone_weights(self, bass, mid, high):
        self._zone_weights = (bass, mid, high)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        w = self.width()
        h = self.height()
        for x in range(w):
            t_uniform = x / max(1, w - 1)
            t_remapped = _remap_t(t_uniform, self._zone_weights)
            r, g, b = rainbow_color_at(t_remapped)
            painter.setPen(QColor(r, g, b))
            painter.drawLine(x, 0, x, h)
        b_pct = self._zone_weights[0] / 100.0
        m_pct = self._zone_weights[1] / 100.0
        painter.setPen(QColor(255, 255, 255, 120))
        painter.drawLine(int(b_pct * w), 0, int(b_pct * w), h)
        painter.drawLine(int((b_pct + m_pct) * w), 0, int((b_pct + m_pct) * w), h)
        painter.end()


class ZoneBalanceWidget(QWidget):
    zone_changed = pyqtSignal(int, int, int)
    MIN_ZONE = 5

    def __init__(self, bass=33, mid=33, high=34, parent=None):
        super().__init__(parent)
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.gradient_preview = GradientPreview()
        layout.addWidget(self.gradient_preview)

        self._sliders = {}
        self._labels = {}
        for name, default, color in [
            ("Bass", bass, "#e74c3c"),
            ("Mid", mid, "#27ae60"),
            ("High", high, "#3498db"),
        ]:
            row = QHBoxLayout()
            lbl_name = QLabel(f"{name}:")
            lbl_name.setMinimumWidth(35)
            lbl_name.setStyleSheet(f"color:{color};font-weight:bold;")
            row.addWidget(lbl_name)

            slider = NoScrollSlider(Qt.Horizontal)
            slider.setRange(self.MIN_ZONE, 100 - 2 * self.MIN_ZONE)
            slider.setValue(default)
            slider.valueChanged.connect(lambda v, n=name: self._on_slider_changed(n, v))
            row.addWidget(slider)

            lbl_val = QLabel(f"{default}%")
            lbl_val.setMinimumWidth(35)
            lbl_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(lbl_val)

            layout.addLayout(row)
            self._sliders[name] = slider
            self._labels[name] = lbl_val

        self._update_gradient()

    def _on_slider_changed(self, changed_name, new_value):
        if self._updating:
            return
        self._updating = True

        names = ["Bass", "Mid", "High"]
        others = [n for n in names if n != changed_name]

        other_vals = {n: self._sliders[n].value() for n in others}
        other_sum = sum(other_vals.values())
        remaining = 100 - new_value

        if other_sum == 0:
            for n in others:
                self._sliders[n].setValue(remaining // 2)
        else:
            for n in others:
                ratio = other_vals[n] / other_sum
                new_v = max(self.MIN_ZONE, int(round(remaining * ratio)))
                self._sliders[n].setValue(new_v)

        vals = {n: self._sliders[n].value() for n in names}
        diff = 100 - sum(vals.values())
        if diff != 0:
            for n in others:
                adjusted = vals[n] + diff
                if self.MIN_ZONE <= adjusted <= 100 - 2 * self.MIN_ZONE:
                    self._sliders[n].setValue(adjusted)
                    break

        for n in names:
            self._labels[n].setText(f"{self._sliders[n].value()}%")

        self._update_gradient()
        self._updating = False

        b, m, h = self.get_values()
        self.zone_changed.emit(b, m, h)

    def _update_gradient(self):
        b, m, h = self.get_values()
        self.gradient_preview.set_zone_weights(b, m, h)

    def get_values(self):
        return (
            self._sliders["Bass"].value(),
            self._sliders["Mid"].value(),
            self._sliders["High"].value(),
        )

    def set_values(self, bass, mid, high):
        self._updating = True
        self._sliders["Bass"].setValue(bass)
        self._sliders["Mid"].setValue(mid)
        self._sliders["High"].setValue(high)
        for name in ["Bass", "Mid", "High"]:
            self._labels[name].setText(f"{self._sliders[name].value()}%")
        self._update_gradient()
        self._updating = False

    def setEnabled(self, enabled):
        for s in self._sliders.values():
            s.setEnabled(enabled)


class SpectrumWidget(QWidget):
    def __init__(self, n_bands=16, parent=None):
        super().__init__(parent)
        self.n_bands = n_bands
        self._values = np.zeros(n_bands)
        self._zone_weights = (33, 33, 34)
        self.setMinimumHeight(50)
        self.setMaximumHeight(70)

    def set_values(self, values):
        self._values = np.clip(values, 0, 1)
        self.update()

    def set_zone_weights(self, bass, mid, high):
        self._zone_weights = (bass, mid, high)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        n = self.n_bands
        bar_w = max(2, (w - n + 1) // n)
        gap = max(1, (w - bar_w * n) // max(1, n - 1))

        for i in range(n):
            val = self._values[i] if i < len(self._values) else 0
            bar_h = max(1, int(val * (h - 4)))
            x = i * (bar_w + gap)
            y = h - bar_h - 2
            t_uniform = i / max(1, n - 1)
            t_remapped = _remap_t(t_uniform, self._zone_weights)
            r, g, b = rainbow_color_at(t_remapped)
            painter.setBrush(QBrush(QColor(r, g, b)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(x, y, bar_w, bar_h, 2, 2)
        painter.end()


# ★ 모니터 형태 LED 프리뷰 위젯
class MonitorPreview(QWidget):
    """모니터 둘레에 LED 색상을 표시하는 프리뷰 위젯."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._led_colors = None
        self._zone_map = None
        self._led_count = config["device"]["led_count"]
        self._positions = None
        self._sides = None
        self._n_zones = 4

        self.setMinimumHeight(220)
        self.setMaximumHeight(360)
        self.setMinimumWidth(400)

        self._compute_positions()

    def _compute_positions(self):
        layout_cfg = self._config["layout"]
        mirror_cfg = self._config.get("mirror", {})

        screen_w = mirror_cfg.get("grid_cols", 64) * 40
        screen_h = mirror_cfg.get("grid_rows", 32) * 40

        positions, sides = get_led_positions(
            screen_w, screen_h,
            layout_cfg["segments"], self._led_count,
            orientation=mirror_cfg.get("orientation", "auto"),
            portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
        )

        self._positions = np.zeros_like(positions)
        if screen_w > 0:
            self._positions[:, 0] = positions[:, 0] / screen_w
        if screen_h > 0:
            self._positions[:, 1] = positions[:, 1] / screen_h
        self._sides = sides

        self._wrap_index = np.zeros(self._led_count, dtype=np.int32)
        side_count = {}
        for seg in layout_cfg["segments"]:
            start, end, side = seg["start"], seg["end"], seg["side"]
            n = abs(start - end)
            if n == 0:
                continue
            wrap = side_count.get(side, 0)
            side_count[side] = wrap + 1
            step = -1 if start > end else 1
            for i in range(n):
                idx = start + step * i
                if 0 <= idx < self._led_count:
                    self._wrap_index[idx] = wrap

    def set_zone_map(self, zone_map):
        self._zone_map = zone_map

    def set_n_zones(self, n_zones):
        self._n_zones = n_zones

    def set_colors(self, colors):
        if colors is not None and len(colors) > 0:
            self._led_colors = np.clip(colors, 0, 255).astype(np.float32)
            self.update()

    def _get_led_color(self, led_idx):
        if self._led_colors is None:
            return (60, 60, 60)

        n_colors = len(self._led_colors)

        if n_colors >= self._led_count:
            c = self._led_colors[led_idx]
        elif self._zone_map is not None:
            zone_idx = self._zone_map[led_idx]
            if zone_idx < n_colors:
                c = self._led_colors[zone_idx]
            else:
                c = self._led_colors[zone_idx % n_colors]
        elif n_colors == 1:
            c = self._led_colors[0]
        else:
            return (60, 60, 60)

        return (int(c[0]), int(c[1]), int(c[2]))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        led_size = 11
        wrap_gap = led_size + 8
        led_margin = wrap_gap * 2 + 20

        avail_w = w - 2 * led_margin
        avail_h = h - 2 * led_margin
        if avail_w < 40 or avail_h < 20:
            painter.end()
            return

        aspect = 16.0 / 9.0
        if avail_w / avail_h > aspect:
            mon_h = avail_h
            mon_w = int(mon_h * aspect)
        else:
            mon_w = avail_w
            mon_h = int(mon_w / aspect)

        mon_x = (w - mon_w) // 2
        mon_y = (h - mon_h) // 2

        painter.setPen(QColor(80, 80, 80))
        painter.setBrush(QBrush(QColor(45, 45, 48)))
        painter.drawRoundedRect(mon_x, mon_y, mon_w, mon_h, 3, 3)

        if self._positions is None:
            painter.end()
            return

        half_led = led_size // 2

        for i in range(self._led_count):
            nx, ny = self._positions[i]
            side = self._sides[i]
            wrap = self._wrap_index[i]

            px = mon_x + nx * mon_w
            py = mon_y + ny * mon_h

            dist = wrap_gap * (2 - wrap)

            if side == "top":
                py = mon_y - dist
            elif side == "bottom":
                py = mon_y + mon_h + dist - led_size
            elif side == "left":
                px = mon_x - dist
            elif side == "right":
                px = mon_x + mon_w + dist - led_size

            r, g, b = self._get_led_color(i)
            painter.setBrush(QBrush(QColor(r, g, b)))
            painter.setPen(QColor(70, 70, 70))
            painter.drawRoundedRect(
                int(px - half_led), int(py - half_led),
                led_size, led_size, 2, 2
            )

        painter.end()


class AudioTab(QWidget):
    """오디오 비주얼라이저 탭 v8 — HybridVisualizer + 색상 소스 UI + 3모드."""

    request_mirror_stop = pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        _ensure_audio_config(config)
        self._visualizer = None
        self._is_running = False
        self._current_color = (255, 0, 80)
        self._edit_mode = False
        self._current_mode_key = "pulse"  # "pulse" / "spectrum" / "bass_detail"
        self._switching_mode = False

        self._all_sliders = []

        self._build_ui()
        self._load_mode_params("pulse")

        self._decay_timer = QTimer(self)
        self._decay_timer.setInterval(50)
        self._decay_timer.timeout.connect(self._decay_levels)

        self._process = psutil.Process(os.getpid())
        self._process.cpu_percent()
        self._res_timer = QTimer(self)
        self._res_timer.timeout.connect(self._update_resource_usage)
        self._res_timer.start(2000)

    # ── 모드별 파라미터 저장/로드 ────────────────────────────────

    def _config_key_for_mode(self, mode_name):
        return f"audio_{mode_name}"

    def _save_current_params_to_mode(self, mode_name):
        key = self._config_key_for_mode(mode_name)
        if key not in self.config:
            self.config[key] = {}
        d = self.config[key]
        d["bass_sens"] = self.slider_bass_sens.value()
        d["mid_sens"] = self.slider_mid_sens.value()
        d["high_sens"] = self.slider_high_sens.value()
        d["brightness"] = self.slider_brightness.value()
        d["attack"] = self.slider_attack.value()
        d["release"] = self.slider_release.value()
        zb, zm, zh = self.zone_balance.get_values()
        d["zone_bass"] = zb
        d["zone_mid"] = zm
        d["zone_high"] = zh

    def _load_mode_params(self, mode_name):
        key = self._config_key_for_mode(mode_name)
        defaults = MODE_DEFAULTS.get(mode_name, MODE_DEFAULTS["pulse"])
        d = self.config.get(key, defaults)

        self._switching_mode = True
        self.slider_bass_sens.setValue(d.get("bass_sens", defaults["bass_sens"]))
        self.slider_mid_sens.setValue(d.get("mid_sens", defaults["mid_sens"]))
        self.slider_high_sens.setValue(d.get("high_sens", defaults["high_sens"]))
        self.slider_brightness.setValue(d.get("brightness", defaults["brightness"]))
        self.slider_attack.setValue(d.get("attack", defaults["attack"]))
        self.slider_release.setValue(d.get("release", defaults["release"]))
        self.zone_balance.set_values(
            d.get("zone_bass", defaults["zone_bass"]),
            d.get("zone_mid", defaults["zone_mid"]),
            d.get("zone_high", defaults["zone_high"]),
        )
        self._switching_mode = False

        self._on_sens_changed()
        self._on_brightness_changed(self.slider_brightness.value())
        self._on_ar_changed()
        zb, zm, zh = self.zone_balance.get_values()
        self._on_zone_changed(zb, zm, zh)

        self._current_mode_key = mode_name

    def _apply_params_to_visualizer(self):
        if not self._visualizer:
            return
        self._visualizer.brightness = self.slider_brightness.value() / 100.0
        self._visualizer.bass_sensitivity = self.slider_bass_sens.value() / 100.0
        self._visualizer.mid_sensitivity = self.slider_mid_sens.value() / 100.0
        self._visualizer.high_sensitivity = self.slider_high_sens.value() / 100.0
        self._visualizer.attack = self.slider_attack.value() / 100.0
        self._visualizer.release = self.slider_release.value() / 100.0
        b, m, h = self.zone_balance.get_values()
        self._visualizer.set_zone_weights(b, m, h)

    # ── UI 빌드 ──────────────────────────────────────────────────

    def _build_ui(self):
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(8)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll.setWidget(container)

        if not HAS_PYAUDIO:
            warn = QLabel(
                "⚠ PyAudioWPatch가 설치되지 않았습니다.\n"
                "pip install PyAudioWPatch"
            )
            warn.setStyleSheet("color:#e74c3c;font-size:14px;padding:20px;")
            warn.setWordWrap(True)
            layout.addWidget(warn)
            layout.addStretch()
            return

        # === 상태 ===
        status_group = QGroupBox("상태")
        sl = QHBoxLayout(status_group)
        self.status_label = QLabel("대기 중")
        self.status_label.setStyleSheet("font-size:14px;font-weight:bold;")
        sl.addWidget(self.status_label)
        sl.addStretch()
        self.cpu_label = QLabel("CPU: —%")
        self.cpu_label.setStyleSheet("font-size:12px;color:#d35400;margin-right:6px;")
        sl.addWidget(self.cpu_label)
        self.ram_label = QLabel("RAM: — MB")
        self.ram_label.setStyleSheet("font-size:12px;color:#27ae60;margin-right:10px;")
        sl.addWidget(self.ram_label)
        self.fps_label = QLabel("— fps")
        self.fps_label.setStyleSheet("font-size:14px;color:#888;")
        sl.addWidget(self.fps_label)
        layout.addWidget(status_group)

        # === 에너지 레벨 ===
        level_group = QGroupBox("에너지 레벨")
        ll = QVBoxLayout(level_group)
        bar_grid = QGridLayout()
        self.bar_bass = self._make_bar(bar_grid, 0, "Bass", "#e74c3c")
        self.bar_mid = self._make_bar(bar_grid, 1, "Mid", "#27ae60")
        self.bar_high = self._make_bar(bar_grid, 2, "High", "#3498db")
        ll.addLayout(bar_grid)
        ll.addWidget(QLabel("스펙트럼 (16밴드, 로그 스케일)"))
        self.spectrum_widget = SpectrumWidget(n_bands=16)
        ll.addWidget(self.spectrum_widget)
        layout.addWidget(level_group)

        # === 제어 버튼 ===
        btn_layout = QHBoxLayout()
        self.btn_start = QPushButton("▶ 시작")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.setStyleSheet(
            "QPushButton{background:#8e44ad;color:white;font-size:14px;"
            "font-weight:bold;border-radius:6px}"
            "QPushButton:hover{background:#9b59b6}"
        )
        self.btn_start.clicked.connect(self._start_visualizer)
        btn_layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton("⏹ 중지")
        self.btn_stop.setMinimumHeight(40)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-size:14px;"
            "font-weight:bold;border-radius:6px}"
            "QPushButton:hover{background:#e74c3c}"
            "QPushButton:disabled{background:#666}"
        )
        self.btn_stop.clicked.connect(self._stop_visualizer)
        btn_layout.addWidget(self.btn_stop)
        layout.addLayout(btn_layout)

        # === 모드 (★ 3개 옵션) ===
        mode_group = QGroupBox("비주얼라이저 모드")
        ml = QVBoxLayout(mode_group)
        self.combo_mode = QComboBox()
        self.combo_mode.addItems([
            "🔴 Bass 반응 — 저음 기반 전체 밝기",
            "🌈 Spectrum — 16밴드 주파수 매핑",
            "🔊 Bass Detail — 저역 세밀 16밴드",
        ])
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        ml.addWidget(self.combo_mode)
        ml.addWidget(QLabel(
            "Bass 반응: bass 에너지로 전체 밝기 제어 + mid/high 색상 변조\n"
            "Spectrum: 16개 주파수 밴드별 밝기 (20Hz~16kHz)\n"
            "Bass Detail: 20~500Hz 저역만 16밴드 세밀 분할\n"
            "※ 모드 전환 시 파라미터가 각 모드의 저장값으로 전환됩니다"
        ))
        layout.addWidget(mode_group)

        # === ★ 색상 소스 ===
        source_group = QGroupBox("색상 소스")
        src_layout = QVBoxLayout(source_group)

        self.combo_color_source = QComboBox()
        self.combo_color_source.addItems([
            "🎨 단색 / 무지개",
            "🖥 화면 연동 — 화면색 + 오디오 밝기",
        ])
        self.combo_color_source.currentIndexChanged.connect(self._on_color_source_changed)
        src_layout.addWidget(self.combo_color_source)

        # 구역 수 (화면 구역 모드에서만 표시)
        self.zone_count_row = QWidget()
        zcr = QHBoxLayout(self.zone_count_row)
        zcr.setContentsMargins(0, 0, 0, 0)
        zcr.addWidget(QLabel("구역 수:"))
        self.combo_zone_count = QComboBox()
        for n, label in ZONE_OPTIONS:
            self.combo_zone_count.addItem(label, n)
        self.combo_zone_count.currentIndexChanged.connect(self._on_zone_count_changed)
        zcr.addWidget(self.combo_zone_count)
        zcr.addStretch()
        src_layout.addWidget(self.zone_count_row)
        self.zone_count_row.setVisible(False)

        # 화면 색상 프리뷰
        preview_row = QHBoxLayout()
        self.btn_preview = QPushButton("👁 프리뷰 보기")
        self.btn_preview.setCheckable(True)
        self.btn_preview.setChecked(False)
        self.btn_preview.setFixedWidth(120)
        self.btn_preview.setStyleSheet(
            "QPushButton{background:#34495e;color:#bdc3c7;border-radius:4px;"
            "padding:4px;font-size:11px;}"
            "QPushButton:checked{background:#2980b9;color:white;}"
        )
        self.btn_preview.toggled.connect(self._on_preview_toggled)
        preview_row.addWidget(self.btn_preview)
        preview_row.addStretch()
        src_layout.addLayout(preview_row)
        self.btn_preview.setVisible(False)

        self.monitor_preview = MonitorPreview(self.config)
        src_layout.addWidget(self.monitor_preview)
        self.monitor_preview.setVisible(False)
        self._preview_active = False

        src_layout.addWidget(QLabel(
            "단색/무지개: 아래 색상 팔레트에서 선택\n"
            "화면 연동: 화면 색상 + 오디오 에너지로 밝기 제어\n"
            "  1구역 = 화면 전체 평균, 4구역 = 상하좌우 등"
        ))

        layout.addWidget(source_group)

        # === 색상 (단색 모드 전용) ===
        self.color_group = QGroupBox("색상")
        cl = QVBoxLayout(self.color_group)
        pg = QGridLayout()
        for i, (name, r, g, b) in enumerate(COLOR_PRESETS):
            btn = QPushButton(name)
            btn.setMinimumHeight(26)
            if r is None:
                btn.setStyleSheet(
                    "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                    "stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,"
                    "stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
                    "color:white;font-weight:bold;border-radius:4px;font-size:11px;"
                )
                btn.clicked.connect(lambda _: self._set_rainbow())
            else:
                tc = "#000" if (r + g + b) > 380 else "#fff"
                btn.setStyleSheet(
                    f"background:rgb({r},{g},{b});color:{tc};"
                    f"font-weight:bold;border-radius:4px;font-size:11px;"
                )
                btn.clicked.connect(lambda _, rgb=(r, g, b): self._set_color(*rgb))
            pg.addWidget(btn, i // 5, i % 5)
        cl.addLayout(pg)

        cr = QHBoxLayout()
        self.btn_custom = QPushButton("🎨 커스텀")
        self.btn_custom.clicked.connect(self._pick_custom_color)
        cr.addWidget(self.btn_custom)
        self.color_preview = QFrame()
        self.color_preview.setFixedSize(40, 26)
        self._is_rainbow = True
        self.color_preview.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,"
            "stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
            "border:1px solid #555;border-radius:4px;"
        )
        cr.addWidget(self.color_preview)
        cr.addStretch()
        cl.addLayout(cr)
        layout.addWidget(self.color_group)

        # === 파라미터 ===
        param_group = QGroupBox("파라미터")
        pl = QVBoxLayout(param_group)

        edit_row = QHBoxLayout()
        self.btn_edit = QPushButton("🔒 파라미터 잠금됨 — 클릭하여 편집")
        self.btn_edit.setCheckable(True)
        self.btn_edit.setChecked(False)
        self.btn_edit.setStyleSheet(
            "QPushButton{background:#34495e;color:#bdc3c7;border-radius:4px;"
            "padding:6px;font-weight:bold;}"
            "QPushButton:checked{background:#2980b9;color:white;}"
        )
        self.btn_edit.toggled.connect(self._toggle_edit_mode)
        edit_row.addWidget(self.btn_edit)

        self.btn_reset = QPushButton("↩ 기본값")
        self.btn_reset.setFixedWidth(80)
        self.btn_reset.clicked.connect(self._reset_defaults)
        self.btn_reset.setEnabled(False)
        edit_row.addWidget(self.btn_reset)

        self.btn_save_params = QPushButton("💾 저장")
        self.btn_save_params.setFixedWidth(70)
        self.btn_save_params.clicked.connect(self._save_params)
        self.btn_save_params.setEnabled(False)
        edit_row.addWidget(self.btn_save_params)

        pl.addLayout(edit_row)

        # 감도
        self._spectrum_only_widgets = []

        self.label_sens = QLabel("감도 (대역별)")
        pl.addWidget(self.label_sens)
        self.slider_bass_sens, self.label_bass_sens = self._add_param_slider(
            pl, "Bass:", 10, 300, 100, self._on_sens_changed
        )

        self.row_mid_sens = QWidget()
        row_m = QHBoxLayout(self.row_mid_sens)
        row_m.setContentsMargins(0, 0, 0, 0)
        row_m.addWidget(QLabel("Mid:"))
        self.slider_mid_sens = NoScrollSlider(Qt.Horizontal)
        self.slider_mid_sens.setRange(10, 300)
        self.slider_mid_sens.setValue(100)
        self.slider_mid_sens.valueChanged.connect(self._on_sens_changed)
        row_m.addWidget(self.slider_mid_sens)
        self.label_mid_sens = QLabel("1.00")
        self.label_mid_sens.setMinimumWidth(40)
        self.label_mid_sens.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_m.addWidget(self.label_mid_sens)
        pl.addWidget(self.row_mid_sens)
        self._all_sliders.append(self.slider_mid_sens)
        self._spectrum_only_widgets.append(self.row_mid_sens)

        self.row_high_sens = QWidget()
        row_h = QHBoxLayout(self.row_high_sens)
        row_h.setContentsMargins(0, 0, 0, 0)
        row_h.addWidget(QLabel("High:"))
        self.slider_high_sens = NoScrollSlider(Qt.Horizontal)
        self.slider_high_sens.setRange(10, 300)
        self.slider_high_sens.setValue(100)
        self.slider_high_sens.valueChanged.connect(self._on_sens_changed)
        row_h.addWidget(self.slider_high_sens)
        self.label_high_sens = QLabel("1.00")
        self.label_high_sens.setMinimumWidth(40)
        self.label_high_sens.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_h.addWidget(self.label_high_sens)
        pl.addWidget(self.row_high_sens)
        self._all_sliders.append(self.slider_high_sens)
        self._spectrum_only_widgets.append(self.row_high_sens)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        pl.addWidget(line)

        self.slider_brightness, self.label_brightness = self._add_param_slider(
            pl, "밝기:", 0, 100, 100,
            self._on_brightness_changed, suffix="%"
        )

        pl.addWidget(QLabel("반응 특성"))
        self.slider_attack, self.label_attack = self._add_param_slider(
            pl, "Attack:", 0, 100, 50, self._on_ar_changed
        )
        self.slider_release, self.label_release = self._add_param_slider(
            pl, "Release:", 0, 100, 50, self._on_ar_changed
        )

        hint = QLabel("Attack ↑ = 빠르게 반응 (펀치감)  |  Release ↑ = 긴 잔향 (여운)")
        hint.setStyleSheet("color:#888;font-size:10px;")
        hint.setWordWrap(True)
        pl.addWidget(hint)

        # 대역 비율 — spectrum/bass_detail 전용
        self.zone_line = QFrame()
        self.zone_line.setFrameShape(QFrame.HLine)
        self.zone_line.setFrameShadow(QFrame.Sunken)
        pl.addWidget(self.zone_line)
        self._spectrum_only_widgets.append(self.zone_line)

        self.zone_label = QLabel("대역 비율 (Spectrum 색상·주파수 분배)")
        self.zone_label.setStyleSheet("font-weight:bold;")
        pl.addWidget(self.zone_label)
        self._spectrum_only_widgets.append(self.zone_label)

        self.zone_desc = QLabel(
            "각 대역이 LED 둘레에서 차지하는 비율. "
            "Bass ↑ → 하단에서 빨강~노랑 영역 확대"
        )
        self.zone_desc.setStyleSheet("color:#888;font-size:10px;")
        self.zone_desc.setWordWrap(True)
        pl.addWidget(self.zone_desc)
        self._spectrum_only_widgets.append(self.zone_desc)

        self.zone_balance = ZoneBalanceWidget(33, 33, 34)
        self.zone_balance.zone_changed.connect(self._on_zone_changed)
        self._all_sliders.extend(self.zone_balance._sliders.values())
        pl.addWidget(self.zone_balance)
        self._spectrum_only_widgets.append(self.zone_balance)

        layout.addWidget(param_group)

        # === 오디오 디바이스 ===
        audio_group = QGroupBox("오디오 디바이스")
        al = QVBoxLayout(audio_group)
        dr = QHBoxLayout()
        self.combo_device = QComboBox()
        self._refresh_devices()
        dr.addWidget(self.combo_device)
        btn_refresh = QPushButton("🔄")
        btn_refresh.setFixedWidth(36)
        btn_refresh.clicked.connect(self._refresh_devices)
        dr.addWidget(btn_refresh)
        al.addLayout(dr)
        al.addWidget(QLabel("WASAPI Loopback 디바이스를 사용합니다."))
        layout.addWidget(audio_group)
        layout.addStretch()

        self._set_sliders_enabled(False)
        self._update_mode_ui("pulse")

    # ── 슬라이더 헬퍼 ─────────────────────────────────────────────

    def _add_param_slider(self, parent_layout, label_text, min_v, max_v,
                          default, callback, suffix=""):
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        slider = NoScrollSlider(Qt.Horizontal)
        slider.setRange(min_v, max_v)
        slider.setValue(default)
        slider.valueChanged.connect(callback)
        row.addWidget(slider)
        if suffix == "%":
            lbl = QLabel(f"{default}{suffix}")
        else:
            lbl = QLabel(f"{default / 100:.2f}")
        lbl.setMinimumWidth(40)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(lbl)
        parent_layout.addLayout(row)
        self._all_sliders.append(slider)
        return slider, lbl

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

    # ── 편집 모드 ─────────────────────────────────────────────────

    def _toggle_edit_mode(self, checked):
        self._edit_mode = checked
        self._set_sliders_enabled(checked)
        self.btn_reset.setEnabled(checked)
        self.btn_save_params.setEnabled(checked)
        if checked:
            self.btn_edit.setText("🔓 편집 중 — 클릭하여 잠금")
        else:
            self.btn_edit.setText("🔒 파라미터 잠금됨 — 클릭하여 편집")

    def _set_sliders_enabled(self, enabled):
        for slider in self._all_sliders:
            slider.setEnabled(enabled)
        self.zone_balance.setEnabled(enabled)

    def _reset_defaults(self):
        defaults = MODE_DEFAULTS.get(self._current_mode_key, MODE_DEFAULTS["pulse"])
        self._switching_mode = True
        self.slider_bass_sens.setValue(defaults["bass_sens"])
        self.slider_mid_sens.setValue(defaults["mid_sens"])
        self.slider_high_sens.setValue(defaults["high_sens"])
        self.slider_brightness.setValue(defaults["brightness"])
        self.slider_attack.setValue(defaults["attack"])
        self.slider_release.setValue(defaults["release"])
        self.zone_balance.set_values(
            defaults["zone_bass"], defaults["zone_mid"], defaults["zone_high"]
        )
        self._switching_mode = False
        self._apply_params_to_visualizer()

    def _save_params(self):
        self._save_current_params_to_mode(self._current_mode_key)
        save_config(self.config)
        mode_labels = {"pulse": "Bass 반응", "spectrum": "Spectrum", "bass_detail": "Bass Detail"}
        mode_label = mode_labels.get(self._current_mode_key, self._current_mode_key)
        QMessageBox.information(self, "저장", f"{mode_label} 모드 설정이 저장되었습니다.")

    # ── 디바이스 ──────────────────────────────────────────────────

    def _refresh_devices(self):
        self.combo_device.clear()
        self.combo_device.addItem("자동 (기본 출력 디바이스)", None)
        for idx, name, sr, ch in list_loopback_devices():
            self.combo_device.addItem(f"{name} ({sr}Hz, {ch}ch)", idx)

    # ── ★ 색상 소스 UI 콜백 ──────────────────────────────────────

    def _get_color_source(self):
        idx = self.combo_color_source.currentIndex()
        return [COLOR_SOURCE_SOLID, COLOR_SOURCE_SCREEN][idx]

    def _get_zone_count(self):
        return self.combo_zone_count.currentData() or 4

    def _on_color_source_changed(self, idx):
        source = self._get_color_source()
        is_screen = (source == COLOR_SOURCE_SCREEN)

        self.color_group.setVisible(not is_screen)
        self.zone_count_row.setVisible(is_screen)
        self.btn_preview.setVisible(is_screen)
        if not is_screen:
            self.monitor_preview.setVisible(False)
            self._preview_active = False
            self.btn_preview.setChecked(False)

        if self._visualizer:
            n_zones = self._get_zone_count() if is_screen else 4
            self._visualizer.set_color_source(source, n_zones=n_zones)

    def _on_zone_count_changed(self, idx):
        n_zones = self._get_zone_count()

        if n_zones != N_ZONES_PER_LED:
            zone_map = _build_led_zone_map_by_side(self.config, n_zones)
            self.monitor_preview.set_zone_map(zone_map)
        self.monitor_preview.set_n_zones(n_zones)

        if self._visualizer and self._get_color_source() == COLOR_SOURCE_SCREEN:
            self._visualizer.set_color_source(COLOR_SOURCE_SCREEN, n_zones=n_zones)

    def _on_screen_colors_updated(self, colors):
        if self._preview_active:
            self.monitor_preview.set_colors(colors)

    def _on_preview_toggled(self, checked):
        self._preview_active = checked
        self.monitor_preview.setVisible(checked)
        if checked:
            self.btn_preview.setText("👁 프리뷰 숨기기")
            n_zones = self._get_zone_count()
            if n_zones != N_ZONES_PER_LED:
                zone_map = _build_led_zone_map_by_side(self.config, n_zones)
                self.monitor_preview.set_zone_map(zone_map)
            self.monitor_preview.set_n_zones(n_zones)
        else:
            self.btn_preview.setText("👁 프리뷰 보기")

    # ── 시작/중지 ────────────────────────────────────────────────

    def _start_visualizer(self):
        if self._is_running:
            return
        self.request_mirror_stop.emit()

        device_idx = self.combo_device.currentData()

        try:
            self._visualizer = HybridVisualizer(self.config, device_index=device_idx)
        except Exception as e:
            QMessageBox.warning(self, "초기화 실패", str(e))
            return

        source = self._get_color_source()
        n_zones = self._get_zone_count()
        self._visualizer.color_source = source
        self._visualizer.n_zones = n_zones

        if self._is_rainbow:
            self._visualizer.set_rainbow(True)
        else:
            r, g, b = self._current_color
            self._visualizer.set_color(r, g, b)

        self._visualizer.mode = self._get_current_mode()
        self._apply_params_to_visualizer()

        self._visualizer.fps_updated.connect(self._on_fps)
        self._visualizer.energy_updated.connect(self._on_energy)
        self._visualizer.spectrum_updated.connect(self._on_spectrum)
        self._visualizer.status_changed.connect(self._on_status)
        self._visualizer.error.connect(self._on_error)
        self._visualizer.finished.connect(self._on_finished)
        self._visualizer.screen_colors_updated.connect(self._on_screen_colors_updated)

        self._visualizer.start()
        self._set_running(True)

    def _stop_visualizer(self):
        if self._visualizer and self._visualizer.isRunning():
            self._visualizer.stop_visualizer()
            self.status_label.setText("중지 중...")

    def stop_visualizer_sync(self):
        if self._visualizer and self._visualizer.isRunning():
            self._visualizer.stop_visualizer()
            self._visualizer.wait(2000)

    def _set_running(self, running):
        self._is_running = running
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.combo_device.setEnabled(not running)
        if not running:
            self._decay_timer.start()
        else:
            self._decay_timer.stop()

    # ── 시그널 핸들러 ─────────────────────────────────────────────

    def _on_fps(self, fps):
        self.fps_label.setText(f"{fps:.1f} fps")

    def _on_energy(self, bass, mid, high):
        self.bar_bass.setValue(int(bass * 100))
        self.bar_mid.setValue(int(mid * 100))
        self.bar_high.setValue(int(high * 100))

    def _on_spectrum(self, spec):
        self.spectrum_widget.set_values(spec)

    def _on_status(self, text):
        self.status_label.setText(text)

    def _on_error(self, msg, severity):
        if severity == "critical":
            QMessageBox.warning(self, "오류", msg)
        else:
            self.status_label.setText(f"⚠ {msg}")

    def _on_finished(self):
        self._set_running(False)
        self.fps_label.setText("— fps")
        self._visualizer = None

    def _decay_levels(self):
        b, m, h = self.bar_bass.value(), self.bar_mid.value(), self.bar_high.value()
        if b <= 0 and m <= 0 and h <= 0:
            self._decay_timer.stop()
            self.spectrum_widget.set_values(np.zeros(16))
            return
        self.bar_bass.setValue(max(0, b - 3))
        self.bar_mid.setValue(max(0, m - 3))
        self.bar_high.setValue(max(0, h - 3))
        self.spectrum_widget.set_values(self.spectrum_widget._values * 0.9)

    def _update_resource_usage(self):
        try:
            cpu = self._process.cpu_percent() / psutil.cpu_count()
            ram_mb = self._process.memory_info().rss / (1024 * 1024)
            self.cpu_label.setText(f"CPU: {cpu:.1f}%")
            self.ram_label.setText(f"RAM: {ram_mb:.0f} MB")
            color = "#c0392b" if cpu >= 20 else "#e67e22" if cpu >= 10 else "#d35400"
            self.cpu_label.setStyleSheet(
                f"font-size:12px;color:{color};margin-right:6px;"
            )
        except Exception:
            pass

    # ── ★ 모드별 UI 표시 (3모드 대응) ──────────────────────────────

    def _update_mode_ui(self, mode_name):
        # spectrum과 bass_detail 모두 대역 비율/감도 위젯 표시
        is_banded = (mode_name in ("spectrum", "bass_detail"))
        for w in self._spectrum_only_widgets:
            w.setVisible(is_banded)
        if mode_name == "bass_detail":
            self.label_sens.setText("감도 (Bass Detail)")
        elif mode_name == "spectrum":
            self.label_sens.setText("감도 (대역별)")
        else:
            self.label_sens.setText("감도 (Bass)")

    # ── ★ 모드 전환 (3모드 대응) ──────────────────────────────────

    def _on_mode_changed(self, idx):
        new_mode = ["pulse", "spectrum", "bass_detail"][idx]
        if new_mode == self._current_mode_key:
            return

        self._save_current_params_to_mode(self._current_mode_key)
        self._load_mode_params(new_mode)
        self._update_mode_ui(new_mode)

        if self._visualizer:
            self._visualizer.set_mode(self._get_current_mode())
            self._apply_params_to_visualizer()

    def _get_current_mode(self):
        return [MODE_PULSE, MODE_SPECTRUM, MODE_BASS_DETAIL][self.combo_mode.currentIndex()]

    # ── 색상 (단색 모드) ──────────────────────────────────────────

    def _set_color(self, r, g, b):
        self._current_color = (r, g, b)
        self._is_rainbow = False
        self.color_preview.setStyleSheet(
            f"background:rgb({r},{g},{b});border:1px solid #555;border-radius:4px;"
        )
        if self._visualizer:
            self._visualizer.set_color(r, g, b)

    def _set_rainbow(self):
        self._is_rainbow = True
        self.color_preview.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,"
            "stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
            "border:1px solid #555;border-radius:4px;"
        )
        if self._visualizer:
            self._visualizer.set_rainbow(True)

    def _pick_custom_color(self):
        r, g, b = self._current_color
        c = QColorDialog.getColor(QColor(r, g, b), self, "기본 색상")
        if c.isValid():
            self._set_color(c.red(), c.green(), c.blue())

    # ── 파라미터 콜백 ─────────────────────────────────────────────

    def _on_sens_changed(self, _=None):
        bv = self.slider_bass_sens.value() / 100.0
        mv = self.slider_mid_sens.value() / 100.0
        hv = self.slider_high_sens.value() / 100.0
        self.label_bass_sens.setText(f"{bv:.2f}")
        self.label_mid_sens.setText(f"{mv:.2f}")
        self.label_high_sens.setText(f"{hv:.2f}")
        if not self._switching_mode and self._visualizer:
            self._visualizer.bass_sensitivity = bv
            self._visualizer.mid_sensitivity = mv
            self._visualizer.high_sensitivity = hv

    def _on_brightness_changed(self, value):
        self.label_brightness.setText(f"{value}%")
        if not self._switching_mode and self._visualizer:
            self._visualizer.brightness = value / 100.0

    def _on_ar_changed(self, _=None):
        atk = self.slider_attack.value() / 100.0
        rel = self.slider_release.value() / 100.0
        self.label_attack.setText(f"{atk:.2f}")
        self.label_release.setText(f"{rel:.2f}")
        if not self._switching_mode and self._visualizer:
            self._visualizer.attack = atk
            self._visualizer.release = rel

    def _on_zone_changed(self, bass, mid, high):
        self.spectrum_widget.set_zone_weights(bass, mid, high)
        if not self._switching_mode and self._visualizer:
            self._visualizer.set_zone_weights(bass, mid, high)

    def cleanup(self):
        self._save_current_params_to_mode(self._current_mode_key)
        self.stop_visualizer_sync()
