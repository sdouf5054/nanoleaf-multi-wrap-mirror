"""Nanoleaf Light Strip USB HID 통신

순수 Python + hidapi. Qt 의존성 없음.

[변경] 절전모드 복귀 대응 강화:
- MAX_RECONNECT_ATTEMPTS: 3 → 10
- 재연결 시도 간 대기: 0.3s → 점진적 백오프 (0.5~2.0s)
- reconnect 쿨다운: 2.0s → 1.0s (더 빨리 재시도)
- 새 메서드: force_reconnect() — 외부에서 강제 재연결 요청
- send_rgb: connected=False일 때도 재연결 시도
"""

import struct
import time

try:
    import hid
except ImportError:
    hid = None

from core.constants import HW_ERRORS

REPORT_ID = 0x00
REPORT_DATA_SIZE = 64
CMD_POWER = 0x07
CMD_WRITE_RGB = 0x02
POWER_ON = bytes([0x01])
READ_TIMEOUT_CMD = 2000
READ_TIMEOUT_RGB = 30
MAX_CONSECUTIVE_FAILURES = 5
RECONNECT_COOLDOWN = 1.0
MAX_RECONNECT_ATTEMPTS = 10


class NanoleafDevice:
    def __init__(self, vendor_id=0x37FA, product_id=0x8202, led_count=75):
        if hid is None:
            raise ImportError("hidapi가 필요합니다.\npip install hidapi")
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.led_count = led_count
        self.device = hid.device()
        self.connected = False
        self._consecutive_failures = 0
        self._last_reconnect_time = 0.0

    def connect(self):
        try:
            self.device.open(self.vendor_id, self.product_id)
            self.device.set_nonblocking(0)
            self.connected = True
            self._consecutive_failures = 0
            self._cmd_blocking(CMD_POWER, POWER_ON)
            self.device.set_nonblocking(1)
            return True
        except HW_ERRORS as e:
            self.connected = False
            raise ConnectionError(f"Nanoleaf 연결 실패: {e}")

    def _cmd_blocking(self, cmd, payload=b""):
        msg = struct.pack(">BH", cmd, len(payload)) + payload
        self.device.write(bytes([REPORT_ID]) + msg)
        self.device.read(REPORT_DATA_SIZE, timeout_ms=READ_TIMEOUT_CMD)

    def _flush(self):
        while True:
            r = self.device.read(REPORT_DATA_SIZE)
            if not r:
                break

    def send_rgb(self, grb_data):
        # ★ connected=False이면 먼저 재연결 시도
        if not self.connected:
            self._try_reconnect()
            if not self.connected:
                return

        header = struct.pack(">BH", CMD_WRITE_RGB, len(grb_data))
        message = header + grb_data
        chunks = []
        for i in range(0, len(message), REPORT_DATA_SIZE):
            chunk = message[i:i + REPORT_DATA_SIZE]
            if len(chunk) < REPORT_DATA_SIZE:
                chunk += bytes(REPORT_DATA_SIZE - len(chunk))
            chunks.append(chunk)
        try:
            for chunk in chunks:
                self.device.write(bytes([REPORT_ID]) + chunk)
            self.device.set_nonblocking(0)
            self.device.read(REPORT_DATA_SIZE, timeout_ms=READ_TIMEOUT_RGB)
            self.device.set_nonblocking(1)
            self._flush()
            self._consecutive_failures = 0
        except HW_ERRORS:
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._try_reconnect()

    def _try_reconnect(self):
        now = time.time()
        if now - self._last_reconnect_time < RECONNECT_COOLDOWN:
            return
        self._last_reconnect_time = now

        # 기존 디바이스 닫기
        try:
            self.device.close()
        except HW_ERRORS:
            pass

        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            # ★ 점진적 백오프: 0.5s, 0.7s, 0.9s, ... 최대 2.0s
            delay = min(0.5 + attempt * 0.2, 2.0)
            time.sleep(delay)

            try:
                self.device = hid.device()
                self.device.open(self.vendor_id, self.product_id)
                self.device.set_nonblocking(0)
                self._cmd_blocking(CMD_POWER, POWER_ON)
                self.device.set_nonblocking(1)
                self.connected = True
                self._consecutive_failures = 0
                return
            except HW_ERRORS:
                try:
                    self.device.close()
                except HW_ERRORS:
                    pass
                continue

        self.connected = False

    def force_reconnect(self):
        """외부에서 강제 재연결 요청 (절전 복귀 등).

        쿨다운을 무시하고 즉시 재연결을 시도합니다.
        """
        self._last_reconnect_time = 0.0
        self._consecutive_failures = 0
        self.connected = False

        try:
            self.device.close()
        except HW_ERRORS:
            pass

        self._try_reconnect()

    def set_all_color(self, r, g, b):
        self.send_rgb(bytes([g, r, b] * self.led_count))

    def turn_off(self):
        self.send_rgb(bytes(self.led_count * 3))

    def disconnect(self):
        if self.connected:
            try:
                self.device.close()
            except HW_ERRORS:
                pass
            self.connected = False
            self._consecutive_failures = 0
