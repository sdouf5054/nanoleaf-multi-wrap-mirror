"""Flowing 모드 — 화면 색 기반 컬러 플로우 렌더링

[Phase 4] 하이브리드 전용. 화면에서 추출한 dominant colors가
LED 둘레를 시계방향으로 회전하며, 음악 에너지에 따라
밝기와 속도가 변화.

[Hotfix] 이전 색 잔류 문제 해결:
  - 절대 보간: color_start→color_target 고정 경로로 crossfade
    (drift가 시작점을 밀어내는 문제 제거)
  - 스마트 warm start: 화면이 크게 바뀌면 prev_centroids 리셋
    (이전 색이 K-means를 고착시키는 문제 제거)
  - drift를 렌더링 시점에만 적용
    (color_current는 순수 crossfade 결과만 유지)
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
FLOW_WIDTH_MIN = 0.04          # 면적 작은 색 — 좁은 blob
FLOW_WIDTH_MAX = 0.08          # 면적 큰 색 — 넓은 blob

# 밝기 범위
FLOW_BRIGHTNESS_MIN = 0.9      # 면적 작은 색
FLOW_BRIGHTNESS_MAX = 1.2      # 면적 큰 색

# 음악 반응
FLOW_BASS_BRIGHT_MIN = 0.7     # bass=0일 때 밝기 배수
FLOW_BASS_BRIGHT_RANGE = 0.6   # bass=1일 때 추가 밝기
FLOW_MID_DRIFT_BOOST = 0.5     # mid가 hsv_drift 진폭에 미치는 배수
FLOW_BASS_SPEED_BOOST = 0.02   # bass가 초당 phase에 추가하는 양

# ★ warm start 리셋 임계값 — 이전 palette와 새 추출의 평균 거리가 이 값 이상이면
# prev_centroids를 버리고 K-means를 처음부터 실행
_WARM_START_RESET_THRESHOLD = 80.0  # RGB 0~255 스케일 (대략 색 1/3 변화)

# 기본 초기 색상 (첫 화면 캡처 전 fallback)
_DEFAULT_INIT_COLORS = np.array([
    [255, 200, 150],  # 따뜻한 흰색
    [200, 150, 255],  # 연보라
    [150, 220, 255],  # 연하늘
    [255, 180, 100],  # 주황빛
    [180, 255, 200],  # 민트
], dtype=np.float32)


# ══════════════════════════════════════════════════════════════════
#  HSV 헬퍼 (flowing 전용)
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


def _lerp_hsv(color_a, color_b, t):
    """HSV 공간에서 두 RGB 색상을 보간. hue shortest path."""
    t = max(0.0, min(1.0, t))
    h1, s1, v1 = _rgb_to_hsv(color_a)
    h2, s2, v2 = _rgb_to_hsv(color_b)
    dh = h2 - h1
    if dh > 0.5:
        dh -= 1.0
    elif dh < -0.5:
        dh += 1.0
    h = (h1 + dh * t) % 1.0
    s = s1 + (s2 - s1) * t
    v = v1 + (v2 - v1) * t
    return _hsv_to_rgb(h, max(0, min(1, s)), max(0, min(1, v)))


def _smooth_step(t):
    """Hermite 스무스 스텝."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


# ══════════════════════════════════════════════════════════════════
#  FlowBlob — 하나의 색상 덩어리
# ══════════════════════════════════════════════════════════════════

class FlowBlob:
    """하나의 색상 blob.

    [Hotfix] color_start 추가:
    - crossfade는 항상 color_start→color_target 고정 경로
    - color_current는 crossfade 결과만 저장 (drift에 의해 변형 안 됨)
    - drift는 render_flowing()에서 적용
    """
    __slots__ = (
        "color_current", "color_target", "color_start",
        "phase", "speed", "width", "brightness",
        "hsv_drift_rate", "hsv_drift_phase",
    )

    def __init__(self, color, phase, speed, width, brightness, drift_rate):
        self.color_current = np.array(color, dtype=np.float32)
        self.color_target = np.array(color, dtype=np.float32)
        self.color_start = np.array(color, dtype=np.float32)
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
    """N개 FlowBlob + palette crossfade 상태."""

    def __init__(self, n_colors=FLOW_N_COLORS):
        self.n_colors = n_colors
        self.blobs = []
        self.transition_progress = 1.0
        self.transition_duration = FLOW_TRANSITION_DURATION
        self._prev_centroids = None

        self._init_default_blobs()

    def _init_default_blobs(self):
        self.blobs = []
        for i in range(self.n_colors):
            color = _DEFAULT_INIT_COLORS[i % len(_DEFAULT_INIT_COLORS)]
            phase = i / self.n_colors
            speed = FLOW_BASE_SPEED + np.random.uniform(-0.02, 0.02)
            width = FLOW_WIDTH_MIN + (FLOW_WIDTH_MAX - FLOW_WIDTH_MIN) * 0.5
            brightness = (FLOW_BRIGHTNESS_MIN + FLOW_BRIGHTNESS_MAX) / 2
            drift = np.random.uniform(0.02, FLOW_HSV_DRIFT_MAX)
            self.blobs.append(FlowBlob(color, phase, speed, width, brightness, drift))

    def update_from_screen(self, per_led_colors):
        """새 palette 추출 + crossfade 시작.

        [Hotfix] 스마트 warm start:
        - 이전 palette와 새 추출 결과의 거리가 크면 prev_centroids를 리셋
        - 이렇게 하면 화면이 크게 바뀔 때 이전 색에 고착되지 않음
        """
        if per_led_colors is None or len(per_led_colors) == 0:
            return

        # ★ 1단계: 먼저 warm start 없이 추출해서 현재 화면의 실제 색 확인
        fresh_colors, _ = extract_dominant_colors(
            per_led_colors,
            n_colors=self.n_colors,
            black_threshold=15,
            prev_centroids=None,  # 순수 추출
        )

        # ★ 2단계: 이전 palette와 비교하여 warm start 사용 여부 결정
        use_warm_start = False
        if self._prev_centroids is not None:
            avg_dist = np.mean(np.sqrt(np.sum(
                (fresh_colors - self._prev_centroids) ** 2, axis=1
            )))
            use_warm_start = avg_dist < _WARM_START_RESET_THRESHOLD

        # ★ 3단계: 결정에 따라 최종 추출
        if use_warm_start:
            colors, ratios = extract_dominant_colors(
                per_led_colors,
                n_colors=self.n_colors,
                black_threshold=15,
                prev_centroids=self._prev_centroids,
            )
        else:
            colors, ratios = fresh_colors, _  # 이미 추출한 결과 사용
            # 아, fresh_colors에서 ratios가 없어. 다시 추출
            colors, ratios = extract_dominant_colors(
                per_led_colors,
                n_colors=self.n_colors,
                black_threshold=15,
                prev_centroids=None,
            )

        self._prev_centroids = colors.copy()

        # ★ 4단계: blob에 새 target 설정 + crossfade 시작점 고정
        for i in range(min(len(self.blobs), self.n_colors)):
            blob = self.blobs[i]
            blob.color_start = blob.color_current.copy()  # ★ 현재 색을 시작점으로 고정
            blob.color_target = colors[i].copy()

            area = ratios[i] if i < len(ratios) else 0.2
            blob.width = FLOW_WIDTH_MIN + area * (FLOW_WIDTH_MAX - FLOW_WIDTH_MIN)
            blob.brightness = FLOW_BRIGHTNESS_MIN + area * (FLOW_BRIGHTNESS_MAX - FLOW_BRIGHTNESS_MIN)

        self.transition_progress = 0.0

    def tick(self, dt, bass, mid, high, base_speed=FLOW_BASE_SPEED):
        """매 프레임: phase 진행 + crossfade.

        [Hotfix] drift는 여기서 하지 않음 — render_flowing()에서 적용.
        color_current는 순수 crossfade 결과만 보유.
        """
        # ── 1. Palette crossfade: color_start → color_target 절대 보간 ──
        if self.transition_progress < 1.0:
            self.transition_progress += dt / self.transition_duration
            self.transition_progress = min(1.0, self.transition_progress)

        t = _smooth_step(self.transition_progress)
        for blob in self.blobs:
            blob.color_current = _lerp_hsv(blob.color_start, blob.color_target, t)

        # ── 2. Phase 진행 (회전) ──
        for blob in self.blobs:
            blob.phase += blob.speed * dt
            blob.phase += bass * FLOW_BASS_SPEED_BOOST * dt
            blob.phase %= 1.0

        # ── 3. drift phase 진행 (실제 drift는 render에서 적용) ──
        for blob in self.blobs:
            blob.hsv_drift_phase += dt * 1.5


# ══════════════════════════════════════════════════════════════════
#  렌더링
# ══════════════════════════════════════════════════════════════════

def render_flowing(clockwise_t, palette, bass, brightness, mid=0.0):
    """FlowPalette → (n_leds, 3) float32 RGB.

    [Hotfix] drift를 렌더링 시점에 적용:
    - blob.color_current(순수 crossfade 결과)에서 hue를 미세 변동
    - color_current 자체는 변형하지 않음 → crossfade 수렴에 영향 없음

    Args:
        clockwise_t: (n_leds,) float64 — LED 둘레 좌표 (0~1)
        palette: FlowPalette
        bass: float — 현재 bass 에너지 (0~1)
        brightness: float — UI 밝기 설정 (0~1)
        mid: float — 현재 mid 에너지 (0~1, drift 진폭 증가용)

    Returns:
        (n_leds, 3) float32 — 보정 전 raw RGB 0~255
    """
    n_leds = len(clockwise_t)
    rgb = np.zeros((n_leds, 3), dtype=np.float64)

    for blob in palette.blobs:
        # 1. 둘레 거리 (circular)
        delta = clockwise_t - blob.phase
        delta = delta - np.round(delta)

        # 2. 가우시안
        w = blob.width
        if w <= 0:
            continue
        two_sigma_sq = 2.0 * w * w
        influence = np.exp(-(delta * delta) / two_sigma_sq)

        # 3. ★ drift를 렌더링 시점에 적용 (color_current를 변형하지 않음)
        render_color = blob.color_current
        if blob.hsv_drift_rate > 0:
            drift_amp = blob.hsv_drift_rate * (1.0 + mid * FLOW_MID_DRIFT_BOOST)
            h, s, v = _rgb_to_hsv(blob.color_current)
            h_delta = drift_amp * 0.016 * np.sin(blob.hsv_drift_phase + blob.phase * 6.28)
            render_color = _hsv_to_rgb((h + h_delta) % 1.0, s, v)

        # 4. 색상 × 밝기 × influence → additive blend
        color_scaled = render_color * blob.brightness * influence[:, np.newaxis]
        rgb += color_scaled

    # 5. 음악 반응
    bass_mod = FLOW_BASS_BRIGHT_MIN + bass * FLOW_BASS_BRIGHT_RANGE
    rgb *= bass_mod * brightness

    # 6. Soft clamp (색조 보호)
    max_per_led = rgb.max(axis=1, keepdims=True)
    scale = np.where(max_per_led > 255, 255.0 / max_per_led, 1.0)
    rgb *= scale

    np.clip(rgb, 0, 255, out=rgb)
    return rgb.astype(np.float32)