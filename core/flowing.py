"""Flowing 모드 — 화면 색 기반 컬러 플로우 렌더링

[Phase 4] 하이브리드 전용. 화면에서 추출한 dominant colors가
LED 둘레를 시계방향으로 회전하며, 음악 에너지에 따라
밝기와 속도가 변화.

[Hotfix v3] 유령 색 즉시 퇴출:
  기존 문제: warm start K-means가 화면에 없는 이전 색을 끌어당김.
  stale_count 기반 감지는 3주기(~9초)가 걸리고,
  warm start가 매번 유사한 유령 색을 재생성하여 stale_count가 리셋됨.

  해결: fresh 추출(warm start 없이)과 warm 추출 결과를 직접 비교.
  warm 결과의 각 색이 fresh 결과의 어떤 색과도 멀면 "유령"으로 판단하고
  해당 slot을 fresh 색으로 즉시 교체. 1주기만에 해결.

  추가: _prev_centroids를 항상 최종 결과(유령 교체 후)로 갱신하여
  다음 갱신에서 유령이 warm start에 재투입되는 것을 원천 차단.
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

# warm start 리셋 임계값
_WARM_START_RESET_THRESHOLD = 80.0
_WARM_START_PER_CENTROID_THRESHOLD = 120.0

# ★ [v3] 유령 색 감지 임계값
# warm 결과의 한 색이 fresh 결과의 모든 색과 이 거리 이상이면 "유령"
_GHOST_DISTANCE_THRESHOLD = 60.0

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


def _min_dist_to_set(color, color_set):
    """한 색과 색 집합 사이의 최소 유클리드 거리."""
    dists = np.sqrt(np.sum((color_set - color) ** 2, axis=1))
    return float(np.min(dists))


# ══════════════════════════════════════════════════════════════════
#  FlowBlob — 하나의 색상 덩어리
# ══════════════════════════════════════════════════════════════════

class FlowBlob:
    """하나의 색상 blob."""
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

        [v3] fresh vs warm 비교로 유령 색 즉시 퇴출.

        전략:
        1. fresh 추출 (warm start 없이) → 화면의 실제 색
        2. warm start 조건 판단 → 조건 충족 시 warm 추출
        3. warm 결과의 각 색을 fresh 결과와 비교:
           - fresh의 어떤 색과도 거리 > threshold → "유령" → fresh 색으로 교체
           - warm start의 안정성은 유지하면서 유령만 제거
        4. 최종 결과를 _prev_centroids에 저장 (유령이 다음에 재투입 안 됨)
        """
        if per_led_colors is None or len(per_led_colors) == 0:
            return

        # ── 1단계: fresh 추출 (화면의 실제 색) ──
        fresh_colors, fresh_ratios = extract_dominant_colors(
            per_led_colors,
            n_colors=self.n_colors,
            black_threshold=15,
            prev_centroids=None,
        )

        # ── 2단계: warm start 판단 ──
        use_warm_start = False
        if self._prev_centroids is not None:
            per_centroid_dists = np.sqrt(np.sum(
                (fresh_colors - self._prev_centroids) ** 2, axis=1
            ))
            avg_dist = float(np.mean(per_centroid_dists))
            max_dist = float(np.max(per_centroid_dists))

            use_warm_start = (
                avg_dist < _WARM_START_RESET_THRESHOLD
                and max_dist < _WARM_START_PER_CENTROID_THRESHOLD
            )

        # ── 3단계: 추출 + 유령 교체 ──
        if use_warm_start:
            warm_colors, warm_ratios = extract_dominant_colors(
                per_led_colors,
                n_colors=self.n_colors,
                black_threshold=15,
                prev_centroids=self._prev_centroids,
            )

            # ★ 유령 감지: warm 결과의 각 색이 fresh의 모든 색과 먼 경우
            final_colors = warm_colors.copy()
            final_ratios = warm_ratios.copy()

            for i in range(self.n_colors):
                min_dist = _min_dist_to_set(warm_colors[i], fresh_colors)
                if min_dist > _GHOST_DISTANCE_THRESHOLD:
                    # 유령 → fresh 결과에서 이 warm 색과 가장 가까운 fresh 색으로 교체
                    best_fresh_idx = np.argmin(
                        np.sqrt(np.sum((fresh_colors - warm_colors[i]) ** 2, axis=1))
                    )
                    final_colors[i] = fresh_colors[best_fresh_idx]
                    final_ratios[i] = fresh_ratios[best_fresh_idx]

            colors, ratios = final_colors, final_ratios
        else:
            colors, ratios = fresh_colors, fresh_ratios

        # ── 4단계: blob에 새 target 설정 ──
        for i in range(min(len(self.blobs), self.n_colors)):
            blob = self.blobs[i]
            blob.color_start = blob.color_current.copy()
            blob.color_target = colors[i].copy()

            area = ratios[i] if i < len(ratios) else 0.2
            blob.width = FLOW_WIDTH_MIN + area * (FLOW_WIDTH_MAX - FLOW_WIDTH_MIN)
            blob.brightness = FLOW_BRIGHTNESS_MIN + area * (FLOW_BRIGHTNESS_MAX - FLOW_BRIGHTNESS_MIN)

        # ★ 유령 교체 후의 최종 결과를 저장 → 다음 warm start에 유령이 재투입 안 됨
        self._prev_centroids = colors.copy()
        self.transition_progress = 0.0

    def tick(self, dt, bass, mid, high, base_speed=FLOW_BASE_SPEED):
        """매 프레임: phase 진행 + crossfade.

        [수정] base_speed를 실제 회전 속도에 반영.
        blob별 속도 오프셋은 유지하여 각 blob이 약간씩 다른 속도로 회전.
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
            # ★ base_speed(UI 슬라이더) + blob별 오프셋
            speed_offset = blob.speed - FLOW_BASE_SPEED
            effective_speed = base_speed + speed_offset
            blob.phase += effective_speed * dt
            blob.phase += bass * FLOW_BASS_SPEED_BOOST * dt
            blob.phase %= 1.0

        # ── 3. drift phase 진행 ──
        for blob in self.blobs:
            blob.hsv_drift_phase += dt * 1.5


# ══════════════════════════════════════════════════════════════════
#  렌더링
# ══════════════════════════════════════════════════════════════════

def render_flowing(clockwise_t, palette, bass, brightness, mid=0.0,
                   min_brightness=0.05):
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
        min_brightness: float — 무음 시 최소 밝기 (0~1, 슬라이더 값)

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

    # 5. 음악 반응 — min_brightness가 무음 시 밝기 바닥을 결정
    bass_floor = max(min_brightness, 0.02)  # 최소 보장
    bass_mod = bass_floor + bass * FLOW_BASS_BRIGHT_RANGE
    rgb *= bass_mod * brightness

    # 6. Soft clamp (색조 보호)
    max_per_led = rgb.max(axis=1, keepdims=True)
    scale = np.where(max_per_led > 255, 255.0 / max_per_led, 1.0)
    rgb *= scale

    np.clip(rgb, 0, 255, out=rgb)
    return rgb.astype(np.float32)