"""Flowing 모드 — 화면 색 기반 컬러 플로우 렌더링

[Phase 4] 하이브리드 전용. 화면에서 추출한 dominant colors가
LED 둘레를 시계방향으로 회전하며, 음악 에너지에 따라
밝기와 속도가 변화.

핵심 비유: 햇살에 하늘거리는 커튼, 물살 위 반짝이는 빛.

데이터 흐름:
  화면 캡처 → per_led_colors → extract_dominant_colors() → FlowPalette
  → tick() (매 프레임: phase 진행 + crossfade + 음악 반응)
  → render_flowing() → (n_leds, 3) RGB
  → ColorCorrection → GRB → USB

사용처:
  engine_hybrid_mode.py의 _run_loop()에서 AUDIO_FLOWING 분기
"""

import numpy as np
from core.color_extract import extract_dominant_colors

# ══════════════════════════════════════════════════════════════════
#  상수
# ══════════════════════════════════════════════════════════════════

FLOW_N_COLORS = 5              # 기본 blob 수
FLOW_BASE_SPEED = 0.08         # 기본 회전 속도 (초당 둘레 비율)
FLOW_TRANSITION_DURATION = 2.0 # palette crossfade 시간 (초)
FLOW_HSV_DRIFT_MAX = 0.15      # hue 일렁임 최대 속도

# 가우시안 blob 기본 크기
FLOW_WIDTH_MIN = 0.06          # 면적 작은 색 — 좁은 blob
FLOW_WIDTH_MAX = 0.16          # 면적 큰 색 — 넓은 blob

# 밝기 범위
FLOW_BRIGHTNESS_MIN = 0.5      # 면적 작은 색
FLOW_BRIGHTNESS_MAX = 1.3      # 면적 큰 색

# 음악 반응
FLOW_BASS_BRIGHT_MIN = 0.7     # bass=0일 때 밝기 배수
FLOW_BASS_BRIGHT_RANGE = 0.6   # bass=1일 때 추가 밝기 (total: 0.7 + 0.6 = 1.3)
FLOW_MID_DRIFT_BOOST = 0.5     # mid가 hsv_drift 진폭에 미치는 배수
FLOW_HIGH_WIDTH_SHRINK = 0.15  # high가 blob 폭을 줄이는 비율 (선명해짐)
FLOW_BASS_SPEED_BOOST = 0.02   # bass가 초당 phase에 추가하는 양

# 기본 초기 색상 (첫 화면 캡처 전 fallback)
_DEFAULT_INIT_COLORS = np.array([
    [255, 200, 150],  # 따뜻한 흰색
    [200, 150, 255],  # 연보라
    [150, 220, 255],  # 연하늘
    [255, 180, 100],  # 주황빛
    [180, 255, 200],  # 민트
], dtype=np.float32)


# ══════════════════════════════════════════════════════════════════
#  HSV 헬퍼 (flowing 전용 — 단일 색상 변환)
# ══════════════════════════════════════════════════════════════════

def _rgb_to_hsv(rgb):
    """(3,) RGB 0~255 → (h, s, v) 0~1."""
    r, g, b = rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
    mx = max(r, g, b)
    mn = min(r, g, b)
    diff = mx - mn
    if diff == 0:
        h = 0.0
    elif mx == r:
        h = ((g - b) / diff) % 6.0 / 6.0
    elif mx == g:
        h = ((b - r) / diff + 2.0) / 6.0
    else:
        h = ((r - g) / diff + 4.0) / 6.0
    s = 0.0 if mx == 0 else diff / mx
    v = mx
    return h, s, v


def _hsv_to_rgb(h, s, v):
    """스칼라 H, S, V (0~1) → (3,) float32 RGB 0~255."""
    h6 = (h % 1.0) * 6.0
    i = int(h6)
    f = h6 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    if i == 0:   r, g, b = v, t, p
    elif i == 1: r, g, b = q, v, p
    elif i == 2: r, g, b = p, v, t
    elif i == 3: r, g, b = p, q, v
    elif i == 4: r, g, b = t, p, v
    else:        r, g, b = v, p, q
    return np.array([r * 255.0, g * 255.0, b * 255.0], dtype=np.float32)


def _lerp_hsv(color_current, color_target, progress):
    """HSV 공간에서 두 RGB 색상을 보간. hue shortest path 사용.

    Args:
        color_current, color_target: (3,) RGB 0~255
        progress: 0~1

    Returns:
        (3,) RGB float32 0~255
    """
    progress = max(0.0, min(1.0, progress))

    h1, s1, v1 = _rgb_to_hsv(color_current)
    h2, s2, v2 = _rgb_to_hsv(color_target)

    # hue shortest path
    dh = h2 - h1
    if dh > 0.5:
        dh -= 1.0
    elif dh < -0.5:
        dh += 1.0

    h = (h1 + dh * progress) % 1.0
    s = s1 + (s2 - s1) * progress
    v = v1 + (v2 - v1) * progress

    return _hsv_to_rgb(h, max(0, min(1, s)), max(0, min(1, v)))


def _smooth_step(t):
    """Hermite 스무스 스텝 — 시작/끝이 부드러운 보간."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


# ══════════════════════════════════════════════════════════════════
#  FlowBlob — 하나의 색상 덩어리
# ══════════════════════════════════════════════════════════════════

class FlowBlob:
    """하나의 색상 blob — LED 둘레를 따라 회전하는 빛 덩어리.

    Attributes:
        color_current: (3,) float32 — 현재 RGB (0~255)
        color_target:  (3,) float32 — 전환 중인 목표 RGB
        phase:         float — 현재 둘레 위치 (0~1, clockwise_t 좌표계)
        speed:         float — 초당 이동량 (양수=시계방향)
        width:         float — 가우시안 σ (둘레 비율)
        brightness:    float — 기본 밝기 배수
        hsv_drift_rate: float — 초당 hue 미세 변동 속도
        hsv_drift_phase: float — drift 사이클의 현재 위상
    """
    __slots__ = (
        "color_current", "color_target",
        "phase", "speed", "width", "brightness",
        "hsv_drift_rate", "hsv_drift_phase",
    )

    def __init__(self, color, phase, speed, width, brightness, drift_rate):
        self.color_current = np.array(color, dtype=np.float32)
        self.color_target = np.array(color, dtype=np.float32)
        self.phase = phase
        self.speed = speed
        self.width = width
        self.brightness = brightness
        self.hsv_drift_rate = drift_rate
        self.hsv_drift_phase = 0.0


# ══════════════════════════════════════════════════════════════════
#  FlowPalette — N개 blob 집합 + crossfade 관리
# ══════════════════════════════════════════════════════════════════

class FlowPalette:
    """N개 FlowBlob + palette crossfade 상태.

    사용법:
        palette = FlowPalette(n_colors=5)
        palette.update_from_screen(per_led_colors)  # N초마다
        palette.tick(dt, bass, mid, high, base_speed)  # 매 프레임
        rgb = render_flowing(clockwise_t, palette, bass, brightness)
    """

    def __init__(self, n_colors=FLOW_N_COLORS):
        self.n_colors = n_colors
        self.blobs = []
        self.transition_progress = 1.0  # 1.0 = 전환 완료
        self.transition_duration = FLOW_TRANSITION_DURATION
        self._prev_centroids = None  # warm start용

        # 기본 색상으로 초기화 (첫 화면 캡처 전 fallback)
        self._init_default_blobs()

    def _init_default_blobs(self):
        """기본 따뜻한 색상으로 blob 초기화."""
        self.blobs = []
        for i in range(self.n_colors):
            color = _DEFAULT_INIT_COLORS[i % len(_DEFAULT_INIT_COLORS)]
            phase = i / self.n_colors  # 균등 배치
            speed = FLOW_BASE_SPEED + np.random.uniform(-0.02, 0.02)
            width = FLOW_WIDTH_MIN + (FLOW_WIDTH_MAX - FLOW_WIDTH_MIN) * 0.5
            brightness = (FLOW_BRIGHTNESS_MIN + FLOW_BRIGHTNESS_MAX) / 2
            drift = np.random.uniform(0.02, FLOW_HSV_DRIFT_MAX)
            self.blobs.append(FlowBlob(color, phase, speed, width, brightness, drift))

    def update_from_screen(self, per_led_colors):
        """Phase 1의 extract_dominant_colors()로 새 palette 추출.

        즉시 교체하지 않고 crossfade 시작.
        warm start로 이전 centroid를 재활용하여 색상 순서 안정화.

        Args:
            per_led_colors: (n_leds, 3) float32 — LED별 RGB
        """
        if per_led_colors is None or len(per_led_colors) == 0:
            return

        colors, ratios = extract_dominant_colors(
            per_led_colors,
            n_colors=self.n_colors,
            black_threshold=15,
            prev_centroids=self._prev_centroids,
        )
        self._prev_centroids = colors.copy()  # 다음 warm start용

        # 기존 blob에 새 target 설정
        for i in range(min(len(self.blobs), self.n_colors)):
            blob = self.blobs[i]
            blob.color_target = colors[i].copy()

            # 면적 비례 속성 갱신 (목표값 — crossfade 중 점진 적용)
            area = ratios[i] if i < len(ratios) else 0.2
            blob.width = FLOW_WIDTH_MIN + area * (FLOW_WIDTH_MAX - FLOW_WIDTH_MIN)
            blob.brightness = FLOW_BRIGHTNESS_MIN + area * (FLOW_BRIGHTNESS_MAX - FLOW_BRIGHTNESS_MIN)

        # crossfade 시작
        self.transition_progress = 0.0

    def tick(self, dt, bass, mid, high, base_speed=FLOW_BASE_SPEED):
        """매 프레임: phase 진행 + crossfade + HSV drift + 음악 반응.

        Args:
            dt: 프레임 시간 (초)
            bass, mid, high: 스무딩된 오디오 에너지 (0~1)
            base_speed: 기본 회전 속도 (UI 슬라이더)
        """
        # ── 1. Palette crossfade 진행 ──
        if self.transition_progress < 1.0:
            self.transition_progress += dt / self.transition_duration
            self.transition_progress = min(1.0, self.transition_progress)

            t = _smooth_step(self.transition_progress)
            for blob in self.blobs:
                blob.color_current = _lerp_hsv(
                    blob.color_current, blob.color_target, t
                )

        # ── 2. Phase 진행 (회전) ──
        for blob in self.blobs:
            # 기본 회전 + bass 반응 (bass가 강하면 약간 빨라짐)
            blob.phase += blob.speed * dt
            blob.phase += bass * FLOW_BASS_SPEED_BOOST * dt
            blob.phase %= 1.0

        # ── 3. HSV drift (미세 색상 변동 — 일렁이는 느낌) ──
        for blob in self.blobs:
            if blob.hsv_drift_rate <= 0:
                continue

            blob.hsv_drift_phase += dt * 1.5  # drift 사이클 속도

            # mid가 drift 진폭을 증가시킴
            drift_amp = blob.hsv_drift_rate * (1.0 + mid * FLOW_MID_DRIFT_BOOST)
            h, s, v = _rgb_to_hsv(blob.color_current)
            h_delta = drift_amp * dt * np.sin(blob.hsv_drift_phase + blob.phase * 6.28)
            h = (h + h_delta) % 1.0
            blob.color_current = _hsv_to_rgb(h, s, v)


# ══════════════════════════════════════════════════════════════════
#  렌더링
# ══════════════════════════════════════════════════════════════════

def render_flowing(clockwise_t, palette, bass, brightness):
    """FlowPalette → (n_leds, 3) float32 RGB.

    각 blob의 가우시안 영향을 additive blend.
    per-LED soft clamp (색조 보호).

    Args:
        clockwise_t: (n_leds,) float64 — LED 둘레 좌표 (0~1)
        palette: FlowPalette
        bass: float — 현재 bass 에너지 (스무딩 후, 0~1)
        brightness: float — UI 밝기 설정 (0~1)

    Returns:
        (n_leds, 3) float32 — 보정 전 raw RGB 0~255
    """
    n_leds = len(clockwise_t)
    rgb = np.zeros((n_leds, 3), dtype=np.float64)

    for blob in palette.blobs:
        # 1. 둘레 거리 계산 (circular)
        delta = clockwise_t - blob.phase
        delta = delta - np.round(delta)  # wrap to [-0.5, 0.5]

        # 2. 가우시안 밝기 profile
        w = blob.width
        if w <= 0:
            continue
        two_sigma_sq = 2.0 * w * w
        influence = np.exp(-(delta * delta) / two_sigma_sq)

        # 3. 색상 × 밝기 × influence → additive blend
        color_scaled = blob.color_current * blob.brightness * influence[:, np.newaxis]
        rgb += color_scaled

    # 4. 음악 반응: bass로 전체 밝기 변조
    bass_mod = FLOW_BASS_BRIGHT_MIN + bass * FLOW_BASS_BRIGHT_RANGE
    rgb *= bass_mod * brightness

    # 5. Soft clamp (색조 보호)
    # 채널별 max를 구해서 255 초과 시 비례 축소
    max_per_led = rgb.max(axis=1, keepdims=True)
    scale = np.where(max_per_led > 255, 255.0 / max_per_led, 1.0)
    rgb *= scale

    # 최종 clamp
    np.clip(rgb, 0, 255, out=rgb)

    return rgb.astype(np.float32)
