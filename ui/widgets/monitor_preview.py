"""모니터 LED 프리뷰 위젯.

[QSS 테마] paintEvent 색상을 palette에서 읽도록 전환.
  - 모니터 배경: preview_monitor_bg
  - LED 미할당 색: preview_led_off
  - LED 테두리: preview_led_border
"""

import numpy as np
from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QColor, QBrush
from PySide6.QtCore import Qt

from core.layout import get_led_positions
from styles.palette import current as _pal_current


class MonitorPreview(QWidget):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._led_colors = None
        self._zone_map = None
        self._led_count = config["device"]["led_count"]
        self._positions = None
        self._sides = None
        self.setMinimumHeight(220)
        self.setMaximumHeight(360)
        self.setMinimumWidth(400)
        self._compute_positions()

    def _compute_positions(self):
        lc = self._config["layout"]
        mc = self._config.get("mirror", {})
        sw = mc.get("grid_cols", 64) * 40
        sh = mc.get("grid_rows", 32) * 40
        pos, sides = get_led_positions(sw, sh, lc["segments"], self._led_count,
                                        orientation=mc.get("orientation", "auto"),
                                        portrait_rotation=mc.get("portrait_rotation", "cw"))
        self._positions = np.zeros_like(pos)
        if sw > 0: self._positions[:, 0] = pos[:, 0] / sw
        if sh > 0: self._positions[:, 1] = pos[:, 1] / sh
        self._sides = sides

        self._wrap_index = np.zeros(self._led_count, dtype=np.int32)
        sc = {}
        for seg in lc["segments"]:
            s, e, side = seg["start"], seg["end"], seg["side"]
            n = abs(s - e)
            if n == 0: continue
            w = sc.get(side, 0)
            sc[side] = w + 1
            step = -1 if s > e else 1
            for i in range(n):
                idx = s + step * i
                if 0 <= idx < self._led_count:
                    self._wrap_index[idx] = w

    def set_colors(self, colors):
        if colors is not None and len(colors) > 0:
            self._led_colors = np.clip(colors, 0, 255).astype(np.float32)
            self.update()

    def _get_led_color(self, li):
        if self._led_colors is None:
            return None  # ★ None → paintEvent에서 palette 참조
        nc = len(self._led_colors)
        if nc >= self._led_count:
            c = self._led_colors[li]
        elif self._zone_map is not None:
            c = self._led_colors[self._zone_map[li] % nc]
        elif nc == 1:
            c = self._led_colors[0]
        else:
            return None
        return (int(c[0]), int(c[1]), int(c[2]))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ★ palette에서 색상 읽기 — 테마 전환 즉시 반영
        pal = _pal_current()
        monitor_bg = QColor(pal["preview_monitor_bg"])
        led_off_color = QColor(pal["preview_led_off"])
        led_border_color = QColor(pal["preview_led_border"])

        w, h = self.width(), self.height()
        ls, wg = 11, 19
        lm = wg * 2 + 20
        aw, ah = w - 2 * lm, h - 2 * lm
        if aw < 40 or ah < 20:
            p.end(); return
        asp = 16.0 / 9.0
        if aw / ah > asp:
            mh = ah; mw = int(mh * asp)
        else:
            mw = aw; mh = int(mw / asp)
        mx, my = (w - mw) // 2, (h - mh) // 2

        # ★ 모니터 배경 — palette 참조
        p.setPen(led_border_color)
        p.setBrush(QBrush(monitor_bg))
        p.drawRoundedRect(mx, my, mw, mh, 3, 3)

        if self._positions is None:
            p.end(); return
        hl = ls // 2
        for i in range(self._led_count):
            nx, ny = self._positions[i]
            side = self._sides[i]
            wrap = self._wrap_index[i]
            px, py = mx + nx * mw, my + ny * mh
            d = wg * (2 - wrap)
            if side == "top": py = my - d
            elif side == "bottom": py = my + mh + d - ls
            elif side == "left": px = mx - d
            elif side == "right": px = mx + mw + d - ls

            rgb = self._get_led_color(i)
            if rgb is not None:
                r, g, b = rgb
                p.setBrush(QBrush(QColor(r, g, b)))
            else:
                # ★ LED 미할당 색 — palette 참조
                p.setBrush(QBrush(led_off_color))

            # ★ LED 테두리 — palette 참조
            p.setPen(led_border_color)
            p.drawRoundedRect(int(px - hl), int(py - hl), ls, ls, 2, 2)
        p.end()
