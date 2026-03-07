"""오디오 비주얼라이저 탭 v9

[변경 v9 — UI 재배치 + 최소밝기]
- ★ UI 순서: 상태 → 에너지 → 버튼 → 색상소스 → [색상] → 모드 → 파라미터 → 디바이스
- ★ 최소밝기 슬라이더: 화면 연동 시 LED 완전 소등 방지
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider,
    QLabel, QGroupBox, QComboBox, QColorDialog, QProgressBar,
    QFrame, QGridLayout, QMessageBox, QScrollArea
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QPainter, QBrush
import numpy as np
import os
import psutil

from core.audio_engine import list_loopback_devices, HAS_PYAUDIO
from core.audio_visualizer import MODE_PULSE, MODE_SPECTRUM, MODE_BASS_DETAIL, _remap_t
from core.hybrid_visualizer import (
    HybridVisualizer, COLOR_SOURCE_SOLID, COLOR_SOURCE_SCREEN,
    N_ZONES_PER_LED, _build_led_zone_map_by_side,
)
from core.layout import get_led_positions
from core.config import save_config

COLOR_PRESETS = [
    ("🌈 무지개", None, None, None),
    ("핑크/마젠타", 255, 0, 80), ("빨강", 255, 30, 0),
    ("주황", 255, 120, 0), ("노랑", 255, 220, 0),
    ("초록", 0, 255, 80), ("시안", 0, 220, 255),
    ("파랑", 30, 0, 255), ("보라", 150, 0, 255),
    ("흰색", 255, 255, 255),
]

MODE_DEFAULTS = {
    "pulse": {"bass_sens": 100, "mid_sens": 100, "high_sens": 100,
              "brightness": 100, "attack": 50, "release": 50,
              "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "spectrum": {"bass_sens": 100, "mid_sens": 100, "high_sens": 100,
                 "brightness": 100, "attack": 50, "release": 50,
                 "zone_bass": 33, "zone_mid": 33, "zone_high": 34},
    "bass_detail": {"bass_sens": 100, "mid_sens": 100, "high_sens": 100,
                    "brightness": 100, "attack": 10, "release": 70,
                    "zone_bass": 48, "zone_mid": 26, "zone_high": 26},
}

RAINBOW_KEYPOINTS = [
    (0.000, 255, 0, 0), (0.130, 255, 127, 0), (0.260, 255, 255, 0),
    (0.400, 0, 255, 0), (0.540, 0, 180, 255), (0.680, 0, 50, 255),
    (0.820, 80, 0, 255), (1.000, 160, 0, 220),
]

ZONE_OPTIONS = [
    (1, "1구역 (화면 전체 평균)"), (2, "2구역 (상/하)"),
    (4, "4구역 (상하좌우)"), (8, "8구역 (모서리 포함)"),
    (16, "16구역"), (32, "32구역"),
    (N_ZONES_PER_LED, "LED별 개별 (미러링)"),
]


def rainbow_color_at(t):
    t = max(0.0, min(1.0, t))
    for i in range(len(RAINBOW_KEYPOINTS) - 1):
        t0, r0, g0, b0 = RAINBOW_KEYPOINTS[i]
        t1, r1, g1, b1 = RAINBOW_KEYPOINTS[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return (int(r0+(r1-r0)*f), int(g0+(g1-g0)*f), int(b0+(b1-b0)*f))
    return (160, 0, 220)


def _ensure_audio_config(config):
    for mode_key in ("audio_pulse", "audio_spectrum", "audio_bass_detail"):
        if mode_key not in config:
            mode_name = mode_key.replace("audio_", "")
            config[mode_key] = dict(MODE_DEFAULTS.get(mode_name, MODE_DEFAULTS["pulse"]))


class NoScrollSlider(QSlider):
    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.StrongFocus)
    def wheelEvent(self, event):
        if not self.hasFocus(): event.ignore()
        else: super().wheelEvent(event)


class GradientPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._zone_weights = (33, 33, 34)
        self.setFixedHeight(20); self.setMinimumWidth(100)
    def set_zone_weights(self, bass, mid, high):
        self._zone_weights = (bass, mid, high); self.update()
    def paintEvent(self, event):
        painter = QPainter(self); w = self.width(); h = self.height()
        for x in range(w):
            t = _remap_t(x / max(1, w-1), self._zone_weights)
            r, g, b = rainbow_color_at(t)
            painter.setPen(QColor(r, g, b)); painter.drawLine(x, 0, x, h)
        bp = self._zone_weights[0]/100.0; mp = self._zone_weights[1]/100.0
        painter.setPen(QColor(255, 255, 255, 120))
        painter.drawLine(int(bp*w), 0, int(bp*w), h)
        painter.drawLine(int((bp+mp)*w), 0, int((bp+mp)*w), h)
        painter.end()


class ZoneBalanceWidget(QWidget):
    zone_changed = pyqtSignal(int, int, int)
    MIN_ZONE = 5
    def __init__(self, bass=33, mid=33, high=34, parent=None):
        super().__init__(parent); self._updating = False
        layout = QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(4)
        self.gradient_preview = GradientPreview(); layout.addWidget(self.gradient_preview)
        self._sliders = {}; self._labels = {}
        for name, default, color in [("Bass",bass,"#e74c3c"),("Mid",mid,"#27ae60"),("High",high,"#3498db")]:
            row = QHBoxLayout()
            ln = QLabel(f"{name}:"); ln.setMinimumWidth(35); ln.setStyleSheet(f"color:{color};font-weight:bold;")
            row.addWidget(ln)
            s = NoScrollSlider(Qt.Horizontal); s.setRange(self.MIN_ZONE, 100-2*self.MIN_ZONE); s.setValue(default)
            s.valueChanged.connect(lambda v, n=name: self._on_slider_changed(n, v)); row.addWidget(s)
            lv = QLabel(f"{default}%"); lv.setMinimumWidth(35); lv.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
            row.addWidget(lv); layout.addLayout(row); self._sliders[name]=s; self._labels[name]=lv
        self._update_gradient()
    def _on_slider_changed(self, changed_name, new_value):
        if self._updating: return
        self._updating = True
        names=["Bass","Mid","High"]; others=[n for n in names if n!=changed_name]
        ov={n:self._sliders[n].value() for n in others}; os_=sum(ov.values()); rem=100-new_value
        if os_==0:
            for n in others: self._sliders[n].setValue(rem//2)
        else:
            for n in others: self._sliders[n].setValue(max(self.MIN_ZONE,int(round(rem*ov[n]/os_))))
        vals={n:self._sliders[n].value() for n in names}; diff=100-sum(vals.values())
        if diff!=0:
            for n in others:
                a=vals[n]+diff
                if self.MIN_ZONE<=a<=100-2*self.MIN_ZONE: self._sliders[n].setValue(a); break
        for n in names: self._labels[n].setText(f"{self._sliders[n].value()}%")
        self._update_gradient(); self._updating=False
        b,m,h=self.get_values(); self.zone_changed.emit(b,m,h)
    def _update_gradient(self): b,m,h=self.get_values(); self.gradient_preview.set_zone_weights(b,m,h)
    def get_values(self): return (self._sliders["Bass"].value(),self._sliders["Mid"].value(),self._sliders["High"].value())
    def set_values(self, bass, mid, high):
        self._updating=True
        self._sliders["Bass"].setValue(bass); self._sliders["Mid"].setValue(mid); self._sliders["High"].setValue(high)
        for n in ["Bass","Mid","High"]: self._labels[n].setText(f"{self._sliders[n].value()}%")
        self._update_gradient(); self._updating=False
    def setEnabled(self, enabled):
        for s in self._sliders.values(): s.setEnabled(enabled)


class SpectrumWidget(QWidget):
    def __init__(self, n_bands=16, parent=None):
        super().__init__(parent); self.n_bands=n_bands; self._values=np.zeros(n_bands)
        self._zone_weights=(33,33,34); self.setMinimumHeight(50); self.setMaximumHeight(70)
    def set_values(self, values): self._values=np.clip(values,0,1); self.update()
    def set_zone_weights(self, bass, mid, high): self._zone_weights=(bass,mid,high); self.update()
    def paintEvent(self, event):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); w=self.width(); h=self.height(); n=self.n_bands
        bw=max(2,(w-n+1)//n); gap=max(1,(w-bw*n)//max(1,n-1))
        for i in range(n):
            v=self._values[i] if i<len(self._values) else 0; bh=max(1,int(v*(h-4)))
            x=i*(bw+gap); y=h-bh-2; t=_remap_t(i/max(1,n-1),self._zone_weights)
            r,g,b=rainbow_color_at(t); p.setBrush(QBrush(QColor(r,g,b))); p.setPen(Qt.NoPen)
            p.drawRoundedRect(x,y,bw,bh,2,2)
        p.end()


class MonitorPreview(QWidget):
    def __init__(self, config, parent=None):
        super().__init__(parent); self._config=config; self._led_colors=None; self._zone_map=None
        self._led_count=config["device"]["led_count"]; self._positions=None; self._sides=None; self._n_zones=4
        self.setMinimumHeight(220); self.setMaximumHeight(360); self.setMinimumWidth(400)
        self._compute_positions()
    def _compute_positions(self):
        lc=self._config["layout"]; mc=self._config.get("mirror",{})
        sw=mc.get("grid_cols",64)*40; sh=mc.get("grid_rows",32)*40
        pos,sides=get_led_positions(sw,sh,lc["segments"],self._led_count,
            orientation=mc.get("orientation","auto"),portrait_rotation=mc.get("portrait_rotation","cw"))
        self._positions=np.zeros_like(pos)
        if sw>0: self._positions[:,0]=pos[:,0]/sw
        if sh>0: self._positions[:,1]=pos[:,1]/sh
        self._sides=sides; self._wrap_index=np.zeros(self._led_count,dtype=np.int32)
        sc={}
        for seg in lc["segments"]:
            s,e,side=seg["start"],seg["end"],seg["side"]; n=abs(s-e)
            if n==0: continue
            w=sc.get(side,0); sc[side]=w+1; step=-1 if s>e else 1
            for i in range(n):
                idx=s+step*i
                if 0<=idx<self._led_count: self._wrap_index[idx]=w
    def set_zone_map(self, zm): self._zone_map=zm
    def set_n_zones(self, n): self._n_zones=n
    def set_colors(self, colors):
        if colors is not None and len(colors)>0:
            self._led_colors=np.clip(colors,0,255).astype(np.float32); self.update()
    def _get_led_color(self, li):
        if self._led_colors is None: return (60,60,60)
        nc=len(self._led_colors)
        if nc>=self._led_count: c=self._led_colors[li]
        elif self._zone_map is not None: c=self._led_colors[self._zone_map[li]%nc]
        elif nc==1: c=self._led_colors[0]
        else: return (60,60,60)
        return (int(c[0]),int(c[1]),int(c[2]))
    def paintEvent(self, event):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); w=self.width(); h=self.height()
        ls=11; wg=ls+8; lm=wg*2+20; aw=w-2*lm; ah=h-2*lm
        if aw<40 or ah<20: p.end(); return
        asp=16.0/9.0
        if aw/ah>asp: mh=ah; mw=int(mh*asp)
        else: mw=aw; mh=int(mw/asp)
        mx=(w-mw)//2; my=(h-mh)//2
        p.setPen(QColor(80,80,80)); p.setBrush(QBrush(QColor(45,45,48))); p.drawRoundedRect(mx,my,mw,mh,3,3)
        if self._positions is None: p.end(); return
        hl=ls//2
        for i in range(self._led_count):
            nx,ny=self._positions[i]; side=self._sides[i]; wrap=self._wrap_index[i]
            px=mx+nx*mw; py=my+ny*mh; d=wg*(2-wrap)
            if side=="top": py=my-d
            elif side=="bottom": py=my+mh+d-ls
            elif side=="left": px=mx-d
            elif side=="right": px=mx+mw+d-ls
            r,g,b=self._get_led_color(i); p.setBrush(QBrush(QColor(r,g,b))); p.setPen(QColor(70,70,70))
            p.drawRoundedRect(int(px-hl),int(py-hl),ls,ls,2,2)
        p.end()


class AudioTab(QWidget):
    """오디오 비주얼라이저 탭 v9."""
    request_mirror_stop = pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent); self.config=config; _ensure_audio_config(config)
        self._visualizer=None; self._is_running=False; self._current_color=(255,0,80)
        self._edit_mode=False; self._current_mode_key="pulse"; self._switching_mode=False
        self._all_sliders=[]; self._build_ui(); self._load_mode_params("pulse")
        self._decay_timer=QTimer(self); self._decay_timer.setInterval(50)
        self._decay_timer.timeout.connect(self._decay_levels)
        self._process=psutil.Process(os.getpid()); self._process.cpu_percent()
        self._res_timer=QTimer(self); self._res_timer.timeout.connect(self._update_resource_usage)
        self._res_timer.start(2000)

    # ── 모드별 파라미터 ──────────────────────────────────────────

    def _config_key_for_mode(self, m): return f"audio_{m}"

    def _save_current_params_to_mode(self, mode_name):
        k=self._config_key_for_mode(mode_name)
        if k not in self.config: self.config[k]={}
        d=self.config[k]
        d["bass_sens"]=self.slider_bass_sens.value(); d["mid_sens"]=self.slider_mid_sens.value()
        d["high_sens"]=self.slider_high_sens.value(); d["brightness"]=self.slider_brightness.value()
        d["attack"]=self.slider_attack.value(); d["release"]=self.slider_release.value()
        zb,zm,zh=self.zone_balance.get_values()
        d["zone_bass"]=zb; d["zone_mid"]=zm; d["zone_high"]=zh

    def _load_mode_params(self, mode_name):
        k=self._config_key_for_mode(mode_name)
        df=MODE_DEFAULTS.get(mode_name, MODE_DEFAULTS["pulse"]); d=self.config.get(k, df)
        self._switching_mode=True
        self.slider_bass_sens.setValue(d.get("bass_sens",df["bass_sens"]))
        self.slider_mid_sens.setValue(d.get("mid_sens",df["mid_sens"]))
        self.slider_high_sens.setValue(d.get("high_sens",df["high_sens"]))
        self.slider_brightness.setValue(d.get("brightness",df["brightness"]))
        self.slider_attack.setValue(d.get("attack",df["attack"]))
        self.slider_release.setValue(d.get("release",df["release"]))
        self.zone_balance.set_values(d.get("zone_bass",df["zone_bass"]),
            d.get("zone_mid",df["zone_mid"]),d.get("zone_high",df["zone_high"]))
        self._switching_mode=False
        self._on_sens_changed(); self._on_brightness_changed(self.slider_brightness.value())
        self._on_ar_changed(); zb,zm,zh=self.zone_balance.get_values(); self._on_zone_changed(zb,zm,zh)
        self._current_mode_key=mode_name

    def _apply_params_to_visualizer(self):
        v=self._visualizer
        if not v: return
        v.brightness=self.slider_brightness.value()/100.0
        v.bass_sensitivity=self.slider_bass_sens.value()/100.0
        v.mid_sensitivity=self.slider_mid_sens.value()/100.0
        v.high_sensitivity=self.slider_high_sens.value()/100.0
        v.attack=self.slider_attack.value()/100.0; v.release=self.slider_release.value()/100.0
        b,m,h=self.zone_balance.get_values(); v.set_zone_weights(b,m,h)

    # ── UI 빌드 ──────────────────────────────────────────────────

    def _build_ui(self):
        scroll=QScrollArea(self); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        container=QWidget(); layout=QVBoxLayout(container); layout.setSpacing(8)
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.addWidget(scroll)
        scroll.setWidget(container)

        if not HAS_PYAUDIO:
            w=QLabel("⚠ PyAudioWPatch가 설치되지 않았습니다.\npip install PyAudioWPatch")
            w.setStyleSheet("color:#e74c3c;font-size:14px;padding:20px;"); w.setWordWrap(True)
            layout.addWidget(w); layout.addStretch(); return

        # ═══ 1. 상태 ═══
        sg=QGroupBox("상태"); sl=QHBoxLayout(sg)
        self.status_label=QLabel("대기 중"); self.status_label.setStyleSheet("font-size:14px;font-weight:bold;")
        sl.addWidget(self.status_label); sl.addStretch()
        self.cpu_label=QLabel("CPU: —%"); self.cpu_label.setStyleSheet("font-size:12px;color:#d35400;margin-right:6px;")
        sl.addWidget(self.cpu_label)
        self.ram_label=QLabel("RAM: — MB"); self.ram_label.setStyleSheet("font-size:12px;color:#27ae60;margin-right:10px;")
        sl.addWidget(self.ram_label)
        self.fps_label=QLabel("— fps"); self.fps_label.setStyleSheet("font-size:14px;color:#888;")
        sl.addWidget(self.fps_label); layout.addWidget(sg)

        # ═══ 2. 에너지 레벨 ═══
        lg=QGroupBox("에너지 레벨"); ll=QVBoxLayout(lg); bg=QGridLayout()
        self.bar_bass=self._make_bar(bg,0,"Bass","#e74c3c")
        self.bar_mid=self._make_bar(bg,1,"Mid","#27ae60")
        self.bar_high=self._make_bar(bg,2,"High","#3498db")
        ll.addLayout(bg); ll.addWidget(QLabel("스펙트럼 (16밴드, 로그 스케일)"))
        self.spectrum_widget=SpectrumWidget(n_bands=16); ll.addWidget(self.spectrum_widget)
        layout.addWidget(lg)

        # ═══ 3. 제어 버튼 ═══
        bl=QHBoxLayout()
        self.btn_start=QPushButton("▶ 시작"); self.btn_start.setMinimumHeight(40)
        self.btn_start.setStyleSheet("QPushButton{background:#8e44ad;color:white;font-size:14px;font-weight:bold;border-radius:6px}QPushButton:hover{background:#9b59b6}")
        self.btn_start.clicked.connect(self._start_visualizer); bl.addWidget(self.btn_start)
        self.btn_stop=QPushButton("⏹ 중지"); self.btn_stop.setMinimumHeight(40); self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("QPushButton{background:#c0392b;color:white;font-size:14px;font-weight:bold;border-radius:6px}QPushButton:hover{background:#e74c3c}QPushButton:disabled{background:#666}")
        self.btn_stop.clicked.connect(self._stop_visualizer); bl.addWidget(self.btn_stop)
        layout.addLayout(bl)

        # ═══ 4. 색상 소스 (★ 모드보다 위) ═══
        src_g=QGroupBox("색상 소스"); src_l=QVBoxLayout(src_g)
        self.combo_color_source=QComboBox()
        self.combo_color_source.addItems(["🎨 단색 / 무지개","🖥 화면 연동 — 화면색 + 오디오 밝기"])
        self.combo_color_source.currentIndexChanged.connect(self._on_color_source_changed)
        src_l.addWidget(self.combo_color_source)

        # 구역 수
        self.zone_count_row=QWidget(); zcr=QHBoxLayout(self.zone_count_row); zcr.setContentsMargins(0,0,0,0)
        zcr.addWidget(QLabel("구역 수:")); self.combo_zone_count=QComboBox()
        for n,label in ZONE_OPTIONS: self.combo_zone_count.addItem(label,n)
        self.combo_zone_count.currentIndexChanged.connect(self._on_zone_count_changed)
        zcr.addWidget(self.combo_zone_count); zcr.addStretch()
        src_l.addWidget(self.zone_count_row); self.zone_count_row.setVisible(False)

        # ★ 최소밝기
        self.min_bright_row=QWidget(); mbr=QHBoxLayout(self.min_bright_row); mbr.setContentsMargins(0,0,0,0)
        mbr.addWidget(QLabel("최소 밝기:"))
        self.slider_min_brightness=NoScrollSlider(Qt.Horizontal)
        self.slider_min_brightness.setRange(0,100); self.slider_min_brightness.setValue(5)
        self.slider_min_brightness.valueChanged.connect(self._on_min_brightness_changed)
        mbr.addWidget(self.slider_min_brightness)
        self.label_min_brightness=QLabel("5%"); self.label_min_brightness.setMinimumWidth(35)
        self.label_min_brightness.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        mbr.addWidget(self.label_min_brightness)
        src_l.addWidget(self.min_bright_row); self.min_bright_row.setVisible(False)

        # 프리뷰
        pr=QHBoxLayout()
        self.btn_preview=QPushButton("👁 프리뷰 보기"); self.btn_preview.setCheckable(True); self.btn_preview.setChecked(False)
        self.btn_preview.setFixedWidth(120)
        self.btn_preview.setStyleSheet("QPushButton{background:#34495e;color:#bdc3c7;border-radius:4px;padding:4px;font-size:11px;}QPushButton:checked{background:#2980b9;color:white;}")
        self.btn_preview.toggled.connect(self._on_preview_toggled); pr.addWidget(self.btn_preview); pr.addStretch()
        src_l.addLayout(pr); self.btn_preview.setVisible(False)
        self.monitor_preview=MonitorPreview(self.config); src_l.addWidget(self.monitor_preview)
        self.monitor_preview.setVisible(False); self._preview_active=False
        src_l.addWidget(QLabel("단색/무지개: 아래 색상 팔레트에서 선택\n화면 연동: 화면 색상 + 오디오 에너지로 밝기 제어"))
        layout.addWidget(src_g)

        # ═══ 5. 색상 팔레트 (단색 전용) ═══
        self.color_group=QGroupBox("색상"); cl=QVBoxLayout(self.color_group); pg=QGridLayout()
        for i,(name,r,g,b) in enumerate(COLOR_PRESETS):
            btn=QPushButton(name); btn.setMinimumHeight(26)
            if r is None:
                btn.setStyleSheet("background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);color:white;font-weight:bold;border-radius:4px;font-size:11px;")
                btn.clicked.connect(lambda _: self._set_rainbow())
            else:
                tc="#000" if (r+g+b)>380 else "#fff"
                btn.setStyleSheet(f"background:rgb({r},{g},{b});color:{tc};font-weight:bold;border-radius:4px;font-size:11px;")
                btn.clicked.connect(lambda _,rgb=(r,g,b): self._set_color(*rgb))
            pg.addWidget(btn,i//5,i%5)
        cl.addLayout(pg)
        cr=QHBoxLayout(); self.btn_custom=QPushButton("🎨 커스텀"); self.btn_custom.clicked.connect(self._pick_custom_color)
        cr.addWidget(self.btn_custom)
        self.color_preview=QFrame(); self.color_preview.setFixedSize(40,26); self._is_rainbow=True
        self.color_preview.setStyleSheet("background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);border:1px solid #555;border-radius:4px;")
        cr.addWidget(self.color_preview); cr.addStretch(); cl.addLayout(cr)
        layout.addWidget(self.color_group)

        # ═══ 6. 비주얼라이저 모드 (★ 색상 소스 아래) ═══
        mg=QGroupBox("비주얼라이저 모드"); ml=QVBoxLayout(mg)
        self.combo_mode=QComboBox()
        self.combo_mode.addItems(["🔴 Bass 반응 — 저음 기반 전체 밝기","🌈 Spectrum — 16밴드 주파수 매핑","🔊 Bass Detail — 저역 세밀 16밴드"])
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed); ml.addWidget(self.combo_mode)
        ml.addWidget(QLabel("Bass 반응: bass 에너지로 전체 밝기 제어 + mid/high 색상 변조\nSpectrum: 16개 주파수 밴드별 밝기 (20Hz~16kHz)\nBass Detail: 20~500Hz 저역만 16밴드 세밀 분할\n※ 모드 전환 시 파라미터가 각 모드의 저장값으로 전환됩니다"))
        layout.addWidget(mg)

        # ═══ 7. 파라미터 ═══
        pg2=QGroupBox("파라미터"); pl=QVBoxLayout(pg2)
        er=QHBoxLayout()
        self.btn_edit=QPushButton("🔒 파라미터 잠금됨 — 클릭하여 편집"); self.btn_edit.setCheckable(True); self.btn_edit.setChecked(False)
        self.btn_edit.setStyleSheet("QPushButton{background:#34495e;color:#bdc3c7;border-radius:4px;padding:6px;font-weight:bold;}QPushButton:checked{background:#2980b9;color:white;}")
        self.btn_edit.toggled.connect(self._toggle_edit_mode); er.addWidget(self.btn_edit)
        self.btn_reset=QPushButton("↩ 기본값"); self.btn_reset.setFixedWidth(80); self.btn_reset.clicked.connect(self._reset_defaults); self.btn_reset.setEnabled(False); er.addWidget(self.btn_reset)
        self.btn_save_params=QPushButton("💾 저장"); self.btn_save_params.setFixedWidth(70); self.btn_save_params.clicked.connect(self._save_params); self.btn_save_params.setEnabled(False); er.addWidget(self.btn_save_params)
        pl.addLayout(er)

        self._spectrum_only_widgets=[]
        self.label_sens=QLabel("감도 (대역별)"); pl.addWidget(self.label_sens)
        self.slider_bass_sens,self.label_bass_sens=self._add_param_slider(pl,"Bass:",10,300,100,self._on_sens_changed)

        self.row_mid_sens=QWidget(); rm=QHBoxLayout(self.row_mid_sens); rm.setContentsMargins(0,0,0,0)
        rm.addWidget(QLabel("Mid:")); self.slider_mid_sens=NoScrollSlider(Qt.Horizontal)
        self.slider_mid_sens.setRange(10,300); self.slider_mid_sens.setValue(100)
        self.slider_mid_sens.valueChanged.connect(self._on_sens_changed); rm.addWidget(self.slider_mid_sens)
        self.label_mid_sens=QLabel("1.00"); self.label_mid_sens.setMinimumWidth(40); self.label_mid_sens.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        rm.addWidget(self.label_mid_sens); pl.addWidget(self.row_mid_sens)
        self._all_sliders.append(self.slider_mid_sens); self._spectrum_only_widgets.append(self.row_mid_sens)

        self.row_high_sens=QWidget(); rh=QHBoxLayout(self.row_high_sens); rh.setContentsMargins(0,0,0,0)
        rh.addWidget(QLabel("High:")); self.slider_high_sens=NoScrollSlider(Qt.Horizontal)
        self.slider_high_sens.setRange(10,300); self.slider_high_sens.setValue(100)
        self.slider_high_sens.valueChanged.connect(self._on_sens_changed); rh.addWidget(self.slider_high_sens)
        self.label_high_sens=QLabel("1.00"); self.label_high_sens.setMinimumWidth(40); self.label_high_sens.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        rh.addWidget(self.label_high_sens); pl.addWidget(self.row_high_sens)
        self._all_sliders.append(self.slider_high_sens); self._spectrum_only_widgets.append(self.row_high_sens)

        ln=QFrame(); ln.setFrameShape(QFrame.HLine); ln.setFrameShadow(QFrame.Sunken); pl.addWidget(ln)
        self.slider_brightness,self.label_brightness=self._add_param_slider(pl,"밝기:",0,100,100,self._on_brightness_changed,suffix="%")
        pl.addWidget(QLabel("반응 특성"))
        self.slider_attack,self.label_attack=self._add_param_slider(pl,"Attack:",0,100,50,self._on_ar_changed)
        self.slider_release,self.label_release=self._add_param_slider(pl,"Release:",0,100,50,self._on_ar_changed)
        ht=QLabel("Attack ↑ = 빠르게 반응 (펀치감)  |  Release ↑ = 긴 잔향 (여운)"); ht.setStyleSheet("color:#888;font-size:10px;"); ht.setWordWrap(True); pl.addWidget(ht)

        self.zone_line=QFrame(); self.zone_line.setFrameShape(QFrame.HLine); self.zone_line.setFrameShadow(QFrame.Sunken); pl.addWidget(self.zone_line); self._spectrum_only_widgets.append(self.zone_line)
        self.zone_label=QLabel("대역 비율 (Spectrum 색상·주파수 분배)"); self.zone_label.setStyleSheet("font-weight:bold;"); pl.addWidget(self.zone_label); self._spectrum_only_widgets.append(self.zone_label)
        self.zone_desc=QLabel("각 대역이 LED 둘레에서 차지하는 비율. Bass ↑ → 하단에서 빨강~노랑 영역 확대"); self.zone_desc.setStyleSheet("color:#888;font-size:10px;"); self.zone_desc.setWordWrap(True); pl.addWidget(self.zone_desc); self._spectrum_only_widgets.append(self.zone_desc)
        self.zone_balance=ZoneBalanceWidget(33,33,34); self.zone_balance.zone_changed.connect(self._on_zone_changed)
        self._all_sliders.extend(self.zone_balance._sliders.values()); pl.addWidget(self.zone_balance); self._spectrum_only_widgets.append(self.zone_balance)
        layout.addWidget(pg2)

        # ═══ 8. 오디오 디바이스 ═══
        ag=QGroupBox("오디오 디바이스"); al=QVBoxLayout(ag); dr=QHBoxLayout()
        self.combo_device=QComboBox(); self._refresh_devices(); dr.addWidget(self.combo_device)
        br=QPushButton("🔄"); br.setFixedWidth(36); br.clicked.connect(self._refresh_devices); dr.addWidget(br)
        al.addLayout(dr); al.addWidget(QLabel("WASAPI Loopback 디바이스를 사용합니다.")); layout.addWidget(ag)
        layout.addStretch()

        self._set_sliders_enabled(False); self._update_mode_ui("pulse")

    # ── 헬퍼 ─────────────────────────────────────────────────────

    def _add_param_slider(self, parent_layout, label_text, min_v, max_v, default, callback, suffix=""):
        row=QHBoxLayout(); row.addWidget(QLabel(label_text))
        s=NoScrollSlider(Qt.Horizontal); s.setRange(min_v,max_v); s.setValue(default); s.valueChanged.connect(callback); row.addWidget(s)
        lbl=QLabel(f"{default}{suffix}" if suffix=="%" else f"{default/100:.2f}"); lbl.setMinimumWidth(40); lbl.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        row.addWidget(lbl); parent_layout.addLayout(row); self._all_sliders.append(s); return s,lbl

    @staticmethod
    def _make_bar(grid, row, name, color):
        grid.addWidget(QLabel(name),row,0); bar=QProgressBar(); bar.setRange(0,100); bar.setTextVisible(False); bar.setFixedHeight(14)
        bar.setStyleSheet(f"QProgressBar{{background:#2b2b2b;border-radius:3px}}QProgressBar::chunk{{background:{color};border-radius:3px}}")
        grid.addWidget(bar,row,1); return bar

    # ── 편집 모드 ────────────────────────────────────────────────

    def _toggle_edit_mode(self, checked):
        self._edit_mode=checked; self._set_sliders_enabled(checked); self.btn_reset.setEnabled(checked); self.btn_save_params.setEnabled(checked)
        self.btn_edit.setText("🔓 편집 중 — 클릭하여 잠금" if checked else "🔒 파라미터 잠금됨 — 클릭하여 편집")

    def _set_sliders_enabled(self, enabled):
        for s in self._all_sliders: s.setEnabled(enabled)
        self.zone_balance.setEnabled(enabled)

    def _reset_defaults(self):
        df=MODE_DEFAULTS.get(self._current_mode_key, MODE_DEFAULTS["pulse"]); self._switching_mode=True
        self.slider_bass_sens.setValue(df["bass_sens"]); self.slider_mid_sens.setValue(df["mid_sens"])
        self.slider_high_sens.setValue(df["high_sens"]); self.slider_brightness.setValue(df["brightness"])
        self.slider_attack.setValue(df["attack"]); self.slider_release.setValue(df["release"])
        self.zone_balance.set_values(df["zone_bass"],df["zone_mid"],df["zone_high"])
        self._switching_mode=False; self._apply_params_to_visualizer()

    def _save_params(self):
        self._save_current_params_to_mode(self._current_mode_key); save_config(self.config)
        ml={"pulse":"Bass 반응","spectrum":"Spectrum","bass_detail":"Bass Detail"}
        QMessageBox.information(self,"저장",f"{ml.get(self._current_mode_key,'')} 모드 설정이 저장되었습니다.")

    def _refresh_devices(self):
        self.combo_device.clear(); self.combo_device.addItem("자동 (기본 출력 디바이스)",None)
        for idx,name,sr,ch in list_loopback_devices(): self.combo_device.addItem(f"{name} ({sr}Hz, {ch}ch)",idx)

    # ── 색상 소스 ────────────────────────────────────────────────

    def _get_color_source(self): return [COLOR_SOURCE_SOLID,COLOR_SOURCE_SCREEN][self.combo_color_source.currentIndex()]
    def _get_zone_count(self): return self.combo_zone_count.currentData() or 4

    def _on_color_source_changed(self, idx):
        source=self._get_color_source(); is_screen=(source==COLOR_SOURCE_SCREEN)
        self.color_group.setVisible(not is_screen)
        self.zone_count_row.setVisible(is_screen)
        self.min_bright_row.setVisible(is_screen)
        self.btn_preview.setVisible(is_screen)
        if not is_screen:
            self.monitor_preview.setVisible(False); self._preview_active=False; self.btn_preview.setChecked(False)
        if self._visualizer:
            n=self._get_zone_count() if is_screen else 4; self._visualizer.set_color_source(source,n_zones=n)
            self._visualizer.min_brightness=self.slider_min_brightness.value()/100.0 if is_screen else 0.02

    def _on_zone_count_changed(self, idx):
        n=self._get_zone_count()
        if n!=N_ZONES_PER_LED: self.monitor_preview.set_zone_map(_build_led_zone_map_by_side(self.config,n))
        self.monitor_preview.set_n_zones(n)
        if self._visualizer and self._get_color_source()==COLOR_SOURCE_SCREEN:
            self._visualizer.set_color_source(COLOR_SOURCE_SCREEN,n_zones=n)

    def _on_min_brightness_changed(self, value):
        self.label_min_brightness.setText(f"{value}%")
        if self._visualizer and self._get_color_source()==COLOR_SOURCE_SCREEN:
            self._visualizer.min_brightness=value/100.0

    def _on_screen_colors_updated(self, colors):
        if self._preview_active: self.monitor_preview.set_colors(colors)

    def _on_preview_toggled(self, checked):
        self._preview_active=checked; self.monitor_preview.setVisible(checked)
        if checked:
            self.btn_preview.setText("👁 프리뷰 숨기기"); n=self._get_zone_count()
            if n!=N_ZONES_PER_LED: self.monitor_preview.set_zone_map(_build_led_zone_map_by_side(self.config,n))
            self.monitor_preview.set_n_zones(n)
        else: self.btn_preview.setText("👁 프리뷰 보기")

    # ── 시작/중지 ────────────────────────────────────────────────

    def _start_visualizer(self):
        if self._is_running: return
        self.request_mirror_stop.emit()
        try: self._visualizer=HybridVisualizer(self.config,device_index=self.combo_device.currentData())
        except Exception as e: QMessageBox.warning(self,"초기화 실패",str(e)); return
        source=self._get_color_source(); n=self._get_zone_count()
        self._visualizer.color_source=source; self._visualizer.n_zones=n
        if source==COLOR_SOURCE_SCREEN: self._visualizer.min_brightness=self.slider_min_brightness.value()/100.0
        if self._is_rainbow: self._visualizer.set_rainbow(True)
        else: r,g,b=self._current_color; self._visualizer.set_color(r,g,b)
        self._visualizer.mode=self._get_current_mode(); self._apply_params_to_visualizer()
        self._visualizer.fps_updated.connect(self._on_fps)
        self._visualizer.energy_updated.connect(self._on_energy)
        self._visualizer.spectrum_updated.connect(self._on_spectrum)
        self._visualizer.status_changed.connect(self._on_status)
        self._visualizer.error.connect(self._on_error)
        self._visualizer.finished.connect(self._on_finished)
        self._visualizer.screen_colors_updated.connect(self._on_screen_colors_updated)
        self._visualizer.start(); self._set_running(True)

    def _stop_visualizer(self):
        if self._visualizer and self._visualizer.isRunning():
            self._visualizer.stop_visualizer(); self.status_label.setText("중지 중...")

    def stop_visualizer_sync(self):
        if self._visualizer and self._visualizer.isRunning():
            self._visualizer.stop_visualizer(); self._visualizer.wait(2000)

    def _set_running(self, running):
        self._is_running=running; self.btn_start.setEnabled(not running); self.btn_stop.setEnabled(running)
        self.combo_device.setEnabled(not running)
        if not running: self._decay_timer.start()
        else: self._decay_timer.stop()

    # ── 시그널 핸들러 ────────────────────────────────────────────

    def _on_fps(self, fps): self.fps_label.setText(f"{fps:.1f} fps")
    def _on_energy(self, bass, mid, high):
        self.bar_bass.setValue(int(bass*100)); self.bar_mid.setValue(int(mid*100)); self.bar_high.setValue(int(high*100))
    def _on_spectrum(self, spec): self.spectrum_widget.set_values(spec)
    def _on_status(self, text): self.status_label.setText(text)
    def _on_error(self, msg, severity):
        if severity=="critical": QMessageBox.warning(self,"오류",msg)
        else: self.status_label.setText(f"⚠ {msg}")
    def _on_finished(self): self._set_running(False); self.fps_label.setText("— fps"); self._visualizer=None

    def _decay_levels(self):
        b,m,h=self.bar_bass.value(),self.bar_mid.value(),self.bar_high.value()
        if b<=0 and m<=0 and h<=0: self._decay_timer.stop(); self.spectrum_widget.set_values(np.zeros(16)); return
        self.bar_bass.setValue(max(0,b-3)); self.bar_mid.setValue(max(0,m-3)); self.bar_high.setValue(max(0,h-3))
        self.spectrum_widget.set_values(self.spectrum_widget._values*0.9)

    def _update_resource_usage(self):
        try:
            cpu=self._process.cpu_percent()/psutil.cpu_count(); ram=self._process.memory_info().rss/(1024*1024)
            self.cpu_label.setText(f"CPU: {cpu:.1f}%"); self.ram_label.setText(f"RAM: {ram:.0f} MB")
            c="#c0392b" if cpu>=20 else "#e67e22" if cpu>=10 else "#d35400"
            self.cpu_label.setStyleSheet(f"font-size:12px;color:{c};margin-right:6px;")
        except: pass

    # ── 모드 ─────────────────────────────────────────────────────

    def _update_mode_ui(self, mode_name):
        is_banded=(mode_name in ("spectrum","bass_detail"))
        for w in self._spectrum_only_widgets: w.setVisible(is_banded)
        if mode_name=="bass_detail": self.label_sens.setText("감도 (Bass Detail)")
        elif mode_name=="spectrum": self.label_sens.setText("감도 (대역별)")
        else: self.label_sens.setText("감도 (Bass)")

    def _on_mode_changed(self, idx):
        nm=["pulse","spectrum","bass_detail"][idx]
        if nm==self._current_mode_key: return
        self._save_current_params_to_mode(self._current_mode_key); self._load_mode_params(nm); self._update_mode_ui(nm)
        if self._visualizer: self._visualizer.set_mode(self._get_current_mode()); self._apply_params_to_visualizer()

    def _get_current_mode(self): return [MODE_PULSE,MODE_SPECTRUM,MODE_BASS_DETAIL][self.combo_mode.currentIndex()]

    # ── 색상 ─────────────────────────────────────────────────────

    def _set_color(self, r, g, b):
        self._current_color=(r,g,b); self._is_rainbow=False
        self.color_preview.setStyleSheet(f"background:rgb({r},{g},{b});border:1px solid #555;border-radius:4px;")
        if self._visualizer: self._visualizer.set_color(r,g,b)

    def _set_rainbow(self):
        self._is_rainbow=True
        self.color_preview.setStyleSheet("background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 red,stop:0.17 orange,stop:0.33 yellow,stop:0.5 lime,stop:0.67 cyan,stop:0.83 blue,stop:1 purple);border:1px solid #555;border-radius:4px;")
        if self._visualizer: self._visualizer.set_rainbow(True)

    def _pick_custom_color(self):
        r,g,b=self._current_color; c=QColorDialog.getColor(QColor(r,g,b),self,"기본 색상")
        if c.isValid(): self._set_color(c.red(),c.green(),c.blue())

    # ── 파라미터 콜백 ────────────────────────────────────────────

    def _on_sens_changed(self, _=None):
        bv=self.slider_bass_sens.value()/100.0; mv=self.slider_mid_sens.value()/100.0; hv=self.slider_high_sens.value()/100.0
        self.label_bass_sens.setText(f"{bv:.2f}"); self.label_mid_sens.setText(f"{mv:.2f}"); self.label_high_sens.setText(f"{hv:.2f}")
        if not self._switching_mode and self._visualizer:
            self._visualizer.bass_sensitivity=bv; self._visualizer.mid_sensitivity=mv; self._visualizer.high_sensitivity=hv

    def _on_brightness_changed(self, value):
        self.label_brightness.setText(f"{value}%")
        if not self._switching_mode and self._visualizer: self._visualizer.brightness=value/100.0

    def _on_ar_changed(self, _=None):
        atk=self.slider_attack.value()/100.0; rel=self.slider_release.value()/100.0
        self.label_attack.setText(f"{atk:.2f}"); self.label_release.setText(f"{rel:.2f}")
        if not self._switching_mode and self._visualizer: self._visualizer.attack=atk; self._visualizer.release=rel

    def _on_zone_changed(self, bass, mid, high):
        self.spectrum_widget.set_zone_weights(bass,mid,high)
        if not self._switching_mode and self._visualizer: self._visualizer.set_zone_weights(bass,mid,high)

    def cleanup(self):
        self._save_current_params_to_mode(self._current_mode_key); self.stop_visualizer_sync()
