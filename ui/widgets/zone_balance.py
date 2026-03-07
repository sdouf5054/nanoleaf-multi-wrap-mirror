"""대역 비율(Zone Balance) 조절 위젯

Bass/Mid/High 세 슬라이더가 합계 100%를 유지하면서
비율을 조절합니다. 상단에 GradientPreview로 색상 분배를 시각화.
"""

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PyQt5.QtCore import Qt, pyqtSignal

from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.gradient_preview import GradientPreview


class ZoneBalanceWidget(QWidget):
    """Bass/Mid/High 대역 비율 조절 — 합계 100% 유지.

    Signals:
        zone_changed(int, int, int): (bass, mid, high) 비율 변경 시
    """

    zone_changed = pyqtSignal(int, int, int)
    MIN_ZONE = 5

    def __init__(self, bass=33, mid=33, high=34, parent=None):
        super().__init__(parent)
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.gradient_preview = GradientPreview()
        layout.addWidget(self.gradient_preview)

        self._sliders = {}
        self._labels = {}

        for name, default, color in [
            ("Bass", bass, "#e74c3c"),
            ("Mid", mid, "#27ae60"),
            ("High", high, "#3498db"),
        ]:
            row = QHBoxLayout()
            ln = QLabel(f"{name}:")
            ln.setMinimumWidth(35)
            ln.setStyleSheet(f"color:{color};font-weight:bold;")
            row.addWidget(ln)

            s = NoScrollSlider(Qt.Horizontal)
            s.setRange(self.MIN_ZONE, 100 - 2 * self.MIN_ZONE)
            s.setValue(default)
            s.valueChanged.connect(lambda v, n=name: self._on_slider_changed(n, v))
            row.addWidget(s)

            lv = QLabel(f"{default}%")
            lv.setMinimumWidth(35)
            lv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(lv)

            layout.addLayout(row)
            self._sliders[name] = s
            self._labels[name] = lv

        self._update_gradient()

    def _on_slider_changed(self, changed_name, new_value):
        if self._updating:
            return
        self._updating = True

        names = ["Bass", "Mid", "High"]
        others = [n for n in names if n != changed_name]

        ov = {n: self._sliders[n].value() for n in others}
        os_ = sum(ov.values())
        rem = 100 - new_value

        if os_ == 0:
            for n in others:
                self._sliders[n].setValue(rem // 2)
        else:
            for n in others:
                self._sliders[n].setValue(
                    max(self.MIN_ZONE, int(round(rem * ov[n] / os_)))
                )

        vals = {n: self._sliders[n].value() for n in names}
        diff = 100 - sum(vals.values())
        if diff != 0:
            for n in others:
                a = vals[n] + diff
                if self.MIN_ZONE <= a <= 100 - 2 * self.MIN_ZONE:
                    self._sliders[n].setValue(a)
                    break

        for n in names:
            self._labels[n].setText(f"{self._sliders[n].value()}%")

        self._update_gradient()
        self._updating = False

        b, m, h = self.get_values()
        self.zone_changed.emit(b, m, h)

    def _update_gradient(self):
        b, m, h = self.get_values()
        self.gradient_preview.set_zone_weights(b, m, h)

    def get_values(self):
        """현재 (bass, mid, high) 비율 반환."""
        return (
            self._sliders["Bass"].value(),
            self._sliders["Mid"].value(),
            self._sliders["High"].value(),
        )

    def set_values(self, bass, mid, high):
        """비율 설정 — 시그널 발생 없이."""
        self._updating = True
        self._sliders["Bass"].setValue(bass)
        self._sliders["Mid"].setValue(mid)
        self._sliders["High"].setValue(high)
        for n in ["Bass", "Mid", "High"]:
            self._labels[n].setText(f"{self._sliders[n].value()}%")
        self._update_gradient()
        self._updating = False

    def setEnabled(self, enabled):
        for s in self._sliders.values():
            s.setEnabled(enabled)
