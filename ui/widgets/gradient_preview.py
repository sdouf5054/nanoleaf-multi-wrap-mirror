"""대역 비율 그라데이션 미리보기 위젯

Bass/Mid/High 비율에 따라 무지개 색상이 어떻게 분배되는지
가로 막대로 시각화합니다.
"""

from PyQt5.QtWidgets import QWidget
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtCore import Qt

from core.audio_visualizer import _remap_t


# 무지개 키포인트 (AudioVisualizer._band_color과 동일)
RAINBOW_KEYPOINTS = [
    (0.000, 255,   0,   0),
    (0.130, 255, 127,   0),
    (0.260, 255, 255,   0),
    (0.400,   0, 255,   0),
    (0.540,   0, 180, 255),
    (0.680,   0,  50, 255),
    (0.820,  80,   0, 255),
    (1.000, 160,   0, 220),
]


def rainbow_color_at(t):
    """밴드 위치(0~1) → RGB 튜플."""
    t = max(0.0, min(1.0, t))
    for i in range(len(RAINBOW_KEYPOINTS) - 1):
        t0, r0, g0, b0 = RAINBOW_KEYPOINTS[i]
        t1, r1, g1, b1 = RAINBOW_KEYPOINTS[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return (
                int(r0 + (r1 - r0) * f),
                int(g0 + (g1 - g0) * f),
                int(b0 + (b1 - b0) * f),
            )
    return (160, 0, 220)


class GradientPreview(QWidget):
    """대역 비율에 따른 무지개 그라데이션 가로 막대."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zone_weights = (33, 33, 34)
        self.setFixedHeight(20)
        self.setMinimumWidth(100)

    def set_zone_weights(self, bass, mid, high):
        self._zone_weights = (bass, mid, high)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        w = self.width()
        h = self.height()

        for x in range(w):
            t = _remap_t(x / max(1, w - 1), self._zone_weights)
            r, g, b = rainbow_color_at(t)
            painter.setPen(QColor(r, g, b))
            painter.drawLine(x, 0, x, h)

        # 대역 경계선
        bp = self._zone_weights[0] / 100.0
        mp = self._zone_weights[1] / 100.0
        painter.setPen(QColor(255, 255, 255, 120))
        painter.drawLine(int(bp * w), 0, int(bp * w), h)
        painter.drawLine(int((bp + mp) * w), 0, int((bp + mp) * w), h)
        painter.end()
