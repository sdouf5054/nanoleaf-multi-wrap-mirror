"""색상 보정 탭 — 화이트밸런스, 감마, 채널 믹싱 + 실시간 LED 프리뷰 (PySide6)"""

import numpy as np
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QSlider, QGridLayout, QFrame, QMessageBox, QScrollArea,
)
from PySide6.QtCore import Qt, Signal

from core.config import save_config

TEST_COLORS = [
    ("흰색", 255, 255, 255), ("빨강", 255, 0, 0), ("초록", 0, 255, 0),
    ("파랑", 0, 0, 255), ("노랑", 255, 255, 0), ("시안", 0, 255, 255),
    ("마젠타", 255, 0, 255), ("연두", 128, 255, 0), ("주황", 255, 128, 0),
    ("따뜻한 백", 255, 220, 180),
]
_OWNER = "color_tab"


class ColorSliderRow(QWidget):
    def __init__(self, label, min_val, max_val, default, decimals=2, parent=None):
        super().__init__(parent)
        self.decimals = decimals
        self.scale = 10 ** decimals
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label)
        self.label.setMinimumWidth(80)
        layout.addWidget(self.label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(min_val * self.scale), int(max_val * self.scale))
        self.slider.setValue(int(default * self.scale))
        self.slider.valueChanged.connect(self._on_changed)
        layout.addWidget(self.slider)
        self.value_label = QLabel(f"{default:.{decimals}f}")
        self.value_label.setMinimumWidth(50)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.value_label)

    def _on_changed(self, val):
        self.value_label.setText(f"{val / self.scale:.{self.decimals}f}")

    def value(self):
        return self.slider.value() / self.scale

    def setValue(self, v):
        self.slider.setValue(int(v * self.scale))


class ColorTab(QWidget):
    request_mirror_stop = Signal()

    def __init__(self, config, device_manager=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.color_cfg = config["color"]
        self.dm = device_manager
        self._current_test_rgb = (255, 255, 255)
        if self.dm:
            self.dm.force_released.connect(self._on_force_released)
        self._build_ui()

    def _build_ui(self):
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(10)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll.setWidget(container)

        # 연결 상태
        conn_layout = QHBoxLayout()
        self.btn_connect = QPushButton("LED 연결")
        self.btn_connect.clicked.connect(self._toggle_connection)
        conn_layout.addWidget(self.btn_connect)
        self.conn_label = QLabel("연결 안 됨")
        self.conn_label.setStyleSheet("color: #c0392b;")
        conn_layout.addWidget(self.conn_label)
        conn_layout.addStretch()
        layout.addLayout(conn_layout)

        # 화이트밸런스
        wb_group = QGroupBox("화이트밸런스")
        wb_layout = QVBoxLayout(wb_group)
        self.wb_r = ColorSliderRow("Red", 0.5, 1.5, self.color_cfg["wb_r"])
        self.wb_g = ColorSliderRow("Green", 0.5, 1.5, self.color_cfg["wb_g"])
        self.wb_b = ColorSliderRow("Blue", 0.5, 1.5, self.color_cfg["wb_b"])
        for s in (self.wb_r, self.wb_g, self.wb_b):
            wb_layout.addWidget(s)
            s.slider.valueChanged.connect(self._on_value_changed)
        layout.addWidget(wb_group)

        # 감마
        gamma_group = QGroupBox("감마")
        gamma_layout = QVBoxLayout(gamma_group)
        self.gamma_r = ColorSliderRow("Red", 0.5, 3.0, self.color_cfg["gamma_r"])
        self.gamma_g = ColorSliderRow("Green", 0.5, 3.0, self.color_cfg["gamma_g"])
        self.gamma_b = ColorSliderRow("Blue", 0.5, 3.0, self.color_cfg["gamma_b"])
        for s in (self.gamma_r, self.gamma_g, self.gamma_b):
            gamma_layout.addWidget(s)
            s.slider.valueChanged.connect(self._on_value_changed)
        layout.addWidget(gamma_group)

        # 채널 믹싱
        mix_group = QGroupBox("채널 믹싱 (비선형)")
        mix_layout = QVBoxLayout(mix_group)
        self.bleed = ColorSliderRow("Green→Red", 0.0, 1.5, self.color_cfg["green_red_bleed"])
        self.bleed.slider.valueChanged.connect(self._on_value_changed)
        mix_layout.addWidget(self.bleed)
        layout.addWidget(mix_group)

        # 테스트 색상
        test_group = QGroupBox("테스트 색상 (클릭하여 LED에 전송)")
        test_layout = QGridLayout(test_group)
        for i, (name, r, g, b) in enumerate(TEST_COLORS):
            btn = QPushButton(name)
            btn.setMinimumHeight(32)
            tc = "#000" if (r + g + b) > 380 else "#fff"
            btn.setStyleSheet(f"background-color:rgb({r},{g},{b});color:{tc};font-weight:bold;border-radius:4px;")
            btn.clicked.connect(lambda checked, rgb=(r, g, b): self._send_test_color(*rgb))
            test_layout.addWidget(btn, i // 5, i % 5)
        layout.addWidget(test_group)

        # 미리보기
        preview_layout = QHBoxLayout()
        preview_layout.addWidget(QLabel("입력:"))
        self.preview_input = QFrame()
        self.preview_input.setFixedSize(40, 40)
        self.preview_input.setStyleSheet("background-color:white;border:1px solid #ccc;")
        preview_layout.addWidget(self.preview_input)
        preview_layout.addWidget(QLabel("→ 보정 후:"))
        self.preview_output = QFrame()
        self.preview_output.setFixedSize(40, 40)
        self.preview_output.setStyleSheet("background-color:white;border:1px solid #ccc;")
        preview_layout.addWidget(self.preview_output)
        self.preview_label = QLabel("")
        preview_layout.addWidget(self.preview_label)
        preview_layout.addStretch()
        layout.addLayout(preview_layout)

        # 버튼
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("💾 설정 저장")
        btn_save.clicked.connect(self._save)
        btn_layout.addWidget(btn_save)
        btn_reset = QPushButton("↩ 기본값 복원")
        btn_reset.clicked.connect(self._reset_defaults)
        btn_layout.addWidget(btn_reset)
        btn_off = QPushButton("LED 끄기")
        btn_off.clicked.connect(self._turn_off_leds)
        btn_layout.addWidget(btn_off)
        layout.addLayout(btn_layout)

    def _set_connected_ui(self):
        self.conn_label.setText("연결됨 ✅")
        self.conn_label.setStyleSheet("color: #2d8c46;")
        self.btn_connect.setText("연결 해제")

    def _set_disconnected_ui(self):
        self.conn_label.setText("연결 안 됨")
        self.conn_label.setStyleSheet("color: #c0392b;")
        self.btn_connect.setText("LED 연결")

    def _on_force_released(self, prev_owner):
        if prev_owner == _OWNER:
            self._set_disconnected_ui()

    def force_disconnect(self):
        if self.dm:
            self.dm.release(_OWNER)
            self._set_disconnected_ui()

    def _toggle_connection(self):
        if not self.dm:
            return
        if self.dm.is_connected and self.dm.owner == _OWNER:
            self.dm.release(_OWNER)
            self._set_disconnected_ui()
        else:
            self.request_mirror_stop.emit()
            try:
                self.dm.acquire(_OWNER)
                self._set_connected_ui()
            except Exception as e:
                QMessageBox.warning(self, "연결 실패", str(e))

    def _apply_correction(self, r, g, b):
        rgb = np.array([[r, g, b]], dtype=np.float32)
        norm = rgb / 255.0
        norm[0, 0] = np.power(norm[0, 0], self.gamma_r.value())
        norm[0, 1] = np.power(norm[0, 1], self.gamma_g.value())
        norm[0, 2] = np.power(norm[0, 2], self.gamma_b.value())
        rgb = norm * 255.0
        R, G = rgb[0, 0], rgb[0, 1]
        R_add = max(0, G - R) * self.bleed.value()
        rgb[0, 0] = R + R_add
        rgb[0, 0] *= self.wb_r.value()
        rgb[0, 1] *= self.wb_g.value()
        rgb[0, 2] *= self.wb_b.value()
        rgb = np.clip(rgb, 0, 255)
        return int(rgb[0, 0]), int(rgb[0, 1]), int(rgb[0, 2])

    def _send_test_color(self, r, g, b):
        self._current_test_rgb = (r, g, b)
        self.preview_input.setStyleSheet(f"background-color:rgb({r},{g},{b});border:1px solid #ccc;")
        cr, cg, cb = self._apply_correction(r, g, b)
        self.preview_output.setStyleSheet(f"background-color:rgb({cr},{cg},{cb});border:1px solid #ccc;")
        self.preview_label.setText(f"({r},{g},{b}) → ({cr},{cg},{cb})")
        if self.dm and self.dm.is_connected and self.dm.owner == _OWNER:
            self.dm.device.set_all_color(cr, cg, cb)

    def _on_value_changed(self, _=None):
        r, g, b = self._current_test_rgb
        self._send_test_color(r, g, b)

    def _save(self):
        self.color_cfg["wb_r"] = self.wb_r.value()
        self.color_cfg["wb_g"] = self.wb_g.value()
        self.color_cfg["wb_b"] = self.wb_b.value()
        self.color_cfg["gamma_r"] = self.gamma_r.value()
        self.color_cfg["gamma_g"] = self.gamma_g.value()
        self.color_cfg["gamma_b"] = self.gamma_b.value()
        self.color_cfg["green_red_bleed"] = self.bleed.value()
        save_config(self.config)
        QMessageBox.information(self, "저장", "색상 설정이 저장되었습니다.")

    def _reset_defaults(self):
        self.wb_r.setValue(1.00); self.wb_g.setValue(0.85); self.wb_b.setValue(0.70)
        self.gamma_r.setValue(1.00); self.gamma_g.setValue(1.00); self.gamma_b.setValue(1.00)
        self.bleed.setValue(0.60)

    def _turn_off_leds(self):
        if self.dm and self.dm.is_connected and self.dm.owner == _OWNER:
            self.dm.device.turn_off()

    def cleanup(self):
        if self.dm:
            self.dm.release(_OWNER)
