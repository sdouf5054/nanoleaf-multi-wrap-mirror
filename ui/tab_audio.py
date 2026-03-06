"""오디오 비주얼라이저 탭 v4

[변경 v4]
- 슬라이더 편집 모드: 기본 잠금, "편집" 버튼으로 활성화
- 기본값 복원 버튼
- 마우스 휠 스크롤로 의도치 않은 값 변경 방지 (focusPolicy)
- N밴드 스펙트럼 바 표시
- 대역별 감도, Attack/Release 독립 슬라이더
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider,
    QLabel, QGroupBox, QComboBox, QColorDialog, QProgressBar,
    QFrame, QGridLayout, QMessageBox, QScrollArea
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QPainter, QBrush
import numpy as np

from core.audio_engine import list_loopback_devices, HAS_PYAUDIO
from core.audio_visualizer import MODE_PULSE, MODE_SPECTRUM

COLOR_PRESETS = [
    ("🌈 무지개", None, None, None),  # 특수: LED 위치 기반 그래디언트
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

# 기본값 정의
DEFAULTS = {
    "bass_sens": 100,
    "mid_sens": 100,
    "high_sens": 100,
    "brightness": 100,
    "attack": 50,
    "release": 50,
}


class NoScrollSlider(QSlider):
    """마우스 휠 스크롤을 무시하는 슬라이더.

    스크롤 영역 안에서 슬라이더가 있을 때,
    스크롤하다가 의도치 않게 값이 바뀌는 것을 방지합니다.
    """

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        # 포커스가 없으면 휠 이벤트 무시 → 부모(스크롤 영역)로 전달
        if not self.hasFocus():
            event.ignore()
        else:
            super().wheelEvent(event)


class SpectrumWidget(QWidget):
    """N밴드 스펙트럼 바 시각화."""

    def __init__(self, n_bands=16, parent=None):
        super().__init__(parent)
        self.n_bands = n_bands
        self._values = np.zeros(n_bands)
        self.setMinimumHeight(50)
        self.setMaximumHeight(70)

    def set_values(self, values):
        self._values = np.clip(values, 0, 1)
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

            t = i / max(1, n - 1)
            # 무지개 7색 키포인트 보간
            kp = [
                (0.000, 255,   0,   0),
                (0.167, 255, 127,   0),
                (0.333, 255, 255,   0),
                (0.500,   0, 255,   0),
                (0.667,   0, 130, 255),
                (0.833,   0,   0, 255),
                (1.000, 148,   0, 211),
            ]
            r, g, b = 148, 0, 211
            for j in range(len(kp) - 1):
                t0, r0, g0, b0 = kp[j]
                t1, r1, g1, b1 = kp[j + 1]
                if t <= t1:
                    f = (t - t0) / (t1 - t0) if t1 > t0 else 0
                    r = int(r0 + (r1 - r0) * f)
                    g = int(g0 + (g1 - g0) * f)
                    b = int(b0 + (b1 - b0) * f)
                    break

            painter.setBrush(QBrush(QColor(r, g, b)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(x, y, bar_w, bar_h, 2, 2)
        painter.end()


class AudioTab(QWidget):
    """오디오 비주얼라이저 탭 v4."""

    request_mirror_stop = pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._visualizer = None
        self._is_running = False
        self._current_color = (255, 0, 80)
        self._edit_mode = False  # 파라미터 편집 모드

        self._all_sliders = []  # 잠금/해제 대상 슬라이더 목록

        self._build_ui()

        self._decay_timer = QTimer(self)
        self._decay_timer.setInterval(50)
        self._decay_timer.timeout.connect(self._decay_levels)

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

        # === 모드 ===
        mode_group = QGroupBox("비주얼라이저 모드")
        ml = QVBoxLayout(mode_group)
        self.combo_mode = QComboBox()
        self.combo_mode.addItems([
            "🔴 Bass 반응 — 저음 기반 전체 밝기",
            "🌈 Spectrum — 16밴드 주파수 매핑",
        ])
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        ml.addWidget(self.combo_mode)
        ml.addWidget(QLabel(
            "Bass 반응: bass 에너지로 전체 밝기 제어 + mid/high 색상 변조\n"
            "Spectrum: 16개 주파수 밴드별 밝기 (하단=저음, 상단=고음)\n"
            "※ 모든 모드에서 색상 팔레트가 적용됩니다"
        ))
        layout.addWidget(mode_group)

        # === 색상 ===
        color_group = QGroupBox("색상")
        cl = QVBoxLayout(color_group)
        pg = QGridLayout()
        for i, (name, r, g, b) in enumerate(COLOR_PRESETS):
            btn = QPushButton(name)
            btn.setMinimumHeight(26)
            if r is None:
                # 무지개 프리셋 — 그래디언트 배경
                btn.setStyleSheet(
                    "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                    "stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
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
        # 기본: 무지개
        self._is_rainbow = True
        self.color_preview.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
            "border:1px solid #555;border-radius:4px;"
        )
        cr.addWidget(self.color_preview)
        cr.addStretch()
        cl.addLayout(cr)
        layout.addWidget(color_group)

        # === 파라미터 (편집 모드 토글) ===
        param_group = QGroupBox("파라미터")
        pl = QVBoxLayout(param_group)

        # 편집/잠금 버튼 행
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
        pl.addLayout(edit_row)

        # 감도
        pl.addWidget(QLabel("감도 (대역별)"))
        self.slider_bass_sens, self.label_bass_sens = self._add_param_slider(
            pl, "Bass:", 10, 300, DEFAULTS["bass_sens"], self._on_sens_changed
        )
        self.slider_mid_sens, self.label_mid_sens = self._add_param_slider(
            pl, "Mid:", 10, 300, DEFAULTS["mid_sens"], self._on_sens_changed
        )
        self.slider_high_sens, self.label_high_sens = self._add_param_slider(
            pl, "High:", 10, 300, DEFAULTS["high_sens"], self._on_sens_changed
        )

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        pl.addWidget(line)

        # 밝기
        self.slider_brightness, self.label_brightness = self._add_param_slider(
            pl, "밝기:", 0, 100, DEFAULTS["brightness"],
            self._on_brightness_changed, suffix="%"
        )

        # Attack / Release
        pl.addWidget(QLabel("반응 특성"))
        self.slider_attack, self.label_attack = self._add_param_slider(
            pl, "Attack:", 0, 100, DEFAULTS["attack"], self._on_ar_changed
        )
        self.slider_release, self.label_release = self._add_param_slider(
            pl, "Release:", 0, 100, DEFAULTS["release"], self._on_ar_changed
        )

        hint = QLabel("Attack ↑ = 빠르게 반응 (펀치감)  |  Release ↑ = 긴 잔향 (여운)")
        hint.setStyleSheet("color:#888;font-size:10px;")
        hint.setWordWrap(True)
        pl.addWidget(hint)

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

        # 초기 상태: 슬라이더 잠금
        self._set_sliders_enabled(False)

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
        if checked:
            self.btn_edit.setText("🔓 편집 중 — 클릭하여 잠금")
        else:
            self.btn_edit.setText("🔒 파라미터 잠금됨 — 클릭하여 편집")

    def _set_sliders_enabled(self, enabled):
        for slider in self._all_sliders:
            slider.setEnabled(enabled)

    def _reset_defaults(self):
        self.slider_bass_sens.setValue(DEFAULTS["bass_sens"])
        self.slider_mid_sens.setValue(DEFAULTS["mid_sens"])
        self.slider_high_sens.setValue(DEFAULTS["high_sens"])
        self.slider_brightness.setValue(DEFAULTS["brightness"])
        self.slider_attack.setValue(DEFAULTS["attack"])
        self.slider_release.setValue(DEFAULTS["release"])

    # ── 디바이스 ──────────────────────────────────────────────────

    def _refresh_devices(self):
        self.combo_device.clear()
        self.combo_device.addItem("자동 (기본 출력 디바이스)", None)
        for idx, name, sr, ch in list_loopback_devices():
            self.combo_device.addItem(f"{name} ({sr}Hz, {ch}ch)", idx)

    # ── 시작/중지 ────────────────────────────────────────────────

    def _start_visualizer(self):
        if self._is_running:
            return
        self.request_mirror_stop.emit()

        from core.audio_visualizer import AudioVisualizer
        device_idx = self.combo_device.currentData()

        try:
            self._visualizer = AudioVisualizer(self.config, device_index=device_idx)
        except Exception as e:
            QMessageBox.warning(self, "초기화 실패", str(e))
            return

        # UI → 비주얼라이저 파라미터
        if self._is_rainbow:
            self._visualizer.set_rainbow(True)
        else:
            r, g, b = self._current_color
            self._visualizer.set_color(r, g, b)
        self._visualizer.brightness = self.slider_brightness.value() / 100.0
        self._visualizer.bass_sensitivity = self.slider_bass_sens.value() / 100.0
        self._visualizer.mid_sensitivity = self.slider_mid_sens.value() / 100.0
        self._visualizer.high_sensitivity = self.slider_high_sens.value() / 100.0
        self._visualizer.mode = self._get_current_mode()
        self._visualizer.attack = self.slider_attack.value() / 100.0
        self._visualizer.release = self.slider_release.value() / 100.0

        self._visualizer.fps_updated.connect(self._on_fps)
        self._visualizer.energy_updated.connect(self._on_energy)
        self._visualizer.spectrum_updated.connect(self._on_spectrum)
        self._visualizer.status_changed.connect(self._on_status)
        self._visualizer.error.connect(self._on_error)
        self._visualizer.finished.connect(self._on_finished)

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

    # ── 파라미터 ──────────────────────────────────────────────────

    def _on_mode_changed(self, idx):
        if self._visualizer:
            self._visualizer.set_mode(self._get_current_mode())

    def _get_current_mode(self):
        return [MODE_PULSE, MODE_SPECTRUM][self.combo_mode.currentIndex()]

    def _set_color(self, r, g, b):
        self._current_color = (r, g, b)
        self._is_rainbow = False
        self.color_preview.setStyleSheet(
            f"background:rgb({r},{g},{b});border:1px solid #555;border-radius:4px;"
        )
        if self._visualizer:
            self._visualizer.set_color(r, g, b)  # rainbow=False로 전환됨

    def _set_rainbow(self):
        self._is_rainbow = True
        self.color_preview.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);"
            "border:1px solid #555;border-radius:4px;"
        )
        if self._visualizer:
            self._visualizer.set_rainbow(True)

    def _pick_custom_color(self):
        r, g, b = self._current_color
        c = QColorDialog.getColor(QColor(r, g, b), self, "기본 색상")
        if c.isValid():
            self._set_color(c.red(), c.green(), c.blue())

    def _on_sens_changed(self, _=None):
        bv = self.slider_bass_sens.value() / 100.0
        mv = self.slider_mid_sens.value() / 100.0
        hv = self.slider_high_sens.value() / 100.0
        self.label_bass_sens.setText(f"{bv:.2f}")
        self.label_mid_sens.setText(f"{mv:.2f}")
        self.label_high_sens.setText(f"{hv:.2f}")
        if self._visualizer:
            self._visualizer.bass_sensitivity = bv
            self._visualizer.mid_sensitivity = mv
            self._visualizer.high_sensitivity = hv

    def _on_brightness_changed(self, value):
        self.label_brightness.setText(f"{value}%")
        if self._visualizer:
            self._visualizer.brightness = value / 100.0

    def _on_ar_changed(self, _=None):
        atk = self.slider_attack.value() / 100.0
        rel = self.slider_release.value() / 100.0
        self.label_attack.setText(f"{atk:.2f}")
        self.label_release.setText(f"{rel:.2f}")
        if self._visualizer:
            self._visualizer.attack = atk
            self._visualizer.release = rel

    def cleanup(self):
        self.stop_visualizer_sync()
