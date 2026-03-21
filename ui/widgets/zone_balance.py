"""대역 비율(Zone Balance) 조절 위젯 — Bass/Mid/High 합계 100%.

[QSS 테마] 라벨 색상을 palette에서 읽도록 전환.
  - 하드코딩 "#e74c3c" 등 → palette의 bar_bass/bar_mid/bar_high
  - 테마 전환 시 에너지 바와 라벨 색상이 동시에 반영
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal

from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.gradient_preview import GradientPreview
from styles.palette import current as _pal_current


class ZoneBalanceWidget(QWidget):
    zone_changed = Signal(int, int, int)
    MIN_ZONE = 5

    def __init__(self, bass=33, mid=33, high=34, parent=None):
        super().__init__(parent)
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.gradient_preview = GradientPreview()
        layout.addWidget(self.gradient_preview)

        # ★ palette에서 색상 읽기
        pal = _pal_current()
        _BAND_COLORS = {
            "Bass": pal["bar_bass"],
            "Mid":  pal["bar_mid"],
            "High": pal["bar_high"],
        }

        self._sliders = {}
        self._labels = {}
        self._color_labels = {}  # ★ 테마 전환 시 갱신용 참조 보관
        for name, default in [("Bass", bass), ("Mid", mid), ("High", high)]:
            color = _BAND_COLORS[name]
            row = QHBoxLayout()
            ln = QLabel(f"{name}:")
            ln.setMinimumWidth(35)
            # ★ palette 참조 — 인라인이지만 palette에서 읽은 값
            ln.setStyleSheet(f"color:{color};font-weight:bold;")
            row.addWidget(ln)
            s = NoScrollSlider(Qt.Orientation.Horizontal)
            s.setRange(self.MIN_ZONE, 100 - 2 * self.MIN_ZONE)
            s.setValue(default)
            s.valueChanged.connect(lambda v, n=name: self._on_slider_changed(n, v))
            row.addWidget(s)
            lv = QLabel(f"{default}%")
            lv.setMinimumWidth(35)
            lv.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(lv)
            layout.addLayout(row)
            self._sliders[name] = s
            self._labels[name] = lv
            self._color_labels[name] = ln
        self._update_gradient()

    def _refresh_label_colors(self):
        """★ 테마 전환 시 라벨 색상을 palette에서 다시 읽어 적용."""
        pal = _pal_current()
        _BAND_COLORS = {
            "Bass": pal["bar_bass"],
            "Mid":  pal["bar_mid"],
            "High": pal["bar_high"],
        }
        for name, ln in self._color_labels.items():
            ln.setStyleSheet(f"color:{_BAND_COLORS[name]};font-weight:bold;")

    def showEvent(self, event):
        """위젯이 표시될 때 palette 색상 갱신 — 테마 전환 반영."""
        super().showEvent(event)
        self._refresh_label_colors()

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
                self._sliders[n].setValue(max(self.MIN_ZONE, int(round(rem * ov[n] / os_))))
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
        self.zone_changed.emit(*self.get_values())

    def _update_gradient(self):
        b, m, h = self.get_values()
        self.gradient_preview.set_zone_weights(b, m, h)

    def get_values(self):
        return (self._sliders["Bass"].value(), self._sliders["Mid"].value(), self._sliders["High"].value())

    def set_values(self, bass, mid, high):
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
