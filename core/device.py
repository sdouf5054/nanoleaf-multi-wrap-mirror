"""Nanoleaf Light Strip USB HID 통신

[변경 사항 v3]
- 통신 프로토콜 매직 넘버를 모듈 상수로 분리 (가독성·유지보수성 향상)
- except Exception → 하드웨어/통신 예외(OSError, ValueError)만 구체적으로 포착
  → NameError, TypeError 등 코드 버그는 숨기지 않고 상위로 전파
"""

import hid
import struct
import time

# ── HID 프로토콜 상수 ─────────────────────────────────────────────
REPORT_ID = 0x00            # HID Report ID (항상 0)
REPORT_DATA_SIZE = 64       # HID 리포트 데이터 영역 크기 (bytes)

CMD_POWER = 0x07            # 전원 제어 명령
CMD_WRITE_RGB = 0x02        # RGB 데이터 쓰기 명령

POWER_ON = bytes([0x01])    # 전원 ON 페이로드
POWER_OFF = bytes([0x00])   # 전원 OFF 페이로드 (필요 시)

READ_TIMEOUT_CMD = 2000     # 명령 응답 대기 타임아웃 (ms)
READ_TIMEOUT_RGB = 30       # RGB 전송 응답 대기 타임아웃 (ms)

# ── 재연결 관련 상수 ──────────────────────────────────────────────
MAX_CONSECUTIVE_FAILURES = 5
RECONNECT_COOLDOWN = 2.0
MAX_RECONNECT_ATTEMPTS = 3

# ── 포착 대상 예외 ────────────────────────────────────────────────
# USB/HID 통신에서 발생할 수 있는 예외만 포착합니다.
# NameError, TypeError 등 코드 버그는 의도적으로 포착하지 않습니다.
_HW_ERRORS = (OSError, IOError, ValueError)


class NanoleafDevice:
    def __init__(self, vendor_id=0x37FA, product_id=0x8202, led_count=75):
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
        except _HW_ERRORS as e:
            self.connected = False
            raise ConnectionError(f"Nanoleaf 연결 실패: {e}")

    def _cmd_blocking(self, cmd, payload=b""):
        """명령 전송 + 블로킹 응답 대기"""
        msg = struct.pack(">BH", cmd, len(payload)) + payload
        self.device.write(bytes([REPORT_ID]) + msg)
        self.device.read(REPORT_DATA_SIZE, timeout_ms=READ_TIMEOUT_CMD)

    def _flush(self):
        """수신 버퍼에 남은 데이터 비우기"""
        while True:
            r = self.device.read(REPORT_DATA_SIZE)
            if not r:
                break

    def send_rgb(self, grb_data):
        """GRB 바이트 데이터를 LED에 전송 (분할 전송 + 응답 대기)

        하드웨어/통신 예외만 흡수하여 크래시 방지.
        연속 실패가 임계값에 도달하면 자동 재연결을 시도합니다.
        """
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

        except _HW_ERRORS:
            self._consecutive_failures += 1

            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._try_reconnect()

    def _try_reconnect(self):
        """연속 전송 실패 시 자동 재연결 시도."""
        now = time.time()
        if now - self._last_reconnect_time < RECONNECT_COOLDOWN:
            return
        self._last_reconnect_time = now

        try:
            self.device.close()
        except _HW_ERRORS:
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

            except _HW_ERRORS:
                time.sleep(0.3)

        self.connected = False

    def set_all_color(self, r, g, b):
        """모든 LED를 단일 색으로 설정 (GRB 순서)"""
        self.send_rgb(bytes([g, r, b] * self.led_count))

    def turn_off(self):
        """모든 LED 끄기"""
        self.send_rgb(bytes(self.led_count * 3))

    def test_rgb(self):
        """빨강→초록→파랑 순서로 테스트"""
        for name, r, g, b in [("빨강", 255, 0, 0), ("초록", 0, 255, 0), ("파랑", 0, 0, 255)]:
            self.set_all_color(r, g, b)
            time.sleep(0.3)

    def disconnect(self):
        if self.connected:
            try:
                self.device.close()
            except _HW_ERRORS:
                pass
            self.connected = False
            self._consecutive_failures = 0
