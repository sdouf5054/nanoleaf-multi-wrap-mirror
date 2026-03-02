"""Nanoleaf Light Strip USB HID 통신"""

import hid
import struct
import time

REPORT_DATA_SIZE = 64


class NanoleafDevice:
    def __init__(self, vendor_id=0x37FA, product_id=0x8202, led_count=75):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.led_count = led_count
        self.device = hid.device()
        self.connected = False

    def connect(self):
        try:
            self.device.open(self.vendor_id, self.product_id)
            self.device.set_nonblocking(0)
            self.connected = True
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
        """GRB 바이트 데이터를 LED에 전송 (분할 전송 + 응답 대기)"""
        header = struct.pack(">BH", 0x02, len(grb_data))
        message = header + grb_data
        chunks = []
        for i in range(0, len(message), REPORT_DATA_SIZE):
            chunk = message[i:i + REPORT_DATA_SIZE]
            if len(chunk) < REPORT_DATA_SIZE:
                chunk += bytes(REPORT_DATA_SIZE - len(chunk))
            chunks.append(chunk)

        # ★ USB 통신 병목·응답 지연으로 발생하는 OSError를 흡수하여 크래시 방지
        try:
            for chunk in chunks:
                self.device.write(bytes([0x00]) + chunk)
            self.device.set_nonblocking(0)
            self.device.read(64, timeout_ms=50)
            self.device.set_nonblocking(1)
            self._flush()
        except OSError:
            # 일시적인 하드웨어 응답 지연: 해당 전송만 스킵하고 계속 진행
            pass

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
