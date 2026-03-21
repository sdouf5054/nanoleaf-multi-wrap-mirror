"""컴팩트 에너지 바 — Bass/Mid/High 3색 바 (라벨 없음, 풀 폭)

QPainter 기반. 높이 14px. 색상으로 구분 (빨강=Bass, 초록=Mid, 파랑=High).
라벨을 제거하여 왼쪽 여백 없이 바가 전체 폭을 사용.

사용법:
    bar = CompactEnergyBar()
    bar.set_values(0.8, 0.4, 0.2)
"""

from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QColor, QBrush
from PySide6.QtCore import Qt, QRectF


_COLOR_BASS = QColor("#e74c3c")
_COLOR_MID = QColor("#27ae60")
_COLOR_HIGH = QColor("#3498db")
_COLOR_BG = QColor(55, 55, 60)


class CompactEnergyBar(QWidget):
    """Bass/Mid/High 에너지 — 3색 바, 라벨 없음, 풀 폭."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bass = 0.0
        self._mid = 0.0
        self._high = 0.0
        self.setFixedHeight(14)
        self.setMinimumWidth(100)

    def set_values(self, bass, mid, high):
        self._bass = max(0.0, min(1.0, bass))
        self._mid = max(0.0, min(1.0, mid))
        self._high = max(0.0, min(1.0, high))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        gap = 4            # 바 사이 간격
        bar_h = 8          # 바 높이
        bar_r = 3          # 라운드

        sections = [
            (self._bass, _COLOR_BASS),
            (self._mid, _COLOR_MID),
            (self._high, _COLOR_HIGH),
        ]
        n = len(sections)
        total_gap = gap * (n - 1)
        bar_w = (w - total_gap) / n

        bar_y = (h - bar_h) / 2.0
        x = 0.0

        for value, color in sections:
            # 배경
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(_COLOR_BG))
            p.drawRoundedRect(QRectF(x, bar_y, bar_w, bar_h), bar_r, bar_r)

            # 값
            fill_w = bar_w * value
            if fill_w > 1:
                p.setBrush(QBrush(color))
                p.drawRoundedRect(QRectF(x, bar_y, fill_w, bar_h), bar_r, bar_r)

            x += bar_w + gap

        p.end()