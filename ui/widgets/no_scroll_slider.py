"""스크롤 방지 슬라이더 — 포커스가 있을 때만 마우스 휠 입력을 받음."""

from PySide6.QtWidgets import QSlider
from PySide6.QtCore import Qt


class NoScrollSlider(QSlider):
    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
        else:
            super().wheelEvent(event)
