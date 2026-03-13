"""Nanoleaf Light Strip USB HID 통신

순수 Python + hidapi. Qt 의존성 없음.
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
RECONNECT_COOLDOWN = 2.0
MAX_RECONNECT_ATTEMPTS = 3


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
        try:
            self.device.close()
        except HW_ERRORS:
            pass
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
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
                time.sleep(0.3)
        self.connected = False

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
