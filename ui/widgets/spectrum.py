"""스펙트럼 바 차트 위젯."""

import numpy as np
from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QColor, QBrush
from PySide6.QtCore import Qt

from core.engine_utils import _remap_t
from ui.widgets.gradient_preview import rainbow_color_at


class SpectrumWidget(QWidget):
    def __init__(self, n_bands=16, parent=None):
        super().__init__(parent)
        self.n_bands = n_bands
        self._values = np.zeros(n_bands)
        self._zone_weights = (33, 33, 34)
        self.setMinimumHeight(50)
        self.setMaximumHeight(70)

    def set_values(self, values):
        self._values = np.clip(values, 0, 1)
        self.update()

    def set_zone_weights(self, bass, mid, high):
        self._zone_weights = (bass, mid, high)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, n = self.width(), self.height(), self.n_bands
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
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(x, y, bw, bh, 2, 2)
        p.end()
