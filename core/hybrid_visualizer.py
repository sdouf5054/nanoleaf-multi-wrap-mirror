"""하이브리드 비주얼라이저 — 오디오 반응 + 화면 색상 소스

[목적]
기존 AudioVisualizer의 상위 호환.
color_source == "solid"이면 AudioVisualizer와 100% 동일하게 동작하고,
"screen"이면 화면 색상을 LED base color로 사용합니다.

[색상 소스 모드]
- solid  : 단색 또는 무지개 (기존 AudioVisualizer)
- screen : 화면 연동 — n_zones에 따라:
           1구역: 화면 전체 평균색 → 모든 LED 동일색
           2구역: 상/하 절반 평균색
           4+구역: LED 위치별 화면 가장자리 색상

[멀티랩 처리]
LED가 모니터 둘레를 여러 바퀴 감는 구조(config.json segments)에서,
같은 물리적 위치의 LED는 같은 화면 구역 색상을 받아야 합니다.

이는 _compute_led_perimeter_t()가 이미 처리합니다:
- 각 LED의 둘레 위치를 0~1 비율로 계산
- 바깥/안쪽 바퀴의 같은 위치 LED → 같은 perimeter_t → 같은 zone
- zone 매핑은 perimeter_t를 n_zones로 양자화하면 완료

[렌더링 흐름]
1. AudioEngine → 3밴드 에너지 + 16밴드 스펙트럼 (콜백, ~48kHz/2048)
2. ScreenSampler.update() → N구역 색상 (매 N프레임, ~20fps)
3. 합성: base_color = f(screen_zone or solid or global)
         brightness = f(audio_energy, mode)
4. USB 전송 (병목 ~30ms worst case)

[타이밍 예산 @ 60fps]
오디오 FFT 읽기        : ~0.1ms (이미 콜백에서 처리됨)
스크린 grab + 구역 평균 : ~0.3ms (매 3프레임 = 실효 ~0.1ms)
LED 색상 합성          : ~0.3ms (75 LEDs, numpy)
USB write + response   : ~5-15ms (보통), 30ms (worst)
합계: ~7-18ms → 60fps 여유 있음
"""

import time
import copy
import threading
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from core.audio_engine import AudioEngine
from core.device import NanoleafDevice
from core.layout import get_led_positions, build_weight_matrix
from core.audio_visualizer import (
    MODE_PULSE, MODE_SPECTRUM, DEFAULT_FPS, MIN_BRIGHTNESS,
    _compute_led_perimeter_t, _compute_led_band_mapping,
    _build_led_order_from_segments,
)
from core.screen_sampler import ScreenSampler

# ── 색상 소스 상수 ─────────────────────────────────────────────────
COLOR_SOURCE_SOLID = "solid"
COLOR_SOURCE_SCREEN = "screen"

# ── 특수 구역 수: LED 개별 미러링 ──────────────────────────────────
# n_zones가 이 값이면 기존 미러링의 weight matrix 파이프라인 사용
N_ZONES_PER_LED = -1  # UI에서는 led_count(75)로 표시, 내부적으로 이 값

# ── 스크린 갱신 간격 (오디오 프레임 수 기준) ───────────────────────
# 3프레임마다 = 60fps 기준 20fps 스크린 갱신
# 화면 변화는 오디오보다 느리므로 1-2프레임 stale은 시각적으로 무관
SCREEN_UPDATE_INTERVAL = 3

# ── zone 매핑 상수 ─────────────────────────────────────────────────
# perimeter_t → zone 매핑에서 사용하는 구역 순서:
#   4구역:  top(0), right(1), bottom(2), left(3)
#   이는 ScreenSampler._regions_4()의 반환 순서와 일치
#   perimeter_t=0 (하단 중앙)부터 시계방향이므로 재매핑 필요
#
# perimeter_t 범위와 화면 변 대응:
#   0.00 ~ 0.25 : bottom (하단 중앙 → 하단 우측)
#   0.25 ~ 0.50 : right  (우하 → 우상)
#   0.50 ~ 0.75 : top    (상단 우측 → 상단 좌측... 대칭이라 상단 중앙)
#   0.75 ~ 1.00 : left   (좌상 → 좌하)
#
# 하지만 perimeter_t는 대칭 거리(하단 중앙=0, 상단 중앙=1)이므로
# 실제로는 양쪽이 같은 값을 가짐. → side 정보로 보완 필요.
#
# ★ 더 간단한 접근: LED의 side 정보를 직접 사용하여 zone 매핑


def _build_led_zone_map_by_side(config, n_zones):
    """각 LED가 어느 screen zone에 매핑되는지 계산.

    멀티랩 자동 처리: get_led_positions()가 모든 세그먼트의
    LED 위치와 side를 반환하므로, 같은 side의 LED는
    바깥/안쪽 바퀴 무관하게 같은 zone 그룹에 속합니다.

    4구역 매핑 (ScreenSampler._regions_4 순서와 일치):
        top=0, right=1, bottom=2, left=3

    8구역 매핑: side + 위치 세분화
        top 좌반=0, top 우반=1, right 상반=2, right 하반=3,
        bottom 우반=4, bottom 좌반=5, left 하반=6, left 상반=7
        → ScreenSampler._regions_8 순서와 일치:
          top, top-right, right, bottom-right,
          bottom, bottom-left, left, top-left

    16/32구역: perimeter_t 기반 균등 분할

    Args:
        config: 앱 설정 dict
        n_zones: 구역 수 (1, 2, 4, 8, 16, 32)

    Returns:
        led_zone_map: np.array (led_count,) int — 각 LED의 zone 인덱스
    """
    layout_cfg = config["layout"]
    mirror_cfg = config.get("mirror", {})
    dev_cfg = config["device"]
    led_count = dev_cfg["led_count"]

    screen_w = mirror_cfg.get("grid_cols", 64) * 40
    screen_h = mirror_cfg.get("grid_rows", 32) * 40

    positions, sides = get_led_positions(
        screen_w, screen_h,
        layout_cfg["segments"], led_count,
        orientation=mirror_cfg.get("orientation", "auto"),
        portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
    )

    mapping = np.zeros(led_count, dtype=np.int32)

    if n_zones == 1:
        # 모든 LED → zone 0 (화면 전체 평균)
        pass  # 이미 0으로 초기화됨

    elif n_zones == 2:
        # 수평 중앙선 기준 이분할
        # ScreenSampler._regions_2: [top half, bottom half]
        #   zone 0 (상반): top 전체 + left 상반 + right 상반
        #   zone 1 (하반): bottom 전체 + left 하반 + right 하반
        cy = screen_h / 2.0
        for i in range(led_count):
            side = sides[i]
            if side == "top":
                mapping[i] = 0
            elif side == "bottom":
                mapping[i] = 1
            elif side in ("left", "right"):
                y = positions[i, 1]
                mapping[i] = 0 if y <= cy else 1
            else:
                mapping[i] = 0

    elif n_zones == 4:
        # 단순 side 매핑
        side_to_zone = {"top": 0, "right": 1, "bottom": 2, "left": 3}
        for i in range(led_count):
            mapping[i] = side_to_zone.get(sides[i], 0)

    elif n_zones == 8:
        # side + 위치 이분할
        # 각 변에서 LED 위치가 변의 중간점 기준 전반/후반
        cx, cy = screen_w / 2.0, screen_h / 2.0
        for i in range(led_count):
            x, y = positions[i]
            side = sides[i]
            if side == "top":
                mapping[i] = 0 if x <= cx else 1      # top-left half → 0, top-right half → 1
            elif side == "right":
                mapping[i] = 2 if y <= cy else 3      # right-top half → 2, right-bottom half → 3
            elif side == "bottom":
                mapping[i] = 4 if x >= cx else 5      # bottom-right half → 4, bottom-left half → 5
            elif side == "left":
                mapping[i] = 6 if y >= cy else 7      # left-bottom half → 6, left-top half → 7
            else:
                mapping[i] = 0

    else:
        # 16/32: perimeter_t 기반 균등 분할
        # perimeter_t는 대칭 거리이므로 side 정보와 결합하여
        # 시계방향 연속 인덱스로 변환
        perimeter_t = _compute_led_perimeter_t(config)

        for i in range(led_count):
            side = sides[i]
            t = perimeter_t[i]  # 0~1, 하단중앙=0, 상단중앙=1

            # perimeter_t를 시계방향 0~1로 변환
            # 원래: 하단중앙=0 → 양방향으로 증가 → 상단중앙=1
            # 목표: top-left=0 → 시계방향 → top-left=1
            #
            # 변환 전략: side별로 0~1 구간 배정
            #   top:    0.00 ~ 0.25  (좌→우)
            #   right:  0.25 ~ 0.50  (상→하)
            #   bottom: 0.50 ~ 0.75  (우→좌)
            #   left:   0.75 ~ 1.00  (하→상)
            #
            # side 내에서의 진행도는 positions에서 계산
            x, y = positions[i]

            if side == "top":
                progress = x / screen_w if screen_w > 0 else 0.5
                cw_t = 0.00 + progress * 0.25
            elif side == "right":
                progress = y / screen_h if screen_h > 0 else 0.5
                cw_t = 0.25 + progress * 0.25
            elif side == "bottom":
                progress = 1.0 - (x / screen_w if screen_w > 0 else 0.5)
                cw_t = 0.50 + progress * 0.25
            elif side == "left":
                progress = 1.0 - (y / screen_h if screen_h > 0 else 0.5)
                cw_t = 0.75 + progress * 0.25
            else:
                cw_t = 0.0

            cw_t = max(0.0, min(cw_t, 0.9999))
            mapping[i] = int(cw_t * n_zones)

    return mapping


class HybridVisualizer(QThread):
    """오디오 반응 + 화면 색상 소스 LED 비주얼라이저.

    color_source == "solid"이면 기존 AudioVisualizer와 동일.
    "screen_zones"/"screen_global"이면 화면 색상을 base color로 사용.

    Signals:
        fps_updated(float): 현재 FPS
        energy_updated(float, float, float): bass, mid, high 에너지
        spectrum_updated(object): 16밴드 스펙트럼 np.array
        error(str, str): (메시지, 심각도)
        status_changed(str): 상태 텍스트
        screen_colors_updated(object): 구역별 색상 (UI 프리뷰용)
    """

    fps_updated = pyqtSignal(float)
    energy_updated = pyqtSignal(float, float, float)
    spectrum_updated = pyqtSignal(object)
    error = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)
    screen_colors_updated = pyqtSignal(object)  # ★ 화면 색상 프리뷰용

    def __init__(self, config, device_index=None):
        super().__init__()
        self.config = copy.deepcopy(config)
        self._stop_event = threading.Event()

        # ── 오디오 파라미터 (AudioVisualizer 호환) ──
        self.base_color = np.array([255, 0, 80], dtype=np.float32)
        self.rainbow = False
        self.brightness = 1.0
        self.bass_sensitivity = 1.0
        self.mid_sensitivity = 1.0
        self.high_sensitivity = 1.0
        self.mode = MODE_PULSE
        self.target_fps = DEFAULT_FPS
        self.attack = 0.5
        self.release = 0.1

        # 대역 비율 (AudioVisualizer 호환)
        self._zone_weights = [33, 33, 34]
        self._zone_dirty = False

        # ── 색상 소스 파라미터 ──
        self.color_source = COLOR_SOURCE_SOLID
        self.n_zones = 4

        # ── 내부 상태 ──
        self._device_index = device_index
        self._audio_engine = None
        self._screen_sampler = None
        self._nanoleaf = None
        self._led_count = 0

        self._perimeter_t = None
        self._led_band_indices = None
        self._led_zone_map = None
        self._led_order = []

        # ★ per-LED 미러링용 (n_zones == N_ZONES_PER_LED)
        self._weight_matrix = None
        self._per_led_colors = None  # (led_count, 3) float32 캐시

        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None

    # ── 외부 제어 (메인 스레드에서 호출) ───────────────────────────

    def set_zone_weights(self, bass, mid, high):
        """오디오 대역 비율 변경."""
        self._zone_weights = [bass, mid, high]
        self._zone_dirty = True

    def set_color(self, r, g, b):
        self.base_color = np.array([r, g, b], dtype=np.float32)
        self.rainbow = False

    def set_rainbow(self, enabled=True):
        self.rainbow = enabled

    def set_mode(self, mode):
        self.mode = mode

    def set_color_source(self, source, n_zones=None):
        """색상 소스 변경 — 실행 중에도 안전.

        Args:
            source: COLOR_SOURCE_SOLID / COLOR_SOURCE_SCREEN
            n_zones: 구역 수 (screen_zones 전용, None이면 현재값 유지)
        """
        self.color_source = source

        if n_zones is not None and n_zones != self.n_zones:
            self.n_zones = n_zones

            if n_zones == N_ZONES_PER_LED:
                # per-LED: weight matrix 빌드 (아직 없으면)
                if self._weight_matrix is None:
                    mirror_cfg = self.config.get("mirror", {})
                    self._build_weight_matrix(mirror_cfg)
            else:
                # zone 매핑 재계산
                if self._perimeter_t is not None:
                    self._led_zone_map = _build_led_zone_map_by_side(
                        self.config, n_zones
                    )
                # ScreenSampler 구역 수 변경
                if self._screen_sampler is not None:
                    self._screen_sampler.set_n_zones(n_zones)

    def stop_visualizer(self):
        self._stop_event.set()

    # ── 밴드 매핑 재계산 ───────────────────────────────────────────

    def _rebuild_band_mapping(self):
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        self._zone_dirty = False

    # ── QThread 진입점 ─────────────────────────────────────────────

    def run(self):
        if not self._init_resources():
            return
        try:
            self._run_loop()
        except Exception as e:
            self.error.emit(f"비주얼라이저 오류: {e}", "warning")
        finally:
            self._cleanup()

    # ── 리소스 초기화 ──────────────────────────────────────────────

    def _init_resources(self):
        dev_cfg = self.config["device"]
        mirror_cfg = self.config.get("mirror", {})
        self._led_count = dev_cfg["led_count"]

        # 1. Nanoleaf 연결
        self.status_changed.emit("Nanoleaf 연결 중...")
        try:
            self._nanoleaf = NanoleafDevice(
                int(dev_cfg["vendor_id"], 16),
                int(dev_cfg["product_id"], 16),
                self._led_count,
            )
            self._nanoleaf.connect()
        except (OSError, IOError, ValueError, ConnectionError) as e:
            self.error.emit(f"Nanoleaf 연결 실패: {e}", "critical")
            return False

        # 2. 오디오 엔진
        self.status_changed.emit("오디오 캡처 초기화...")
        try:
            self._audio_engine = AudioEngine(
                device_index=self._device_index,
                sensitivity=1.0,
                smoothing=0.15,
            )
            self._audio_engine.bass_sensitivity = self.bass_sensitivity
            self._audio_engine.mid_sensitivity = self.mid_sensitivity
            self._audio_engine.high_sensitivity = self.high_sensitivity
            self._audio_engine.start()
        except Exception as e:
            self.error.emit(f"오디오 캡처 실패: {e}", "critical")
            if self._nanoleaf:
                self._nanoleaf.disconnect()
            return False

        # 3. LED 둘레 매핑
        n_bands = self._audio_engine.n_bands
        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(segments, self._led_count)

        # 4. zone 매핑 (멀티랩 자동 처리)
        if self.n_zones != N_ZONES_PER_LED:
            self._led_zone_map = _build_led_zone_map_by_side(
                self.config, self.n_zones
            )

        # 4b. ★ per-LED 미러링용 weight matrix
        if self.n_zones == N_ZONES_PER_LED:
            self._build_weight_matrix(mirror_cfg)

        # 5. 스크린 샘플러 (screen 소스일 때만 초기화)
        if self.color_source != COLOR_SOURCE_SOLID:
            self._init_screen_sampler(mirror_cfg)

        self._smooth_spectrum = np.zeros(n_bands, dtype=np.float64)

        self.status_changed.emit("하이브리드 비주얼라이저 실행 중")
        return True

    def _build_weight_matrix(self, mirror_cfg):
        """★ per-LED 미러링용 가중치 행렬 생성.

        기존 mirror.py의 _build_layout과 동일한 로직.
        64×32 그리드 기준으로 각 LED가 어떤 셀의 색을 받을지 계산.
        """
        layout_cfg = self.config["layout"]
        grid_cols = mirror_cfg.get("grid_cols", 64)
        grid_rows = mirror_cfg.get("grid_rows", 32)
        screen_w = grid_cols * 40
        screen_h = grid_rows * 40

        positions, sides = get_led_positions(
            screen_w, screen_h,
            layout_cfg["segments"], self._led_count,
            orientation=mirror_cfg.get("orientation", "auto"),
            portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
        )

        decay = mirror_cfg.get("decay_radius", 0.3)
        penalty = mirror_cfg.get("parallel_penalty", 5.0)

        # per-side 값이 있으면 사용
        per_decay = mirror_cfg.get("decay_radius_per_side", {})
        decay_param = (
            {s: per_decay.get(s, decay) for s in ("top", "bottom", "left", "right")}
            if per_decay else decay
        )
        per_penalty = mirror_cfg.get("parallel_penalty_per_side", {})
        penalty_param = (
            {s: per_penalty.get(s, penalty) for s in ("top", "bottom", "left", "right")}
            if per_penalty else penalty
        )

        self._weight_matrix = build_weight_matrix(
            screen_w, screen_h, positions, sides,
            grid_cols, grid_rows, decay_param, penalty_param,
        )
        self._per_led_colors = np.zeros((self._led_count, 3), dtype=np.float32)

    def _init_screen_sampler(self, mirror_cfg):
        """스크린 샘플러 초기화 — 실패해도 solid 폴백으로 계속 실행."""
        self.status_changed.emit("화면 캡처 초기화...")
        try:
            self._screen_sampler = ScreenSampler(
                n_zones=self.n_zones,
                grid_cols=mirror_cfg.get("grid_cols", 64),
                grid_rows=mirror_cfg.get("grid_rows", 32),
            )
            self._screen_sampler.start(
                monitor_index=mirror_cfg.get("monitor_index", 0)
            )
        except Exception as e:
            self.error.emit(
                f"화면 캡처 실패 — 단색 모드로 전환: {e}", "warning"
            )
            self._screen_sampler = None
            self.color_source = COLOR_SOURCE_SOLID

    def _ensure_screen_sampler(self):
        """screen 소스로 전환 시 샘플러가 없으면 지연 초기화."""
        if self._screen_sampler is not None:
            return True
        if self.color_source == COLOR_SOURCE_SOLID:
            return True  # 불필요

        mirror_cfg = self.config.get("mirror", {})
        self._init_screen_sampler(mirror_cfg)
        return self._screen_sampler is not None

    # ── 메인 루프 ──────────────────────────────────────────────────

    def _run_loop(self):
        frame_interval = 1.0 / self.target_fps
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            # ── 대역 비율 변경 감지 ──
            if self._zone_dirty:
                self._rebuild_band_mapping()

            # ── 색상 소스 전환 시 스크린 샘플러 지연 초기화 ──
            if self.color_source != COLOR_SOURCE_SOLID:
                self._ensure_screen_sampler()

            # ── 오디오 감도 반영 ──
            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity

            # ── 오디오 에너지 읽기 ──
            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            # ── attack/release 스무딩 ──
            atk = 0.15 + self.attack * 0.70
            rel = 0.25 - self.release * 0.245

            self._smooth_bass = self._ar(self._smooth_bass, raw_bass, atk, rel)
            self._smooth_mid = self._ar(self._smooth_mid, raw_mid, atk, rel)
            self._smooth_high = self._ar(self._smooth_high, raw_high, atk, rel)
            for i in range(len(self._smooth_spectrum)):
                self._smooth_spectrum[i] = self._ar(
                    self._smooth_spectrum[i], raw_spectrum[i], atk, rel
                )

            bass = self._smooth_bass
            mid = self._smooth_mid
            high = self._smooth_high
            spec = self._smooth_spectrum

            # ── 스크린 갱신 (매 N프레임) ──
            if (self._screen_sampler is not None
                    and frame_count % SCREEN_UPDATE_INTERVAL == 0):
                self._screen_sampler.update()

                # ★ per-LED 미러링: 프레임에 weight matrix 적용
                if self.n_zones == N_ZONES_PER_LED and self._weight_matrix is not None:
                    frame = self._screen_sampler.get_last_frame()
                    if frame is not None:
                        grid_flat = frame.reshape(-1, 3).astype(np.float32)
                        self._per_led_colors = self._weight_matrix @ grid_flat

            # ── LED 렌더링 ──
            mode = self.mode
            if mode == MODE_SPECTRUM:
                grb_data = self._render_spectrum(spec)
            else:
                grb_data = self._render_pulse(bass, mid, high)

            # ── USB 전송 ──
            try:
                self._nanoleaf.send_rgb(grb_data)
            except (OSError, IOError, ValueError):
                pass

            if not self._nanoleaf.connected:
                self.status_changed.emit("USB 연결 끊김")
                if stop_wait(timeout=1.0):
                    break
                continue

            # ── UI 시그널 (매 3프레임) ──
            if frame_count % 3 == 0:
                self.energy_updated.emit(bass, mid, high)
                self.spectrum_updated.emit(spec.copy())
                if self._screen_sampler is not None:
                    if self.n_zones == N_ZONES_PER_LED:
                        # per-LED: 프리뷰는 4변 평균으로 요약
                        if self._per_led_colors is not None:
                            self.screen_colors_updated.emit(
                                self._per_led_colors.copy()
                            )
                    elif self.n_zones == 1:
                        gc = self._screen_sampler.get_global_color()
                        self.screen_colors_updated.emit(gc.reshape(1, 3))
                    else:
                        self.screen_colors_updated.emit(
                            self._screen_sampler.get_zone_colors()
                        )

            # ── FPS 계산 ──
            frame_count += 1
            now = time.monotonic()
            if now - fps_display >= 1.0:
                fps = frame_count / (now - fps_start) if (now - fps_start) > 0 else 0
                self.fps_updated.emit(fps)
                fps_display = now

            # ── 프레임 간격 대기 ──
            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    # ── 렌더링 — Pulse ─────────────────────────────────────────────

    def _render_pulse(self, bass, mid, high):
        n_leds = self._led_count
        n_bands = len(self._smooth_spectrum) if self._smooth_spectrum is not None else 16
        intensity = max(MIN_BRIGHTNESS, bass) * self.brightness

        leds = np.zeros((n_leds, 3), dtype=np.float32)
        for led_idx in range(n_leds):
            color = self._get_base_color(led_idx, n_bands)

            # mid → 색상 변조, high → 화이트 믹스
            c = color * (0.7 + mid * 0.3)
            white_mix = high * 0.3
            c = c * (1 - white_mix) + 255.0 * white_mix

            leds[led_idx] = c * intensity

        return self._leds_to_grb(leds)

    # ── 렌더링 — Spectrum ──────────────────────────────────────────

    def _render_spectrum(self, spec):
        n_leds = self._led_count
        n_bands = len(spec)
        leds = np.zeros((n_leds, 3), dtype=np.float32)

        for led_idx in range(n_leds):
            band_f = self._led_band_indices[led_idx]
            band_lo = max(0, min(int(band_f), n_bands - 1))
            band_hi = min(band_lo + 1, n_bands - 1)
            frac = band_f - int(band_f)
            energy = spec[band_lo] * (1 - frac) + spec[band_hi] * frac

            color = self._get_base_color(led_idx, n_bands)
            intensity = max(MIN_BRIGHTNESS, energy) * self.brightness
            leds[led_idx] = color * intensity

        return self._leds_to_grb(leds)

    # ── 색상 소스 분기 ─────────────────────────────────────────────

    def _get_base_color(self, led_idx, n_bands):
        """LED의 기본 색상 — 색상 소스에 따라 분기.

        solid  → 기존 단색/무지개 로직
        screen → n_zones에 따라:
                 1구역: 화면 전체 평균색 (global_color)
                 2+구역: LED 위치에 대응하는 화면 구역 색상

        멀티랩: _led_zone_map이 이미 바깥/안쪽 바퀴의 같은 위치 LED를
                같은 zone에 매핑하므로 추가 처리 불필요.
        """
        source = self.color_source

        if source == COLOR_SOURCE_SOLID:
            return self._get_solid_color(led_idx, n_bands)

        elif source == COLOR_SOURCE_SCREEN:
            if self._screen_sampler is None or not self._screen_sampler.has_data:
                return self._get_solid_color(led_idx, n_bands)

            # ★ per-LED 미러링: weight matrix 기반 개별 색상
            if self.n_zones == N_ZONES_PER_LED:
                if self._per_led_colors is not None:
                    return self._per_led_colors[led_idx].copy()
                return self._get_solid_color(led_idx, n_bands)

            # 1구역: 화면 전체 평균 — 모든 LED에 동일한 색
            if self.n_zones == 1:
                return self._screen_sampler.get_global_color().copy()

            # 2+구역: LED 위치 기반 zone 매핑
            zone_idx = self._led_zone_map[led_idx]
            zone_colors = self._screen_sampler.get_zone_colors()

            # zone 인덱스 범위 안전 체크
            if zone_idx >= len(zone_colors):
                zone_idx = zone_idx % len(zone_colors)

            return zone_colors[zone_idx].copy()

        # 알 수 없는 소스 → solid 폴백
        return self._get_solid_color(led_idx, n_bands)

    def _get_solid_color(self, led_idx, n_bands):
        """기존 AudioVisualizer의 단색/무지개 색상 로직."""
        if self.rainbow:
            t = self._led_band_indices[led_idx] / max(1, n_bands - 1)
            return self._band_color(t)
        else:
            return self.base_color.copy()

    # ── 색상 헬퍼 (AudioVisualizer에서 가져옴) ─────────────────────

    @staticmethod
    def _band_color(t):
        """밴드 위치(0=저음, 1=고음) → RGB 무지개색."""
        keypoints = [
            (0.000, 255,   0,   0),
            (0.130, 255, 127,   0),
            (0.260, 255, 255,   0),
            (0.400,   0, 255,   0),
            (0.540,   0, 180, 255),
            (0.680,   0,  50, 255),
            (0.820,  80,   0, 255),
            (1.000, 160,   0, 220),
        ]
        t = max(0.0, min(1.0, t))
        for i in range(len(keypoints) - 1):
            t0, r0, g0, b0 = keypoints[i]
            t1, r1, g1, b1 = keypoints[i + 1]
            if t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0
                r = r0 + (r1 - r0) * f
                g = g0 + (g1 - g0) * f
                b = b0 + (b1 - b0) * f
                return np.array([r, g, b], dtype=np.float32)
        return np.array([160, 0, 220], dtype=np.float32)

    @staticmethod
    def _ar(current, target, attack_rate, release_rate):
        if target > current:
            return current + (target - current) * attack_rate
        else:
            return current + (target - current) * release_rate

    @staticmethod
    def _leds_to_grb(leds):
        np.clip(leds, 0, 255, out=leds)
        u8 = leds.astype(np.uint8)
        grb = np.empty_like(u8)
        grb[:, 0] = u8[:, 1]  # G
        grb[:, 1] = u8[:, 0]  # R
        grb[:, 2] = u8[:, 2]  # B
        return grb.tobytes()

    # ── 정리 ───────────────────────────────────────────────────────

    def _cleanup(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None
        if self._screen_sampler:
            self._screen_sampler.stop()
            self._screen_sampler = None
        if self._nanoleaf:
            try:
                self._nanoleaf.turn_off()
                self._nanoleaf.disconnect()
            except (OSError, IOError, ValueError):
                pass
            self._nanoleaf = None
        self.status_changed.emit("비주얼라이저 중지됨")
