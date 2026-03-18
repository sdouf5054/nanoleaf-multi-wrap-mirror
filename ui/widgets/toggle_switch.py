"""토글 스위치 — iOS 스타일 커스텀 QCheckBox

QCheckBox를 상속하여 기존 시그널(stateChanged, toggled) 사용 가능.
스타일시트 indicator 대신 직접 그려서 QPainter engine==0 경고 방지.

사용법:
    toggle = ToggleSwitch("디스플레이 미러링")
    toggle.toggled.connect(on_toggled)
    toggle.setChecked(True)
"""

from PySide6.QtWidgets import QCheckBox
from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QBrush


class ToggleSwitch(QCheckBox):
    """iOS 스타일 토글 스위치 — 커스텀 paintEvent."""

    _TRACK_W = 38
    _TRACK_H = 20
    _KNOB_MARGIN = 2
    _SPACING = 8

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        # indicator를 숨기고 직접 그림
        self.setStyleSheet("""
            QCheckBox {
                spacing: 8px;
                font-size: 13px;
                color: #d0d0d0;
            }
            QCheckBox::indicator {
                width: 0px;
                height: 0px;
                margin: 0px;
                padding: 0px;
                border: none;
                background: transparent;
            }
        """)

    def sizeHint(self):
        base = super().sizeHint()
        # 트랙 너비 + 간격 + 텍스트 너비
        w = self._TRACK_W + self._SPACING + base.width()
        h = max(self._TRACK_H + 4, base.height())
        return QSize(w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        checked = self.isChecked()
        tw, th = self._TRACK_W, self._TRACK_H
        km = self._KNOB_MARGIN
        knob_d = th - 2 * km

        # 트랙 위치: 수직 중앙
        y = (self.height() - th) / 2

        # ── 트랙 ──
        track_rect = QRectF(0, y, tw, th)
        if checked:
            track_color = QColor("#2e86c1")
        else:
            track_color = QColor("#3a3a42")
        painter.setPen(QPen(QColor("#555") if not checked else track_color, 1))
        painter.setBrush(QBrush(track_color))
        painter.drawRoundedRect(track_rect, th / 2, th / 2)

        # ── 놉 ──
        if checked:
            knob_x = tw - km - knob_d
        else:
            knob_x = km
        knob_rect = QRectF(knob_x, y + km, knob_d, knob_d)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(knob_rect)

        # ── 텍스트 ──
        text = self.text()
        if text:
            painter.setPen(QColor("#000000"))
            font = self.font()
            painter.setFont(font)
            text_x = tw + self._SPACING
            text_rect = QRectF(text_x, 0, self.width() - text_x, self.height())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, text)

        painter.end()

    def hitButton(self, pos):
        """클릭 영역을 위젯 전체로."""
        return self.rect().contains(pos)
