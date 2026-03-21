"""LED 캘리브레이션 탭 — 코너 찾기 + 세그먼트 자동 생성 (PySide6)

[QSS 테마] objectName 기반 스타일링.
[Refactor] DeviceOwnerMixin 적용.
[★ 동적 바퀴 수] N_WRAPS를 config에서 읽고, UI 스핀박스로 변경 가능.
  - 코너 테이블 행 수가 바퀴 수에 맞게 동적 조절.
  - _load_from_config / _generate_segments가 임의 바퀴 수를 지원.
  - 모니터 크기 프리셋 힌트 제공.
"""

import time
import copy
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QSpinBox, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QTextEdit, QScrollArea, QFrame,
)
from PySide6.QtCore import Qt, QThread, Signal

from core.config import save_config
from ui.mixins.device_owner import DeviceOwnerMixin
from ui.dialogs import msg_info, msg_warning

_GROUP_MARGINS = (6, 6, 6, 6)
_GROUP_SPACING = 6

# 모니터 크기 → 권장 바퀴 수 힌트 (75개 LED 기준)
_MONITOR_PRESETS = [
    ("직접 설정", None),
    ("소형 (15~20인치) — 권장 3바퀴", 3),
    ("24인치 (약 60cm) — 권장 2바퀴", 2),
    ("27인치 (약 68cm) — 권장 2바퀴", 2),
    ("32인치 (약 80cm) — 권장 1바퀴", 1),
    ("34인치 울트라와이드 — 권장 1바퀴", 1),
]

# 시작 코너 위치 — sides_order + column_headers 매핑
# sides: 시작 코너에서 시계방향으로 4변의 순서
# corners: config에 저장되는 코너 약어 (시작→끝 순서)
# headers: 코너 테이블 열 헤더
_START_POSITIONS = {
    "좌하 (기본)": {
        "sides": ["left", "top", "right", "bottom"],
        "corners": ("bl", "tl", "tr", "br"),
        "headers": ["시작(좌하)", "좌상", "우상", "우하", "끝점"],
    },
    "좌상": {
        "sides": ["top", "right", "bottom", "left"],
        "corners": ("tl", "tr", "br", "bl"),
        "headers": ["시작(좌상)", "우상", "우하", "좌하", "끝점"],
    },
    "우상": {
        "sides": ["right", "bottom", "left", "top"],
        "corners": ("tr", "br", "bl", "tl"),
        "headers": ["시작(우상)", "우하", "좌하", "좌상", "끝점"],
    },
    "우하": {
        "sides": ["bottom", "left", "top", "right"],
        "corners": ("br", "bl", "tl", "tr"),
        "headers": ["시작(우하)", "좌하", "좌상", "우상", "끝점"],
    },
}

_START_POS_KEYS = list(_START_POSITIONS.keys())


class LedScanThread(QThread):
    led_changed = Signal(int)
    finished_scan = Signal()

    def __init__(self, device, led_count, delay_ms=500):
        super().__init__()
        self.device = device
        self.led_count = led_count
        self.delay_ms = delay_ms
        self._running = True
        self._current = 0
        self._paused = False
        self._step_request = 0
        self._jump_request = -1

    def run(self):
        self._current = 0
        time.sleep(0.1)
        try:
            while self._running and self._current < self.led_count:
                self._light_single(self._current)
                self.led_changed.emit(self._current)
                while self._running:
                    if self._jump_request != -1:
                        self._current = max(0, min(self._jump_request, self.led_count - 1))
                        self._jump_request = -1; break
                    if self._step_request != 0:
                        self._current += self._step_request
                        self._current = max(0, min(self._current, self.led_count - 1))
                        self._step_request = 0; break
                    if not self._paused:
                        time.sleep(self.delay_ms / 1000.0)
                        self._current += 1; break
                    time.sleep(0.05)
        except Exception:
            pass
        finally:
            if self.device and self.device.connected:
                try: self.device.turn_off()
                except Exception: pass
            self.finished_scan.emit()

    def _light_single(self, idx):
        if not self.device or not self.device.connected: return
        data = bytearray(self.led_count * 3)
        data[idx * 3] = 255; data[idx * 3 + 1] = 255; data[idx * 3 + 2] = 255
        self.device.send_rgb(bytes(data))

    def jump_to(self, idx): self._jump_request = idx
    def step_forward(self): self._step_request = 1
    def step_backward(self): self._step_request = -1
    def set_paused(self, paused): self._paused = paused
    def stop_scan(self): self._running = False


class SetupTab(DeviceOwnerMixin, QWidget):
    """LED 캘리브레이션 탭."""

    _DEVICE_OWNER = "setup_tab"
    request_mirror_stop = Signal()

    def __init__(self, config, device_manager=None, parent=None):
        QWidget.__init__(self, parent)
        self.config = config
        self.layout_cfg = config["layout"]
        self.scan_thread = None
        self._saved_layout = copy.deepcopy(config["layout"])
        self._saved_led_count = config["device"]["led_count"]

        # ★ 바퀴 수: config에서 읽기 (기본 2)
        self._n_wraps = self.layout_cfg.get("n_wraps", 2)
        # ★ 시작 위치
        saved_start = self.layout_cfg.get("start_position", "좌하 (기본)")
        self._start_position = saved_start if saved_start in _START_POS_KEYS else "좌하 (기본)"

        self._init_device_owner(device_manager)
        self._build_ui()
        self._load_from_config()

    def _on_device_force_released(self):
        self._stop_scan()

    def _build_ui(self):
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 6, 10, 6)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll.setWidget(container)

        # 연결
        conn_layout = QHBoxLayout()
        self.btn_connect = QPushButton("LED 연결")
        self.btn_connect.clicked.connect(self._toggle_connection)
        conn_layout.addWidget(self.btn_connect)
        self.conn_label = QLabel("연결 안 됨")
        self.conn_label.setObjectName("connLabel")
        from ui.mixins.device_owner import _set_property
        _set_property(self.conn_label, "connState", "disconnected")
        conn_layout.addWidget(self.conn_label)
        conn_layout.addStretch()
        layout.addLayout(conn_layout)

        # 기본 설정
        basic_group = QGroupBox("기본 설정")
        basic_layout = QVBoxLayout(basic_group)
        basic_layout.setContentsMargins(*_GROUP_MARGINS)
        basic_layout.setSpacing(_GROUP_SPACING)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("LED 수:"))
        self.spin_led_count = QSpinBox()
        self.spin_led_count.setRange(1, 300)
        self.spin_led_count.setValue(self.config["device"]["led_count"])
        row1.addWidget(self.spin_led_count)
        row1.addWidget(QLabel("감는 방향:"))
        self.combo_direction = QComboBox()
        self.combo_direction.addItems(["시계방향 (LED 번호 감소)", "반시계방향 (LED 번호 증가)"])
        row1.addWidget(self.combo_direction)
        row1.addStretch()
        basic_layout.addLayout(row1)

        # ★ 바퀴 수 + 모니터 프리셋
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("바퀴 수:"))
        self.spin_wraps = QSpinBox()
        self.spin_wraps.setRange(1, 5)
        self.spin_wraps.setValue(self._n_wraps)
        self.spin_wraps.setToolTip("LED 스트립이 모니터 둘레를 감는 횟수")
        self.spin_wraps.valueChanged.connect(self._on_wraps_changed)
        row2.addWidget(self.spin_wraps)
        row2.addWidget(QLabel("모니터 크기:"))
        self.combo_monitor = QComboBox()
        for label, _ in _MONITOR_PRESETS:
            self.combo_monitor.addItem(label)
        self.combo_monitor.currentIndexChanged.connect(self._on_monitor_preset_changed)
        row2.addWidget(self.combo_monitor)
        row2.addStretch()
        basic_layout.addLayout(row2)

        # ★ 시작 위치
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("시작 위치:"))
        self.combo_start_pos = QComboBox()
        for label in _START_POS_KEYS:
            self.combo_start_pos.addItem(label)
        # config에서 복원
        saved_start = self.layout_cfg.get("start_position", "좌하 (기본)")
        idx = _START_POS_KEYS.index(saved_start) if saved_start in _START_POS_KEYS else 0
        self._start_position = _START_POS_KEYS[idx]
        self.combo_start_pos.setCurrentIndex(idx)
        self.combo_start_pos.currentIndexChanged.connect(self._on_start_pos_changed)
        self.combo_start_pos.setToolTip(
            "LED 스트립이 시작되는 모니터 코너.\n"
            "선택한 코너부터 시계방향으로 코너를 순서대로 기록합니다."
        )
        row3.addWidget(self.combo_start_pos)
        row3.addStretch()
        basic_layout.addLayout(row3)

        layout.addWidget(basic_group)

        # LED 스캔
        scan_group = QGroupBox("LED 코너 찾기")
        scan_layout = QVBoxLayout(scan_group)
        scan_layout.setSpacing(_GROUP_SPACING)
        scan_layout.setContentsMargins(*_GROUP_MARGINS)
        scan_desc = QLabel("LED를 순차 점등하면서 각 코너의 LED 번호를 기록합니다.")
        scan_desc.setWordWrap(True)
        scan_layout.addWidget(scan_desc)

        ctrl_layout = QHBoxLayout()
        self.btn_scan_start = QPushButton("▶ 자동 스캔")
        self.btn_scan_start.clicked.connect(self._start_auto_scan)
        ctrl_layout.addWidget(self.btn_scan_start)
        self.btn_manual = QPushButton("수동 모드")
        self.btn_manual.clicked.connect(self._start_manual_mode)
        ctrl_layout.addWidget(self.btn_manual)
        self.btn_prev = QPushButton("◂")
        self.btn_prev.setFixedWidth(50)
        self.btn_prev.clicked.connect(self._step_backward)
        self.btn_prev.setEnabled(False)
        ctrl_layout.addWidget(self.btn_prev)
        self.btn_next = QPushButton("▸")
        self.btn_next.setFixedWidth(50)
        self.btn_next.clicked.connect(self._step_forward)
        self.btn_next.setEnabled(False)
        ctrl_layout.addWidget(self.btn_next)
        self.btn_scan_stop = QPushButton("■ 중지")
        self.btn_scan_stop.clicked.connect(self._stop_scan)
        self.btn_scan_stop.setEnabled(False)
        ctrl_layout.addWidget(self.btn_scan_stop)
        scan_layout.addLayout(ctrl_layout)

        led_display = QHBoxLayout()
        led_display.addWidget(QLabel("현재 LED:"))
        self.spin_current_led = QSpinBox()
        self.spin_current_led.setObjectName("spinCurrentLed")
        self.spin_current_led.setRange(0, 999)
        self.spin_current_led.setFixedWidth(90)
        self.spin_current_led.setEnabled(False)
        self.spin_current_led.valueChanged.connect(self._on_spin_value_changed)
        led_display.addWidget(self.spin_current_led)
        self.btn_mark_corner = QPushButton("이 LED를 코너로 기록")
        self.btn_mark_corner.clicked.connect(self._mark_corner)
        self.btn_mark_corner.setEnabled(False)
        led_display.addWidget(self.btn_mark_corner)
        led_display.addStretch()
        scan_layout.addLayout(led_display)
        layout.addWidget(scan_group)

        # 코너 테이블
        self._corner_group = QGroupBox(f"코너 데이터 ({self._n_wraps}바퀴)")
        corner_layout = QVBoxLayout(self._corner_group)
        corner_layout.setSpacing(_GROUP_SPACING)
        corner_layout.setContentsMargins(*_GROUP_MARGINS)
        corner_desc = QLabel("각 바퀴에서 변이 바뀌는 코너 LED 번호를 입력합니다.")
        corner_desc.setWordWrap(True)
        corner_layout.addWidget(corner_desc)
        self.corner_table = QTableWidget()
        self._rebuild_corner_table()
        corner_layout.addWidget(self.corner_table)
        layout.addWidget(self._corner_group)

        # 세그먼트 미리보기
        seg_group = QGroupBox("세그먼트 (자동 생성)")
        seg_layout = QVBoxLayout(seg_group)
        seg_layout.setSpacing(_GROUP_SPACING)
        seg_layout.setContentsMargins(*_GROUP_MARGINS)
        self.seg_preview = QTextEdit()
        self.seg_preview.setObjectName("segPreview")
        self.seg_preview.setReadOnly(True)
        self.seg_preview.setMaximumHeight(160)
        seg_layout.addWidget(self.seg_preview)
        layout.addWidget(seg_group)

        layout.addStretch()

        # 버튼
        btn_layout = QHBoxLayout()

        btn_generate = QPushButton("세그먼트 생성")
        btn_generate.clicked.connect(self._generate_segments)
        btn_layout.addWidget(btn_generate)

        btn_reset = QPushButton("↩ 저장된 값 복원")
        btn_reset.clicked.connect(self._reset_to_saved)
        btn_layout.addWidget(btn_reset)

        btn_save = QPushButton("LED 레이아웃 저장")
        btn_save.clicked.connect(self._save)
        btn_layout.addWidget(btn_save)
        
        layout.addLayout(btn_layout)

    # ── ★ 바퀴 수 동적 변경 ──

    def _rebuild_corner_table(self):
        """바퀴 수 + 시작 위치에 맞게 코너 테이블을 재구성."""
        pos_info = _START_POSITIONS[self._start_position]
        self.corner_table.setRowCount(self._n_wraps)
        self.corner_table.setColumnCount(5)
        self.corner_table.setHorizontalHeaderLabels(pos_info["headers"])
        self.corner_table.setVerticalHeaderLabels(
            [f"바퀴 {i+1}" for i in range(self._n_wraps)]
        )
        self.corner_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.corner_table.setFixedHeight(30 + 30 * self._n_wraps)

    def _on_wraps_changed(self, value):
        """바퀴 수 스핀박스 변경 시 테이블 재구성."""
        # 기존 데이터 보존
        old_data = []
        for row in range(self.corner_table.rowCount()):
            row_data = []
            for col in range(5):
                item = self.corner_table.item(row, col)
                row_data.append(item.text() if item else "")
            old_data.append(row_data)

        self._n_wraps = value
        self._rebuild_corner_table()
        self._corner_group.setTitle(f"코너 데이터 ({self._n_wraps}바퀴)")

        # 기존 데이터 복원 (줄어든 행은 잘림, 늘어난 행은 빈칸)
        for row in range(min(len(old_data), self._n_wraps)):
            for col in range(5):
                if old_data[row][col]:
                    self.corner_table.setItem(
                        row, col, QTableWidgetItem(old_data[row][col])
                    )

    def _on_monitor_preset_changed(self, index):
        """모니터 크기 프리셋 선택 시 바퀴 수 자동 설정."""
        if index <= 0:
            return  # "직접 설정"
        _, wraps = _MONITOR_PRESETS[index]
        if wraps is not None:
            self.spin_wraps.setValue(wraps)
        # 선택 후 "직접 설정"으로 돌려놓기 (한 번만 적용)
        self.combo_monitor.blockSignals(True)
        self.combo_monitor.setCurrentIndex(0)
        self.combo_monitor.blockSignals(False)

    def _on_start_pos_changed(self, index):
        """시작 위치 콤보 변경 시 테이블 헤더 갱신."""
        self._start_position = _START_POS_KEYS[index]
        self._rebuild_corner_table()

    # ── config 로드 ──

    def _load_from_config(self):
        # ★ 시작 위치 복원
        saved_start = self.layout_cfg.get("start_position", "좌하 (기본)")
        if saved_start in _START_POS_KEYS:
            self._start_position = saved_start
        else:
            self._start_position = "좌하 (기본)"
        idx = _START_POS_KEYS.index(self._start_position)
        self.combo_start_pos.blockSignals(True)
        self.combo_start_pos.setCurrentIndex(idx)
        self.combo_start_pos.blockSignals(False)

        # ★ 바퀴 수 복원
        saved_wraps = self.layout_cfg.get("n_wraps", 2)
        if saved_wraps != self._n_wraps:
            self._n_wraps = saved_wraps
            self.spin_wraps.blockSignals(True)
            self.spin_wraps.setValue(saved_wraps)
            self.spin_wraps.blockSignals(False)

        self._rebuild_corner_table()
        self._corner_group.setTitle(f"코너 데이터 ({self._n_wraps}바퀴)")

        # ★ 코너 데이터 복원 — 시작 위치의 corners 순서에 맞게 매핑
        pos_info = _START_POSITIONS[self._start_position]
        corner_abbrs = pos_info["corners"]  # (c0, c1, c2, c3)
        corners = self.layout_cfg.get("corners", {})
        for w in range(self._n_wraps):
            prefix = f"w{w+1}_"
            # 열 0~3: 시작 위치 순서대로, 열 4: 끝점
            col_keys = [f"{prefix}{c}" for c in corner_abbrs] + [f"{prefix}end"]
            for col, key in enumerate(col_keys):
                val = corners.get(key, "")
                self.corner_table.setItem(
                    w, col,
                    QTableWidgetItem(str(val) if val != "" else "")
                )
        self._update_seg_preview()

    def _update_seg_preview(self):
        segments = self.layout_cfg.get("segments", [])
        lines = [f"LED {seg['start']:>2}→{seg['end']:<2}  {seg['side']}" for seg in segments]
        self.seg_preview.setPlainText("\n".join(lines) if lines else "(세그먼트 없음)")

    # ── 스캔 ──

    def _start_auto_scan(self):
        if not self.dm or not self.dm.is_connected:
            msg_warning(self, "연결 필요", "먼저 LED를 연결하세요.")
            return
        self._start_scan(paused=False)

    def _start_manual_mode(self):
        if not self.dm or not self.dm.is_connected:
            msg_warning(self, "연결 필요", "먼저 LED를 연결하세요.")
            return
        self._start_scan(paused=True)

    def _start_scan(self, paused=False):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop_scan()
            self.scan_thread.wait()
        led_count = self.spin_led_count.value()
        self.spin_current_led.setRange(0, led_count - 1)
        self.spin_current_led.setValue(0)
        self.scan_thread = LedScanThread(self.dm.device, led_count, delay_ms=400)
        self.scan_thread.set_paused(paused)
        self.scan_thread.led_changed.connect(self._on_led_changed)
        self.scan_thread.finished_scan.connect(self._on_scan_finished)
        self.btn_scan_start.setEnabled(False)
        self.btn_manual.setEnabled(False)
        self.btn_scan_stop.setEnabled(True)
        self.btn_prev.setEnabled(True)
        self.btn_next.setEnabled(True)
        self.btn_mark_corner.setEnabled(True)
        self.spin_current_led.setEnabled(True)
        self.scan_thread.start()

    def _stop_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop_scan()
            self.btn_scan_stop.setEnabled(False)
        else:
            self._on_scan_finished()

    def _on_scan_finished(self):
        self.btn_scan_start.setEnabled(True)
        self.btn_manual.setEnabled(True)
        self.btn_scan_stop.setEnabled(False)
        self.btn_prev.setEnabled(False)
        self.btn_next.setEnabled(False)
        self.btn_mark_corner.setEnabled(False)
        self.spin_current_led.setEnabled(False)

    def _on_spin_value_changed(self, value):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.jump_to(value)

    def _on_led_changed(self, idx):
        self.spin_current_led.blockSignals(True)
        self.spin_current_led.setValue(idx)
        self.spin_current_led.blockSignals(False)

    def _step_forward(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.step_forward()

    def _step_backward(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.step_backward()

    def _mark_corner(self):
        led_idx = str(self.spin_current_led.value())
        for row in range(self._n_wraps):
            for col in range(5):
                item = self.corner_table.item(row, col)
                if item is None or item.text().strip() == "":
                    self.corner_table.setItem(row, col, QTableWidgetItem(led_idx))
                    return
        msg_info(self, "코너", "모든 코너가 채워졌습니다.")

    # ── 세그먼트 생성 ──

    def _validate_corners(self, corners_all):
        all_corners = []
        for wrap in corners_all:
            all_corners.extend(wrap)
        if len(all_corners) < 2:
            return True, ""
        direction = 0
        for i in range(1, len(all_corners)):
            if all_corners[i] > all_corners[i - 1]: direction = 1; break
            elif all_corners[i] < all_corners[i - 1]: direction = -1; break
        for i in range(1, len(all_corners)):
            if direction == 1 and all_corners[i] < all_corners[i - 1]:
                return False, f"오름차순이 예상되나 {all_corners[i-1]} 다음에 {all_corners[i]}이(가) 있습니다."
            if direction == -1 and all_corners[i] > all_corners[i - 1]:
                return False, f"내림차순이 예상되나 {all_corners[i-1]} 다음에 {all_corners[i]}이(가) 있습니다."
        return True, ""

    def _generate_segments(self):
        pos_info = _START_POSITIONS[self._start_position]
        sides_order = pos_info["sides"]
        corner_names = pos_info["corners"]  # 4개: (c0, c1, c2, c3)
        # 컬럼 순서: 시작(c0), c1, c2, c3, 끝점(=c0 of next wrap)

        try:
            corners_all = []
            for row in range(self._n_wraps):
                wrap = []
                for col in range(5):
                    item = self.corner_table.item(row, col)
                    if item is None or item.text().strip() == "":
                        raise ValueError(f"바퀴 {row+1}의 {col+1}번째 코너가 비어있습니다.")
                    wrap.append(int(item.text().strip()))
                corners_all.append(wrap)
        except ValueError as e:
            msg_warning(self, "코너 오류", str(e))
            return

        valid, err = self._validate_corners(corners_all)
        if not valid:
            msg_warning(self, "순서 오류", err)
            return

        segments = []
        corners_dict = {}
        for w, wrap in enumerate(corners_all):
            prefix = f"w{w+1}_"
            # 4개 코너 + 끝점 저장
            for i, cname in enumerate(corner_names):
                corners_dict[f"{prefix}{cname}"] = wrap[i]
            corners_dict[f"{prefix}end"] = wrap[4]
            # 4개 세그먼트 생성
            for i in range(4):
                segments.append({"start": wrap[i], "end": wrap[i + 1], "side": sides_order[i]})

        self.layout_cfg["corners"] = corners_dict
        self.layout_cfg["segments"] = segments
        self.layout_cfg["n_wraps"] = self._n_wraps
        self.layout_cfg["start_position"] = self._start_position
        self._update_seg_preview()
        msg_info(self, "세그먼트", f"{len(segments)}개 세그먼트 생성 완료.")

    def _reset_to_saved(self):
        self.config["layout"] = copy.deepcopy(self._saved_layout)
        self.layout_cfg = self.config["layout"]
        self.spin_led_count.setValue(self._saved_led_count)
        self._load_from_config()
        msg_info(self, "복원", "저장된 설정으로 복원했습니다.")

    def _save(self):
        self.config["device"]["led_count"] = self.spin_led_count.value()
        self.layout_cfg["n_wraps"] = self._n_wraps
        self.layout_cfg["start_position"] = self._start_position
        if not self.layout_cfg.get("segments"):
            self._generate_segments()
        save_config(self.config)
        self._saved_layout = copy.deepcopy(self.config["layout"])
        self._saved_led_count = self.config["device"]["led_count"]
        msg_info(self, "저장", "LED 설정이 저장되었습니다.")

    def cleanup(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop_scan()
            self.scan_thread.wait(2000)
        self._device_cleanup()