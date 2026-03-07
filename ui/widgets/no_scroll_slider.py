"""스크롤 방지 슬라이더

스크롤 영역 안에서 슬라이더가 의도치 않게 변경되는 것을 방지합니다.
포커스가 있을 때만 마우스 휠 입력을 받습니다.
"""

from PyQt5.QtWidgets import QSlider
from PyQt5.QtCore import Qt


class NoScrollSlider(QSlider):
    """포커스 없을 때 wheelEvent를 무시하는 QSlider."""

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
        else:
            super().wheelEvent(event)
