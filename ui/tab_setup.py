"""LED 캘리브레이션 탭 — 코너 찾기 + 세그먼트 자동 생성 (2바퀴 고정)"""

import time
import copy
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QSpinBox, QComboBox, QTableWidget, QTableWidgetItem,
    QMessageBox, QHeaderView, QTextEdit, QScrollArea, QFrame
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

from core.device import NanoleafDevice
from core.config import save_config


class LedScanThread(QThread):
    """LED를 한 개씩 순차 점등하는 스레드"""
    led_changed = pyqtSignal(int)
    finished_scan = pyqtSignal()

    def __init__(self, device, led_count, delay_ms=500):
        super().__init__()
        self.device = device
        self.led_count = led_count
        self.delay_ms = delay_ms
        self._running = True
        self._current = 0
        self._paused = False
        self._step_request = 0
        self._jump_request = -1  # ★ 직접 번호 입력 점프용

    def run(self):
        self._current = 0
        # ★ USB 기기 안정화 대기: 이전 스레드의 turn_off() 명령이
        #   물리적으로 처리될 시간을 확보하여 OSError 방지
        time.sleep(0.1)
        try:
            while self._running and self._current < self.led_count:
                self._light_single(self._current)
                self.led_changed.emit(self._current)

                while self._running:
                    # ★ 점프 요청 우선 처리
                    if self._jump_request != -1:
                        self._current = max(0, min(self._jump_request, self.led_count - 1))
                        self._jump_request = -1
                        break
                    if self._step_request != 0:
                        self._current += self._step_request
                        self._current = max(0, min(self._current, self.led_count - 1))
                        self._step_request = 0
                        break
                    if not self._paused:
                        time.sleep(self.delay_ms / 1000.0)
                        self._current += 1
                        break
                    time.sleep(0.05)
        except Exception:
            # ★ 예상치 못한 에러가 C++ 런타임까지 전파되는 것을 차단
            pass
        finally:
            if self.device and self.device.connected:
                try:
                    self.device.turn_off()
                except Exception:
                    pass
            self.finished_scan.emit()

    def _light_single(self, idx):
        if not self.device or not self.device.connected:
            return
        data = bytearray(self.led_count * 3)
        data[idx * 3] = 255
        data[idx * 3 + 1] = 255
        data[idx * 3 + 2] = 255
        self.device.send_rgb(bytes(data))

    def jump_to(self, idx):
        """★ 특정 LED 번호로 즉시 이동"""
        self._jump_request = idx

    def step_forward(self):
        self._step_request = 1

    def step_backward(self):
        self._step_request = -1

    def set_paused(self, paused):
        self._paused = paused

    def stop_scan(self):
        self._running = False


class SetupTab(QWidget):
    N_WRAPS = 2  # 바퀴 수 고정

    # ★ 미러링 중지 요청 시그널 — LED 연결 시도 전 MainWindow로 전송
    request_mirror_stop = pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.layout_cfg = config["layout"]
        self.device = None
        self.scan_thread = None

        # 리셋용: 앱 시작 시점의 layout 설정 백업
        self._saved_layout = copy.deepcopy(config["layout"])
        self._saved_led_count = config["device"]["led_count"]

        self._build_ui()
        self._load_from_config()

    def _build_ui(self):
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(14)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll.setWidget(container)

        # === 연결 ===
        conn_layout = QHBoxLayout()
        self.btn_connect = QPushButton("🔌 LED 연결")
        self.btn_connect.clicked.connect(self._toggle_connection)
        conn_layout.addWidget(self.btn_connect)

        self.conn_label = QLabel("연결 안 됨")
        self.conn_label.setStyleSheet("color: #c0392b;")
        conn_layout.addWidget(self.conn_label)
        conn_layout.addStretch()
        layout.addLayout(conn_layout)

        # === 기본 설정 ===
        basic_group = QGroupBox("기본 설정")
        basic_layout = QHBoxLayout(basic_group)

        basic_layout.addWidget(QLabel("LED 수:"))
        self.spin_led_count = QSpinBox()
        self.spin_led_count.setRange(1, 300)
        self.spin_led_count.setValue(self.config["device"]["led_count"])
        basic_layout.addWidget(self.spin_led_count)

        basic_layout.addWidget(QLabel("감는 방향:"))
        self.combo_direction = QComboBox()
        self.combo_direction.addItems(["시계방향 (LED 번호 감소)", "반시계방향 (LED 번호 증가)"])
        basic_layout.addWidget(self.combo_direction)

        basic_layout.addStretch()
        layout.addWidget(basic_group)

        # === LED 스캔 ===
        scan_group = QGroupBox("LED 코너 찾기")
        scan_layout = QVBoxLayout(scan_group)

        scan_desc = QLabel(
            "LED를 순차 점등하면서 각 코너(변이 바뀌는 지점)의\n"
            "LED 번호를 기록합니다. ◀/▶ 또는 자동 스캔으로 이동하세요."
        )
        scan_desc.setWordWrap(True)
        scan_layout.addWidget(scan_desc)

        ctrl_layout = QHBoxLayout()
        self.btn_scan_start = QPushButton("▶ 자동 스캔")
        self.btn_scan_start.clicked.connect(self._start_auto_scan)
        ctrl_layout.addWidget(self.btn_scan_start)

        self.btn_manual = QPushButton("🖐 수동 모드")
        self.btn_manual.clicked.connect(self._start_manual_mode)
        ctrl_layout.addWidget(self.btn_manual)

        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedWidth(50)
        self.btn_prev.clicked.connect(self._step_backward)
        self.btn_prev.setEnabled(False)
        ctrl_layout.addWidget(self.btn_prev)

        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedWidth(50)
        self.btn_next.clicked.connect(self._step_forward)
        self.btn_next.setEnabled(False)
        ctrl_layout.addWidget(self.btn_next)

        self.btn_scan_stop = QPushButton("⏹ 중지")
        self.btn_scan_stop.clicked.connect(self._stop_scan)
        self.btn_scan_stop.setEnabled(False)
        ctrl_layout.addWidget(self.btn_scan_stop)

        scan_layout.addLayout(ctrl_layout)

        led_display = QHBoxLayout()
        led_display.addWidget(QLabel("현재 LED:"))

        # ★ QLabel → QSpinBox: 직접 번호 입력으로 즉시 이동 가능
        self.spin_current_led = QSpinBox()
        self.spin_current_led.setRange(0, 999)  # 스캔 시작 시 실제 LED 수로 조정
        self.spin_current_led.setFixedWidth(90)
        self.spin_current_led.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: #2980b9;"
        )
        self.spin_current_led.setEnabled(False)  # 스캔 중에만 활성화
        self.spin_current_led.valueChanged.connect(self._on_spin_value_changed)
        led_display.addWidget(self.spin_current_led)

        self.btn_mark_corner = QPushButton("📌 이 LED를 코너로 기록")
        self.btn_mark_corner.clicked.connect(self._mark_corner)
        self.btn_mark_corner.setEnabled(False)
        led_display.addWidget(self.btn_mark_corner)

        led_display.addStretch()
        scan_layout.addLayout(led_display)

        layout.addWidget(scan_group)

        # === 코너 테이블 ===
        corner_group = QGroupBox("코너 데이터 (2바퀴)")
        corner_layout = QVBoxLayout(corner_group)

        corner_desc = QLabel(
            "각 바퀴에서 변이 바뀌는 코너 LED 번호를 입력합니다.\n"
            "순서: 시작점(좌하) → 좌상 → 우상 → 우하 → 끝점\n"
            "⚠ 코너 번호는 일관되게 내림차순 또는 오름차순이어야 합니다."
        )
        corner_desc.setWordWrap(True)
        corner_layout.addWidget(corner_desc)

        self.corner_table = QTableWidget()
        self.corner_table.setRowCount(self.N_WRAPS)
        self.corner_table.setColumnCount(5)
        self.corner_table.setHorizontalHeaderLabels(
            ["시작(좌하)", "좌상", "우상", "우하", "끝점"]
        )
        self.corner_table.setVerticalHeaderLabels(["바퀴 1", "바퀴 2"])
        self.corner_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        # 테이블 높이: 헤더 + 2행
        self.corner_table.setFixedHeight(90)
        corner_layout.addWidget(self.corner_table)

        layout.addWidget(corner_group)

        # === 세그먼트 미리보기 ===
        seg_group = QGroupBox("세그먼트 (자동 생성)")
        seg_layout = QVBoxLayout(seg_group)

        self.seg_preview = QTextEdit()
        self.seg_preview.setReadOnly(True)
        self.seg_preview.setMaximumHeight(160)
        self.seg_preview.setStyleSheet("font-family: Consolas, monospace;")
        seg_layout.addWidget(self.seg_preview)

        layout.addWidget(seg_group)

        # === 버튼 ===
        btn_layout = QHBoxLayout()

        btn_generate = QPushButton("🔄 세그먼트 생성")
        btn_generate.clicked.connect(self._generate_segments)
        btn_layout.addWidget(btn_generate)

        btn_reset = QPushButton("↩ 저장된 값 복원")
        btn_reset.clicked.connect(self._reset_to_saved)
        btn_layout.addWidget(btn_reset)

        btn_save = QPushButton("💾 설정 저장")
        btn_save.clicked.connect(self._save)
        btn_layout.addWidget(btn_save)

        layout.addLayout(btn_layout)

    def _load_from_config(self):
        """config에서 코너 데이터를 테이블에 로드"""
        corners = self.layout_cfg.get("corners", {})

        # 바퀴 1
        w1 = [corners.get(k, "") for k in ("w1_bl", "w1_tl", "w1_tr", "w1_br", "w1_end")]
        for col, val in enumerate(w1):
            self.corner_table.setItem(0, col, QTableWidgetItem(str(val) if val != "" else ""))

        # 바퀴 2
        w2_bl = corners.get("w2_bl", corners.get("w1_end", ""))
        w2 = [
            w2_bl,
            corners.get("w2_tl", ""),
            corners.get("w2_tr", ""),
            corners.get("w2_br", ""),
            corners.get("w2_end", ""),
        ]
        for col, val in enumerate(w2):
            self.corner_table.setItem(1, col, QTableWidgetItem(str(val) if val != "" else ""))

        self._update_seg_preview()

    def _update_seg_preview(self):
        segments = self.layout_cfg.get("segments", [])
        lines = []
        for seg in segments:
            lines.append(f"LED {seg['start']:>2}→{seg['end']:<2}  {seg['side']}")
        self.seg_preview.setPlainText("\n".join(lines) if lines else "(세그먼트 없음)")

    # --- 연결 ---
    def force_disconnect(self):
        """★ 외부(MainWindow 등)에서 강제로 기기 연결을 해제하고 UI를 초기화합니다.
        미러링 시작 시 MainWindow._start_mirror()에서 호출됩니다.
        """
        if self.device and self.device.connected:
            self._stop_scan()
            try:
                self.device.turn_off()
            except Exception:
                pass
            self.device.disconnect()
            self.device = None
            self.conn_label.setText("연결 안 됨")
            self.conn_label.setStyleSheet("color: #c0392b;")
            self.btn_connect.setText("🔌 LED 연결")

    def _toggle_connection(self):
        if self.device and self.device.connected:
            self.force_disconnect()
        else:
            # ★ 새 연결 전 미러링 강제 종료 요청 (MainWindow._stop_mirror_sync 호출)
            self.request_mirror_stop.emit()

            try:
                dev_cfg = self.config["device"]
                self.device = NanoleafDevice(
                    int(dev_cfg["vendor_id"], 16),
                    int(dev_cfg["product_id"], 16),
                    dev_cfg["led_count"]
                )
                self.device.connect()
                self.conn_label.setText("연결됨 ✅")
                self.conn_label.setStyleSheet("color: #2d8c46;")
                self.btn_connect.setText("🔌 연결 해제")
            except Exception as e:
                QMessageBox.warning(self, "연결 실패", str(e))

    # --- 스캔 ---
    def _start_auto_scan(self):
        if not self.device or not self.device.connected:
            QMessageBox.warning(self, "연결 필요", "먼저 LED를 연결하세요.")
            return
        self._start_scan(paused=False)

    def _start_manual_mode(self):
        if not self.device or not self.device.connected:
            QMessageBox.warning(self, "연결 필요", "먼저 LED를 연결하세요.")
            return
        self._start_scan(paused=True)

    def _start_scan(self, paused=False):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop_scan()
            # ★ 크래시 방지: 제한 없이 스레드가 완전히 종료될 때까지 대기
            self.scan_thread.wait()

        led_count = self.spin_led_count.value()
        # ★ SpinBox 범위를 실제 LED 수에 맞게 조정
        self.spin_current_led.setRange(0, led_count - 1)
        self.spin_current_led.setValue(0)

        self.scan_thread = LedScanThread(self.device, led_count, delay_ms=400)
        self.scan_thread.set_paused(paused)
        self.scan_thread.led_changed.connect(self._on_led_changed)
        self.scan_thread.finished_scan.connect(self._on_scan_finished)

        self.btn_scan_start.setEnabled(False)
        self.btn_manual.setEnabled(False)
        self.btn_scan_stop.setEnabled(True)
        self.btn_prev.setEnabled(True)
        self.btn_next.setEnabled(True)
        self.btn_mark_corner.setEnabled(True)
        self.spin_current_led.setEnabled(True)  # ★ 스캔 시작 시 입력 활성화
        self.scan_thread.start()

    def _stop_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop_scan()
            # ★ 크래시 방지: 강제로 _on_scan_finished()를 즉시 호출하지 않고
            #   스레드가 finally 블록에서 finished_scan 시그널을 보낼 때까지 대기.
            #   (중지 버튼 직후 재시작 버튼 연타 시 C++ 객체 파괴 충돌 방지)
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
        self.spin_current_led.setEnabled(False)  # ★ 스캔 종료 시 입력 비활성화

    def _on_spin_value_changed(self, value):
        """★ 사용자가 SpinBox에 직접 숫자를 입력했을 때 해당 LED로 점프"""
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.jump_to(value)

    def _on_led_changed(self, idx):
        # ★ 스레드→UI→스레드 무한루프 방지: 시그널 일시 차단 후 값 설정
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
        # ★ QLabel.text() 대신 QSpinBox.value() 사용
        led_idx = str(self.spin_current_led.value())
        for row in range(self.N_WRAPS):
            for col in range(5):
                item = self.corner_table.item(row, col)
                if item is None or item.text().strip() == "":
                    self.corner_table.setItem(row, col, QTableWidgetItem(led_idx))
                    return
        QMessageBox.information(self, "코너", "모든 코너가 채워졌습니다.")

    # --- 검증 ---
    def _validate_corners(self, corners_all):
        all_corners = []
        for wrap in corners_all:
            all_corners.extend(wrap)
        if len(all_corners) < 2:
            return True, ""

        # 1. 값이 처음으로 달라지는 구간 기준으로 전체 방향 결정
        #    (중복값은 방향 판단에서 건너뜀)
        direction = 0  # 0: 모두 동일, 1: 오름차순, -1: 내림차순
        for i in range(1, len(all_corners)):
            if all_corners[i] > all_corners[i - 1]:
                direction = 1
                break
            elif all_corners[i] < all_corners[i - 1]:
                direction = -1
                break

        # 2. 역방향으로 꺾이는 경우만 차단 (중복값은 허용)
        #    예) 끝점==좌하(29,29), 끝점==우하(0,0) 같은 경계값 중복 허용
        for i in range(1, len(all_corners)):
            if direction == 1 and all_corners[i] < all_corners[i - 1]:
                return False, (
                    f"오름차순이 예상되나 {all_corners[i-1]} 다음에 "
                    f"{all_corners[i]}이(가) 있습니다.\n"
                    f"전체: {all_corners}"
                )
            if direction == -1 and all_corners[i] > all_corners[i - 1]:
                return False, (
                    f"내림차순이 예상되나 {all_corners[i-1]} 다음에 "
                    f"{all_corners[i]}이(가) 있습니다.\n"
                    f"전체: {all_corners}"
                )
        return True, ""

    # --- 세그먼트 ---
    def _generate_segments(self):
        sides_order = ["left", "top", "right", "bottom"]

        try:
            corners_all = []
            for row in range(self.N_WRAPS):
                wrap = []
                for col in range(5):
                    item = self.corner_table.item(row, col)
                    if item is None or item.text().strip() == "":
                        raise ValueError(f"바퀴 {row+1}의 {col+1}번째 코너가 비어있습니다.")
                    wrap.append(int(item.text().strip()))
                corners_all.append(wrap)
        except ValueError as e:
            QMessageBox.warning(self, "코너 오류", str(e))
            return

        valid, err = self._validate_corners(corners_all)
        if not valid:
            QMessageBox.warning(self, "순서 오류", err)
            return

        segments = []
        corners_dict = {}
        for w, wrap in enumerate(corners_all):
            prefix = f"w{w+1}_"
            for i, name in enumerate(("bl", "tl", "tr", "br", "end")):
                corners_dict[prefix + name] = wrap[i]
            for i in range(4):
                segments.append({
                    "start": wrap[i], "end": wrap[i + 1], "side": sides_order[i]
                })

        self.layout_cfg["corners"] = corners_dict
        self.layout_cfg["segments"] = segments
        self._update_seg_preview()
        QMessageBox.information(self, "세그먼트", f"{len(segments)}개 세그먼트 생성 완료.")

    def _reset_to_saved(self):
        """앱 시작 시점의 저장된 값으로 복원"""
        self.config["layout"] = copy.deepcopy(self._saved_layout)
        self.layout_cfg = self.config["layout"]
        self.spin_led_count.setValue(self._saved_led_count)
        self._load_from_config()
        QMessageBox.information(self, "복원", "저장된 설정으로 복원했습니다.")

    def _save(self):
        self.config["device"]["led_count"] = self.spin_led_count.value()
        if not self.layout_cfg.get("segments"):
            self._generate_segments()
        save_config(self.config)
        # 저장 후 백업도 갱신
        self._saved_layout = copy.deepcopy(self.config["layout"])
        self._saved_led_count = self.config["device"]["led_count"]
        QMessageBox.information(self, "저장", "LED 설정이 저장되었습니다.")

    def cleanup(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop_scan()
            self.scan_thread.wait(2000)
        if self.device and self.device.connected:
            try:
                self.device.turn_off()
                self.device.disconnect()
            except Exception:
                pass
