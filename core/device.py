"""Nanoleaf Light Strip USB HID 통신

[변경 사항 v2]
- send_rgb()의 예외 범위를 Exception으로 확대 (ValueError, IOError 등 대응)
- 연속 N회 전송 실패 시 자동 재연결 시도 (_try_reconnect)
- 재연결 성공/실패 상태를 connected 플래그에 반영
- 재연결 쿨다운으로 과도한 재시도 방지
"""

import hid
import struct
import time

REPORT_DATA_SIZE = 64

# 재연결 관련 상수
MAX_CONSECUTIVE_FAILURES = 5    # 이 횟수만큼 연속 실패하면 재연결 시도
RECONNECT_COOLDOWN = 2.0        # 재연결 시도 간 최소 간격 (초)
MAX_RECONNECT_ATTEMPTS = 3      # 한 번의 재연결 사이클에서 최대 시도 횟수


class NanoleafDevice:
    def __init__(self, vendor_id=0x37FA, product_id=0x8202, led_count=75):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.led_count = led_count
        self.device = hid.device()
        self.connected = False

        # ★ 실패 추적
        self._consecutive_failures = 0
        self._last_reconnect_time = 0.0

    def connect(self):
        try:
            self.device.open(self.vendor_id, self.product_id)
            self.device.set_nonblocking(0)
            self.connected = True
            self._consecutive_failures = 0
            self._cmd_blocking(0x07, bytes([0x01]))  # POWER ON
            self.device.set_nonblocking(1)
            return True
        except Exception as e:
            self.connected = False
            raise ConnectionError(f"Nanoleaf 연결 실패: {e}")

    def _cmd_blocking(self, cmd, payload=b""):
        msg = struct.pack(">BH", cmd, len(payload)) + payload
        self.device.write(bytes([0x00]) + msg)
        self.device.read(64, timeout_ms=2000)

    def _flush(self):
        while True:
            r = self.device.read(64)
            if not r:
                break

    def send_rgb(self, grb_data):
        """GRB 바이트 데이터를 LED에 전송 (분할 전송 + 응답 대기)

        ★ 모든 예외를 흡수하여 크래시 방지.
        연속 실패가 임계값에 도달하면 자동 재연결을 시도합니다.
        """
        header = struct.pack(">BH", 0x02, len(grb_data))
        message = header + grb_data
        chunks = []
        for i in range(0, len(message), REPORT_DATA_SIZE):
            chunk = message[i:i + REPORT_DATA_SIZE]
            if len(chunk) < REPORT_DATA_SIZE:
                chunk += bytes(REPORT_DATA_SIZE - len(chunk))
            chunks.append(chunk)

        try:
            for chunk in chunks:
                self.device.write(bytes([0x00]) + chunk)
            self.device.set_nonblocking(0)
            self.device.read(64, timeout_ms=30)
            self.device.set_nonblocking(1)
            self._flush()

            # ★ 전송 성공 — 실패 카운터 리셋
            self._consecutive_failures = 0

        except Exception:
            # ★ 모든 예외 흡수 (OSError, ValueError, IOError 등)
            self._consecutive_failures += 1

            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._try_reconnect()

    def _try_reconnect(self):
        """연속 전송 실패 시 자동 재연결 시도.

        쿨다운 시간 내에는 재시도하지 않아 과도한 재연결 방지.
        재연결 성공 시 실패 카운터를 리셋하고, 실패 시 connected를 False로 설정.
        """
        now = time.time()
        if now - self._last_reconnect_time < RECONNECT_COOLDOWN:
            return
        self._last_reconnect_time = now

        # 기존 연결 안전하게 닫기
        try:
            self.device.close()
        except Exception:
            pass

        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            try:
                self.device = hid.device()
                self.device.open(self.vendor_id, self.product_id)
                self.device.set_nonblocking(0)
                self._cmd_blocking(0x07, bytes([0x01]))  # POWER ON
                self.device.set_nonblocking(1)

                # 재연결 성공
                self.connected = True
                self._consecutive_failures = 0
                return

            except Exception:
                time.sleep(0.3)

        # 모든 시도 실패
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
            except Exception:
                pass
            self.connected = False
            self._consecutive_failures = 0
