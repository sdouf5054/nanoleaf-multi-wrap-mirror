"""모니터 LED 프리뷰 위젯

모니터 뒷면에 부착된 LED 스트립의 물리적 배치를 시각화합니다.
멀티랩(여러 바퀴) 구조를 반영하여 안/바깥 바퀴를 구분합니다.

모든 모드(미러링/하이브리드/오디오)에서 사용 가능합니다.
"""

import numpy as np
from PyQt5.QtWidgets import QWidget
from PyQt5.QtGui import QPainter, QColor, QBrush
from PyQt5.QtCore import Qt

from core.layout import get_led_positions


class MonitorPreview(QWidget):
    """모니터 뒷면 LED 배치 시각화.

    각 LED의 현재 색상을 set_colors()로 전달하면
    모니터 주변에 점으로 표시합니다.

    zone 매핑이 설정된 경우, zone_colors → LED별 색상으로 변환합니다.
    """

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._led_colors = None
        self._zone_map = None
        self._led_count = config["device"]["led_count"]
        self._positions = None
        self._sides = None
        self._n_zones = 4
        self.setMinimumHeight(220)
        self.setMaximumHeight(360)
        self.setMinimumWidth(400)
        self._compute_positions()

    def _compute_positions(self):
        """LED 위치를 정규화 좌표(0~1)로 계산."""
        lc = self._config["layout"]
        mc = self._config.get("mirror", {})
        sw = mc.get("grid_cols", 64) * 40
        sh = mc.get("grid_rows", 32) * 40

        pos, sides = get_led_positions(
            sw, sh, lc["segments"], self._led_count,
            orientation=mc.get("orientation", "auto"),
            portrait_rotation=mc.get("portrait_rotation", "cw"),
        )

        self._positions = np.zeros_like(pos)
        if sw > 0:
            self._positions[:, 0] = pos[:, 0] / sw
        if sh > 0:
            self._positions[:, 1] = pos[:, 1] / sh
        self._sides = sides

        # 멀티랩: 같은 side에 속하는 세그먼트 수로 바퀴 인덱스 결정
        self._wrap_index = np.zeros(self._led_count, dtype=np.int32)
        sc = {}
        for seg in lc["segments"]:
            s, e, side = seg["start"], seg["end"], seg["side"]
            n = abs(s - e)
            if n == 0:
                continue
            w = sc.get(side, 0)
            sc[side] = w + 1
            step = -1 if s > e else 1
            for i in range(n):
                idx = s + step * i
                if 0 <= idx < self._led_count:
                    self._wrap_index[idx] = w

    def set_zone_map(self, zm):
        """LED→zone 매핑 배열 설정."""
        self._zone_map = zm

    def set_n_zones(self, n):
        """구역 수 설정."""
        self._n_zones = n

    def set_colors(self, colors):
        """LED 색상 설정.

        Args:
            colors: (n_leds, 3) or (n_zones, 3) float/int array, RGB 0~255
        """
        if colors is not None and len(colors) > 0:
            self._led_colors = np.clip(colors, 0, 255).astype(np.float32)
            self.update()

    def _get_led_color(self, li):
        """LED 인덱스에 대한 RGB 튜플 반환."""
        if self._led_colors is None:
            return (60, 60, 60)

        nc = len(self._led_colors)
        if nc >= self._led_count:
            # per-LED 색상
            c = self._led_colors[li]
        elif self._zone_map is not None:
            # zone 매핑
            c = self._led_colors[self._zone_map[li] % nc]
        elif nc == 1:
            c = self._led_colors[0]
        else:
            return (60, 60, 60)

        return (int(c[0]), int(c[1]), int(c[2]))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()

        ls = 11  # LED 점 크기
        wg = ls + 8  # 바퀴 간 간격
        lm = wg * 2 + 20  # 마진
        aw = w - 2 * lm
        ah = h - 2 * lm

        if aw < 40 or ah < 20:
            p.end()
            return

        # 16:9 비율 유지
        asp = 16.0 / 9.0
        if aw / ah > asp:
            mh = ah
            mw = int(mh * asp)
        else:
            mw = aw
            mh = int(mw / asp)

        mx = (w - mw) // 2
        my = (h - mh) // 2

        # 모니터 배경
        p.setPen(QColor(80, 80, 80))
        p.setBrush(QBrush(QColor(45, 45, 48)))
        p.drawRoundedRect(mx, my, mw, mh, 3, 3)

        if self._positions is None:
            p.end()
            return

        hl = ls // 2

        for i in range(self._led_count):
            nx, ny = self._positions[i]
            side = self._sides[i]
            wrap = self._wrap_index[i]

            # LED 위치: 모니터 가장자리 바깥쪽, 바퀴별 간격
            px = mx + nx * mw
            py = my + ny * mh
            d = wg * (2 - wrap)

            if side == "top":
                py = my - d
            elif side == "bottom":
                py = my + mh + d - ls
            elif side == "left":
                px = mx - d
            elif side == "right":
                px = mx + mw + d - ls

            r, g, b = self._get_led_color(i)
            p.setBrush(QBrush(QColor(r, g, b)))
            p.setPen(QColor(70, 70, 70))
            p.drawRoundedRect(int(px - hl), int(py - hl), ls, ls, 2, 2)

        p.end()
