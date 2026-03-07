"""스펙트럼 바 차트 위젯

16밴드(또는 N밴드) 오디오 스펙트럼을 컬러 막대 그래프로 시각화합니다.
대역 비율(zone_weights)에 따라 각 밴드의 색상이 결정됩니다.
"""

import numpy as np
from PyQt5.QtWidgets import QWidget
from PyQt5.QtGui import QPainter, QColor, QBrush
from PyQt5.QtCore import Qt

from core.audio_visualizer import _remap_t
from ui.widgets.gradient_preview import rainbow_color_at


class SpectrumWidget(QWidget):
    """N밴드 스펙트럼 바 차트.

    set_values()로 각 밴드의 에너지(0~1)를 전달하면
    색상 막대로 시각화합니다.
    """

    def __init__(self, n_bands=16, parent=None):
        super().__init__(parent)
        self.n_bands = n_bands
        self._values = np.zeros(n_bands)
        self._zone_weights = (33, 33, 34)
        self.setMinimumHeight(50)
        self.setMaximumHeight(70)

    def set_values(self, values):
        """밴드별 에너지 값 설정 (0~1 범위)."""
        self._values = np.clip(values, 0, 1)
        self.update()

    def set_zone_weights(self, bass, mid, high):
        """대역 비율 변경 — 밴드 색상에 영향."""
        self._zone_weights = (bass, mid, high)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        n = self.n_bands

        bw = max(2, (w - n + 1) // n)
        gap = max(1, (w - bw * n) // max(1, n - 1))

        for i in range(n):
            v = self._values[i] if i < len(self._values) else 0
            bh = max(1, int(v * (h - 4)))
            x = i * (bw + gap)
            y = h - bh - 2
            t = _remap_t(i / max(1, n - 1), self._zone_weights)
            r, g, b = rainbow_color_at(t)
            p.setBrush(QBrush(QColor(r, g, b)))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(x, y, bw, bh, 2, 2)

        p.end()
