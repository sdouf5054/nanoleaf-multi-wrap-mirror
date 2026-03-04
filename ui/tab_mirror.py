"""미러링 탭 — 시작/중지, 밝기, 스무딩, FPS 표시 + 자원 모니터링

[변경 사항 v2]
- 미러링 중 감쇠 반경, 타원 페널티, 변별 값, 스무딩 계수 실시간 변경 가능
- layout_params_changed 시그널: MainWindow가 MirrorThread에 전달
- smoothing_factor_changed 시그널: 스무딩 계수 실시간 반영
- 디바운스 타이머: 슬라이더/스핀박스 조작 중 재계산 폭주 방지 (300ms)
"""

import os
import psutil

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider,
    QLabel, QGroupBox, QCheckBox, QComboBox, QSpinBox,
    QDoubleSpinBox
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal


class MirrorTab(QWidget):

    # ★ 레이아웃 파라미터 변경 시그널 — MainWindow가 MirrorThread에 전달
    layout_params_changed = pyqtSignal(dict)
    # ★ 스무딩 계수 변경 시그널
    smoothing_factor_changed = pyqtSignal(float)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.mirror_cfg = config["mirror"]
        self._is_running = False  # ★ 미러링 상태 추적

        self._build_ui()

        # ★ 디바운스 타이머 — 레이아웃 파라미터 변경 시 300ms 대기 후 시그널 발생
        self._layout_debounce = QTimer(self)
        self._layout_debounce.setSingleShot(True)
        self._layout_debounce.setInterval(300)
        self._layout_debounce.timeout.connect(self._emit_layout_params)

        # ★ 자원 모니터링 타이머 (2초 주기)
        self._process = psutil.Process(os.getpid())
        self._process.cpu_percent()  # 첫 호출은 0 반환 — 기준값 초기화용
        self._res_timer = QTimer(self)
        self._res_timer.timeout.connect(self._update_resource_usage)
        self._res_timer.start(2000)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # === 상태 표시 ===
        status_group = QGroupBox("상태")
        status_layout = QHBoxLayout(status_group)

        self.status_label = QLabel("대기 중")
        self.status_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        status_layout.addWidget(self.status_label)

        status_layout.addStretch()

        # CPU 사용량
        self.cpu_label = QLabel("CPU: --%")
        self.cpu_label.setStyleSheet(
            "font-size: 12px; color: #d35400; margin-right: 6px;"
        )
        status_layout.addWidget(self.cpu_label)

        # RAM 사용량
        self.ram_label = QLabel("RAM: -- MB")
        self.ram_label.setStyleSheet(
            "font-size: 12px; color: #27ae60; margin-right: 10px;"
        )
        status_layout.addWidget(self.ram_label)

        self.fps_label = QLabel("— fps")
        self.fps_label.setStyleSheet("font-size: 14px; color: #888;")
        status_layout.addWidget(self.fps_label)

        layout.addWidget(status_group)

        # === 제어 버튼 ===
        btn_layout = QHBoxLayout()

        self.btn_start = QPushButton("▶ 시작")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.setStyleSheet("""
            QPushButton { background-color: #2d8c46; color: white;
                          font-size: 14px; font-weight: bold; border-radius: 6px; }
            QPushButton:hover { background-color: #35a352; }
        """)
        btn_layout.addWidget(self.btn_start)

        self.btn_pause = QPushButton("⏸ 일시정지")
        self.btn_pause.setMinimumHeight(40)
        self.btn_pause.setEnabled(False)
        btn_layout.addWidget(self.btn_pause)

        self.btn_stop = QPushButton("⏹ 중지")
        self.btn_stop.setMinimumHeight(40)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("""
            QPushButton { background-color: #c0392b; color: white;
                          font-size: 14px; font-weight: bold; border-radius: 6px; }
            QPushButton:hover { background-color: #e74c3c; }
            QPushButton:disabled { background-color: #666; }
        """)
        btn_layout.addWidget(self.btn_stop)

        layout.addLayout(btn_layout)

        # === 밝기 ===
        bright_group = QGroupBox("밝기")
        bright_layout = QHBoxLayout(bright_group)

        self.brightness_slider = QSlider(Qt.Horizontal)
        self.brightness_slider.setRange(0, 100)
        self.brightness_slider.setValue(int(self.mirror_cfg["brightness"] * 100))
        self.brightness_slider.setTickPosition(QSlider.TicksBelow)
        self.brightness_slider.setTickInterval(25)
        bright_layout.addWidget(self.brightness_slider)

        self.brightness_label = QLabel(f'{int(self.mirror_cfg["brightness"] * 100)}%')
        self.brightness_label.setMinimumWidth(45)
        self.brightness_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bright_layout.addWidget(self.brightness_label)

        self.brightness_slider.valueChanged.connect(self._on_brightness_changed)
        layout.addWidget(bright_group)

        # === 옵션 ===
        opt_group = QGroupBox("옵션")
        opt_layout = QVBoxLayout(opt_group)

        # 스무딩
        smooth_row = QHBoxLayout()
        self.chk_smoothing = QCheckBox("스무딩")
        self.chk_smoothing.setChecked(True)
        smooth_row.addWidget(self.chk_smoothing)

        smooth_row.addWidget(QLabel("계수:"))
        self.spin_smoothing = QDoubleSpinBox()
        self.spin_smoothing.setRange(0.0, 0.95)
        self.spin_smoothing.setSingleStep(0.05)
        self.spin_smoothing.setValue(self.mirror_cfg["smoothing_factor"])
        # ★ 스무딩 계수 변경 시 실시간 반영
        self.spin_smoothing.valueChanged.connect(self._on_smoothing_factor_changed)
        smooth_row.addWidget(self.spin_smoothing)
        smooth_row.addStretch()
        opt_layout.addLayout(smooth_row)

        # Target FPS
        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("Target FPS:"))
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(10, 60)
        self.spin_fps.setValue(self.mirror_cfg["target_fps"])
        fps_row.addWidget(self.spin_fps)
        fps_row.addStretch()
        opt_layout.addLayout(fps_row)

        # 감쇠
        decay_row = QHBoxLayout()
        decay_row.addWidget(QLabel("감쇠 반경:"))
        self.spin_decay = QDoubleSpinBox()
        self.spin_decay.setRange(0.05, 1.0)
        self.spin_decay.setSingleStep(0.05)
        self.spin_decay.setValue(self.mirror_cfg["decay_radius"])
        # ★ 실시간 반영 연결
        self.spin_decay.valueChanged.connect(self._on_layout_param_changed)
        decay_row.addWidget(self.spin_decay)

        decay_row.addWidget(QLabel("타원 페널티:"))
        self.spin_penalty = QDoubleSpinBox()
        self.spin_penalty.setRange(1.0, 10.0)
        self.spin_penalty.setSingleStep(0.5)
        self.spin_penalty.setValue(self.mirror_cfg["parallel_penalty"])
        # ★ 실시간 반영 연결
        self.spin_penalty.valueChanged.connect(self._on_layout_param_changed)
        decay_row.addWidget(self.spin_penalty)
        decay_row.addStretch()
        opt_layout.addLayout(decay_row)

        # 변별 오버라이드
        self.chk_per_side = QCheckBox("변별 값 사용")
        per_decay = self.mirror_cfg.get("decay_radius_per_side", {})
        per_penalty = self.mirror_cfg.get("parallel_penalty_per_side", {})
        has_per_side = bool(per_decay or per_penalty)
        self.chk_per_side.setChecked(has_per_side)
        # ★ 체크박스 변경 시에도 디바운스
        self.chk_per_side.stateChanged.connect(self._on_layout_param_changed)
        opt_layout.addWidget(self.chk_per_side)

        from PyQt5.QtWidgets import QGridLayout
        self.per_side_grid = QGridLayout()
        sides = ["top", "bottom", "left", "right"]
        side_labels = {"top": "상단", "bottom": "하단", "left": "좌측", "right": "우측"}

        self.per_side_grid.addWidget(QLabel(""), 0, 0)
        self.per_side_grid.addWidget(QLabel("감쇠 반경"), 0, 1)
        self.per_side_grid.addWidget(QLabel("타원 페널티"), 0, 2)

        self.spin_decay_per = {}
        self.spin_penalty_per = {}
        for row_i, side in enumerate(sides, 1):
            self.per_side_grid.addWidget(QLabel(side_labels[side]), row_i, 0)

            sp_d = QDoubleSpinBox()
            sp_d.setRange(0.05, 1.0)
            sp_d.setSingleStep(0.05)
            sp_d.setValue(per_decay.get(side, self.mirror_cfg["decay_radius"]))
            # ★ 실시간 반영 연결
            sp_d.valueChanged.connect(self._on_layout_param_changed)
            self.spin_decay_per[side] = sp_d
            self.per_side_grid.addWidget(sp_d, row_i, 1)

            sp_p = QDoubleSpinBox()
            sp_p.setRange(1.0, 10.0)
            sp_p.setSingleStep(0.5)
            sp_p.setValue(per_penalty.get(side, self.mirror_cfg["parallel_penalty"]))
            # ★ 실시간 반영 연결
            sp_p.valueChanged.connect(self._on_layout_param_changed)
            self.spin_penalty_per[side] = sp_p
            self.per_side_grid.addWidget(sp_p, row_i, 2)

        self.per_side_widget = QWidget()
        self.per_side_widget.setLayout(self.per_side_grid)
        self.per_side_widget.setVisible(has_per_side)
        self.chk_per_side.stateChanged.connect(
            lambda s: self.per_side_widget.setVisible(bool(s))
        )
        opt_layout.addWidget(self.per_side_widget)

        # 화면 방향
        orient_row = QHBoxLayout()
        orient_row.addWidget(QLabel("화면 방향:"))
        self.combo_orientation = QComboBox()
        self.combo_orientation.addItems(["자동 감지", "가로 (Landscape)", "세로 (Portrait)"])
        orient_val = self.mirror_cfg.get("orientation", "auto")
        idx_map = {"auto": 0, "landscape": 1, "portrait": 2}
        self.combo_orientation.setCurrentIndex(idx_map.get(orient_val, 0))
        orient_row.addWidget(self.combo_orientation)

        orient_row.addWidget(QLabel("세로 회전:"))
        self.combo_rotation = QComboBox()
        self.combo_rotation.addItems(["시계방향 (CW)", "반시계방향 (CCW)"])
        rot_val = self.mirror_cfg.get("portrait_rotation", "cw")
        self.combo_rotation.setCurrentIndex(0 if rot_val == "cw" else 1)
        orient_row.addWidget(self.combo_rotation)
        orient_row.addStretch()
        opt_layout.addLayout(orient_row)

        layout.addWidget(opt_group)
        layout.addStretch()

    # ── 실시간 반영 슬롯 ──────────────────────────────────────────────

    def _on_brightness_changed(self, value):
        self.brightness_label.setText(f"{value}%")

    def _on_smoothing_factor_changed(self, value):
        """★ 스무딩 계수 변경 → 실시간 반영"""
        if self._is_running:
            self.smoothing_factor_changed.emit(value)

    def _on_layout_param_changed(self, _=None):
        """★ 감쇠/페널티/변별 값 변경 → 디바운스 후 시그널 발생"""
        if self._is_running:
            self._layout_debounce.start()  # 300ms 리셋

    def _emit_layout_params(self):
        """★ 디바운스 만료 — 현재 UI 값을 dict로 모아서 시그널 발생"""
        params = {
            "decay_radius": self.spin_decay.value(),
            "parallel_penalty": self.spin_penalty.value(),
        }
        if self.chk_per_side.isChecked():
            params["decay_per_side"] = {
                side: self.spin_decay_per[side].value()
                for side in self.spin_decay_per
            }
            params["penalty_per_side"] = {
                side: self.spin_penalty_per[side].value()
                for side in self.spin_penalty_per
            }
        else:
            params["decay_per_side"] = {}
            params["penalty_per_side"] = {}

        self.layout_params_changed.emit(params)

    # ── 자원 사용량 갱신 ──────────────────────────────────────────────

    def _update_resource_usage(self):
        try:
            cpu = self._process.cpu_percent() / psutil.cpu_count()
            ram_mb = self._process.memory_info().rss / (1024 * 1024)
            self.cpu_label.setText(f"CPU: {cpu:.1f}%")
            self.ram_label.setText(f"RAM: {ram_mb:.0f} MB")

            if cpu >= 20:
                color = "#c0392b"
            elif cpu >= 10:
                color = "#e67e22"
            else:
                color = "#d35400"
            self.cpu_label.setStyleSheet(
                f"font-size: 12px; color: {color}; margin-right: 6px;"
            )
        except Exception:
            pass

    # ── 공개 메서드 ──────────────────────────────────────────────────

    def get_brightness(self):
        return self.brightness_slider.value() / 100.0

    def get_smoothing_enabled(self):
        return self.chk_smoothing.isChecked()

    def get_smoothing_factor(self):
        return self.spin_smoothing.value()

    def apply_to_config(self):
        """현재 UI 값을 config dict에 반영"""
        self.mirror_cfg["brightness"] = self.get_brightness()
        self.mirror_cfg["smoothing_factor"] = self.get_smoothing_factor()
        self.mirror_cfg["target_fps"] = self.spin_fps.value()
        self.mirror_cfg["decay_radius"] = self.spin_decay.value()
        self.mirror_cfg["parallel_penalty"] = self.spin_penalty.value()

        # 변별 오버라이드
        if self.chk_per_side.isChecked():
            self.mirror_cfg["decay_radius_per_side"] = {
                side: self.spin_decay_per[side].value() for side in self.spin_decay_per
            }
            self.mirror_cfg["parallel_penalty_per_side"] = {
                side: self.spin_penalty_per[side].value() for side in self.spin_penalty_per
            }
        else:
            self.mirror_cfg["decay_radius_per_side"] = {}
            self.mirror_cfg["parallel_penalty_per_side"] = {}

        orient_map = {0: "auto", 1: "landscape", 2: "portrait"}
        self.mirror_cfg["orientation"] = orient_map.get(
            self.combo_orientation.currentIndex(), "auto"
        )
        self.mirror_cfg["portrait_rotation"] = (
            "cw" if self.combo_rotation.currentIndex() == 0 else "ccw"
        )

    def set_running_state(self, running):
        """미러링 시작/중지 시 UI 상태 전환

        ★ 감쇠/페널티/변별/스무딩 위젯은 미러링 중에도 활성 상태 유지
           Target FPS와 화면 방향만 미러링 중 비활성화 (재시작 필요)
        """
        self._is_running = running
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)

        # 재시작이 필요한 옵션만 비활성화
        self.spin_fps.setEnabled(not running)
        self.combo_orientation.setEnabled(not running)
        self.combo_rotation.setEnabled(not running)

        # ★ 실시간 반영 가능한 옵션은 항상 활성화
        # spin_decay, spin_penalty, chk_per_side, per_side_widget,
        # spin_smoothing 은 setEnabled() 호출하지 않음 → 항상 활성

    def update_fps(self, fps):
        self.fps_label.setText(f"{fps:.1f} fps")

    def update_status(self, text):
        self.status_label.setText(text)
