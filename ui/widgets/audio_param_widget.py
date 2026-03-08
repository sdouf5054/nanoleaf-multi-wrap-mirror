"""오디오 파라미터 위젯 — 감도/밝기/Attack/Release/대역 비율 공용

오디오 패널과 하이브리드 패널에서 동일한 파라미터 슬라이더 세트를
중복 없이 공유합니다.

사용법:
    widget = AudioParamWidget()
    widget.params_changed.connect(handler)

    # 값 읽기
    params = widget.get_params()

    # 값 쓰기 (시그널 발생 없이)
    widget.set_params({"bass_sens": 120, "attack": 70, ...})

    # 서브모드 전환 시 표시/숨김
    widget.set_audio_mode("spectrum")  # mid/high 감도 + 대역 비율 표시
    widget.set_audio_mode("pulse")     # bass 감도만 표시
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)
from PyQt5.QtCore import Qt, pyqtSignal

from ui.widgets.no_scroll_slider import NoScrollSlider
from ui.widgets.zone_balance import ZoneBalanceWidget


# ── 오디오 모드별 기본 파라미터 ───────────────────────────────────
AUDIO_DEFAULTS = {
    "pulse": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100, "attack": 50, "release": 50,
        "zone_bass": 33, "zone_mid": 33, "zone_high": 34,
    },
    "spectrum": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100, "attack": 50, "release": 50,
        "zone_bass": 33, "zone_mid": 33, "zone_high": 34,
    },
    "bass_detail": {
        "bass_sens": 100, "mid_sens": 100, "high_sens": 100,
        "brightness": 100, "attack": 10, "release": 70,
        "zone_bass": 48, "zone_mid": 26, "zone_high": 26,
    },
}


class AudioParamWidget(QWidget):
    """감도/밝기/Attack/Release/대역 비율 슬라이더 세트.

    Signals:
        params_changed(): 아무 슬라이더 값이 변경되었을 때
    """

    params_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._updating = False  # set_params 중 시그널 억제
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        # ── 감도 ──
        self.label_sens = QLabel("감도 (Bass)")
        layout.addWidget(self.label_sens)
        self.slider_bass_sens, self.lbl_bass_sens = \
            self._add_slider(layout, "Bass:", 10, 300, 100)

        # Mid 감도 (spectrum/bass_detail 전용)
        self.row_mid_sens = QWidget()
        rm = QHBoxLayout(self.row_mid_sens)
        rm.setContentsMargins(0, 0, 0, 0)
        rm.addWidget(QLabel("Mid:"))
        self.slider_mid_sens = NoScrollSlider(Qt.Horizontal)
        self.slider_mid_sens.setRange(10, 300)
        self.slider_mid_sens.setValue(100)
        self.slider_mid_sens.valueChanged.connect(self._on_changed)
        rm.addWidget(self.slider_mid_sens)
        self.lbl_mid_sens = QLabel("1.00")
        self.lbl_mid_sens.setMinimumWidth(40)
        self.lbl_mid_sens.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        rm.addWidget(self.lbl_mid_sens)
        layout.addWidget(self.row_mid_sens)

        # High 감도 (spectrum/bass_detail 전용)
        self.row_high_sens = QWidget()
        rh = QHBoxLayout(self.row_high_sens)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(QLabel("High:"))
        self.slider_high_sens = NoScrollSlider(Qt.Horizontal)
        self.slider_high_sens.setRange(10, 300)
        self.slider_high_sens.setValue(100)
        self.slider_high_sens.valueChanged.connect(self._on_changed)
        rh.addWidget(self.slider_high_sens)
        self.lbl_high_sens = QLabel("1.00")
        self.lbl_high_sens.setMinimumWidth(40)
        self.lbl_high_sens.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        rh.addWidget(self.lbl_high_sens)
        layout.addWidget(self.row_high_sens)

        # ── 밝기 ──
        ln = QFrame()
        ln.setFrameShape(QFrame.HLine)
        ln.setFrameShadow(QFrame.Sunken)
        layout.addWidget(ln)
        self.slider_brightness, self.lbl_brightness = \
            self._add_slider(layout, "밝기:", 0, 100, 100, suffix="%")

        # ── 반응 특성 ──
        layout.addWidget(QLabel("반응 특성"))
        self.slider_attack, self.lbl_attack = \
            self._add_slider(layout, "Attack:", 0, 100, 50)
        self.slider_release, self.lbl_release = \
            self._add_slider(layout, "Release:", 0, 100, 50)

        # ── 대역 비율 (spectrum/bass_detail 전용) ──
        self.zone_line = QFrame()
        self.zone_line.setFrameShape(QFrame.HLine)
        self.zone_line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(self.zone_line)
        self.zone_label = QLabel("대역 비율 (주파수 분배)")
        self.zone_label.setStyleSheet("font-weight:bold;")
        layout.addWidget(self.zone_label)
        self.zone_balance = ZoneBalanceWidget(33, 33, 34)
        self.zone_balance.zone_changed.connect(lambda *_: self._on_changed())
        layout.addWidget(self.zone_balance)

        # spectrum 전용 위젯 목록
        self._spectrum_only = [
            self.row_mid_sens, self.row_high_sens,
            self.zone_line, self.zone_label, self.zone_balance,
        ]

    def _add_slider(self, parent_layout, label_text, min_v, max_v,
                    default, suffix=""):
        """슬라이더 + 라벨 한 줄."""
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        s = NoScrollSlider(Qt.Horizontal)
        s.setRange(min_v, max_v)
        s.setValue(default)
        s.valueChanged.connect(self._on_changed)
        row.addWidget(s)
        lbl = QLabel(
            f"{default}{suffix}" if suffix == "%" else f"{default / 100:.2f}"
        )
        lbl.setMinimumWidth(40)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(lbl)
        parent_layout.addLayout(row)
        return s, lbl

    def _on_changed(self, _=None):
        """아무 슬라이더 변경 → 라벨 갱신 + 시그널."""
        if self._updating:
            return
        self._update_labels()
        self.params_changed.emit()

    def _update_labels(self):
        """모든 라벨을 현재 슬라이더 값으로 갱신."""
        self.lbl_bass_sens.setText(f"{self.slider_bass_sens.value() / 100:.2f}")
        self.lbl_mid_sens.setText(f"{self.slider_mid_sens.value() / 100:.2f}")
        self.lbl_high_sens.setText(f"{self.slider_high_sens.value() / 100:.2f}")
        self.lbl_brightness.setText(f"{self.slider_brightness.value()}%")
        self.lbl_attack.setText(f"{self.slider_attack.value() / 100:.2f}")
        self.lbl_release.setText(f"{self.slider_release.value() / 100:.2f}")

    # ── 서브모드에 따른 표시/숨김 ────────────────────────────────

    def set_audio_mode(self, mode_name):
        """pulse/spectrum/bass_detail에 따라 위젯 표시/숨김."""
        is_banded = mode_name in ("spectrum", "bass_detail")
        for w in self._spectrum_only:
            w.setVisible(is_banded)
        if mode_name == "bass_detail":
            self.label_sens.setText("감도 (Bass Detail)")
        elif mode_name == "spectrum":
            self.label_sens.setText("감도 (대역별)")
        else:
            self.label_sens.setText("감도 (Bass)")

    # ── 값 읽기/쓰기 ────────────────────────────────────────────

    def get_params(self):
        """현재 슬라이더 값을 dict로 반환."""
        zb, zm, zh = self.zone_balance.get_values()
        return {
            "bass_sens": self.slider_bass_sens.value(),
            "mid_sens": self.slider_mid_sens.value(),
            "high_sens": self.slider_high_sens.value(),
            "brightness": self.slider_brightness.value(),
            "attack": self.slider_attack.value(),
            "release": self.slider_release.value(),
            "zone_bass": zb,
            "zone_mid": zm,
            "zone_high": zh,
        }

    def set_params(self, d, defaults=None):
        """dict에서 슬라이더 값 설정 (시그널 발생 없이).

        Args:
            d: config에서 읽은 파라미터 dict
            defaults: 키가 없을 때 사용할 기본값 dict
        """
        self._updating = True
        df = defaults or AUDIO_DEFAULTS["pulse"]
        self.slider_bass_sens.setValue(d.get("bass_sens", df["bass_sens"]))
        self.slider_mid_sens.setValue(d.get("mid_sens", df["mid_sens"]))
        self.slider_high_sens.setValue(d.get("high_sens", df["high_sens"]))
        self.slider_brightness.setValue(d.get("brightness", df["brightness"]))
        self.slider_attack.setValue(d.get("attack", df["attack"]))
        self.slider_release.setValue(d.get("release", df["release"]))
        self.zone_balance.set_values(
            d.get("zone_bass", df["zone_bass"]),
            d.get("zone_mid", df["zone_mid"]),
            d.get("zone_high", df["zone_high"]),
        )
        self._update_labels()
        self._updating = False

    def save_to_dict(self, d):
        """현재 값을 dict에 저장."""
        p = self.get_params()
        d.update(p)
