"""Flowing Palette 프리뷰 위젯 — 현재 추출된 5개 색상 표시.

HybridPanel의 Flowing 설정 섹션에 배치.
엔진에서 palette가 갱신될 때 set_colors()를 호출하여 업데이트.
"""

from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PySide6.QtGui import QPainter, QColor, QBrush, QPen
from PySide6.QtCore import Qt


class FlowPalettePreview(QWidget):
    """5개 색상 스와치를 가로로 나열하는 프리뷰 위젯.

    사용법:
        preview = FlowPalettePreview()
        preview.set_colors([(255,0,0), (0,255,0), (0,0,255), (255,255,0), (128,0,255)])
        # 또는 ratios(면적 비율)도 전달하면 스와치 폭이 비례
        preview.set_colors(colors, ratios=[0.4, 0.25, 0.15, 0.12, 0.08])
    """

    def __init__(self, n_swatches=5, parent=None):
        super().__init__(parent)
        self._n = n_swatches
        self._colors = [(80, 80, 80)] * n_swatches  # 초기: 회색
        self._ratios = [1.0 / n_swatches] * n_swatches
        self.setFixedHeight(28)
        self.setMinimumWidth(150)

    def set_colors(self, colors, ratios=None):
        """팔레트 색상 갱신.

        Args:
            colors: list of (R, G, B) tuples or (N, 3) array — RGB 0~255
            ratios: list of float 또는 None — 면적 비율 (스와치 폭 결정)
        """
        n = min(len(colors), self._n)
        self._colors = []
        for i in range(n):
            c = colors[i]
            self._colors.append((int(c[0]), int(c[1]), int(c[2])))
        # 부족하면 마지막 색으로 채움
        while len(self._colors) < self._n:
            self._colors.append(self._colors[-1] if self._colors else (80, 80, 80))

        if ratios is not None and len(ratios) >= n:
            self._ratios = [float(ratios[i]) for i in range(n)]
            # 부족분 채움
            while len(self._ratios) < self._n:
                self._ratios.append(0.05)
            # 정규화
            total = sum(self._ratios)
            if total > 0:
                self._ratios = [r / total for r in self._ratios]
        else:
            self._ratios = [1.0 / self._n] * self._n

        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width() - 2  # 양쪽 1px 여백
        h = self.height() - 2
        gap = 3
        total_gap = gap * (self._n - 1)
        available = w - total_gap

        x = 1
        for i in range(self._n):
            sw = max(8, int(available * self._ratios[i]))
            r, g, b = self._colors[i]

            # 스와치 배경
            p.setBrush(QBrush(QColor(r, g, b)))
            p.setPen(QPen(QColor(60, 60, 60), 1))
            p.drawRoundedRect(int(x), 1, sw, h, 4, 4)

            x += sw + gap

        p.end()
