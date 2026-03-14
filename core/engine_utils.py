"""엔진 유틸리티 — LED 둘레 매핑, 밴드 계산, 구역 매핑, 공용 상수

[ADR-014 적용] 벡터화된 오디오 렌더링 헬퍼 함수 추가
- build_base_color_array(): 전체 LED 색상 배열을 한 번에 생성
- vectorized_render_pulse(): Python 루프 없이 펄스 렌더링
- vectorized_render_spectrum(): Python 루프 없이 스펙트럼 렌더링

순수 numpy 모듈. Qt 의존성 없음.
"""

import numpy as np
from core.layout import get_led_positions

# ══════════════════════════════════════════════════════════════════
#  공용 상수
# ══════════════════════════════════════════════════════════════════

MODE_MIRROR = "mirror"
MODE_AUDIO = "audio"
MODE_HYBRID = "hybrid"

AUDIO_PULSE = "pulse"
AUDIO_SPECTRUM = "spectrum"
AUDIO_BASS_DETAIL = "bass_detail"

COLOR_SOURCE_SOLID = "solid"
COLOR_SOURCE_SCREEN = "screen"

N_ZONES_PER_LED = -1

SCREEN_UPDATE_INTERVAL = 3

_STALE_RECREATE_COOLDOWN = 3.0
_STALE_LED_OFF_THRESHOLD = 10.0

DEFAULT_FPS = 60
MIN_BRIGHTNESS = 0.02
DEFAULT_ZONE_WEIGHTS = (33, 33, 34)

BASS_DETAIL_FREQ_MIN = 20
BASS_DETAIL_FREQ_MAX = 500
BASS_DETAIL_N_BANDS = 16


# ══════════════════════════════════════════════════════════════════
#  유틸리티 함수
# ══════════════════════════════════════════════════════════════════

def _remap_t(t, zone_weights):
    """균등 둘레 비율 t(0~1)를 대역 비율에 맞게 색상/밴드 t로 변환."""
    b_pct = zone_weights[0] / 100.0
    m_pct = zone_weights[1] / 100.0
    h_pct = zone_weights[2] / 100.0

    t_bound1 = b_pct
    t_bound2 = b_pct + m_pct

    c0, c1 = 0.0, 1.0 / 3.0
    c2, c3 = 1.0 / 3.0, 2.0 / 3.0
    c4, c5 = 2.0 / 3.0, 1.0

    t = max(0.0, min(1.0, t))

    if t <= t_bound1 and b_pct > 0:
        frac = t / b_pct
        return c0 + frac * (c1 - c0)
    elif t <= t_bound2 and m_pct > 0:
        frac = (t - t_bound1) / m_pct
        return c2 + frac * (c3 - c2)
    elif h_pct > 0:
        frac = (t - t_bound2) / h_pct
        return c4 + frac * (c5 - c4)
    else:
        return t


def _compute_led_perimeter_t(config):
    """각 LED의 균등 둘레 비율 t(0~1)를 계산."""
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

    cx = screen_w / 2.0
    half_bottom = screen_w / 2.0
    side_height = screen_h
    half_top = screen_w / 2.0
    half_perimeter = half_bottom + side_height + half_top

    perimeter_t = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        x, y = positions[i, 0], positions[i, 1]
        side = sides[i]

        if side == "bottom":
            dist = abs(x - cx)
        elif side == "left" or side == "right":
            dist = half_bottom + (screen_h - y)
        elif side == "top":
            dist_to_center = abs(x - cx)
            dist = half_bottom + side_height + (half_top - dist_to_center)
        else:
            dist = 0.0

        t = dist / half_perimeter if half_perimeter > 0 else 0.5
        perimeter_t[i] = max(0.0, min(1.0, t))

    return perimeter_t


def _compute_led_band_mapping(perimeter_t, n_bands, zone_weights):
    """둘레 비율 + 대역 비율 → 각 LED의 밴드 인덱스."""
    led_count = len(perimeter_t)
    band_indices = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        remapped = _remap_t(perimeter_t[i], zone_weights)
        band_indices[i] = remapped * (n_bands - 1)

    return band_indices


def _build_led_order_from_segments(segments, led_count):
    """세그먼트 순서대로 LED 인덱스의 물리적 순서를 반환."""
    order = []
    for seg in segments:
        start, end = seg["start"], seg["end"]
        if start > end:
            indices = list(range(start, end, -1))
        elif start < end:
            indices = list(range(start, end))
        else:
            continue
        for idx in indices:
            if idx not in order and 0 <= idx < led_count:
                order.append(idx)
    for i in range(led_count):
        if i not in order:
            order.append(i)
    return order


def _build_led_zone_map_by_side(config, n_zones):
    """각 LED가 어느 screen zone에 매핑되는지 계산."""
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
        pass

    elif n_zones == 2:
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
        cx, cy = screen_w / 2.0, screen_h / 2.0
        for i in range(led_count):
            x, y = positions[i]
            if x <= cx:
                mapping[i] = 0 if y <= cy else 3  # 좌상=0, 좌하=3
            else:
                mapping[i] = 1 if y <= cy else 2  # 우상=1, 우하=2

    elif n_zones == 8:
        cx, cy = screen_w / 2.0, screen_h / 2.0
        for i in range(led_count):
            x, y = positions[i]
            side = sides[i]
            if side == "top":
                mapping[i] = 0 if x <= cx else 1
            elif side == "right":
                mapping[i] = 2 if y <= cy else 3
            elif side == "bottom":
                mapping[i] = 4 if x >= cx else 5
            elif side == "left":
                mapping[i] = 6 if y >= cy else 7
            else:
                mapping[i] = 0

    else:
        for i in range(led_count):
            side = sides[i]
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


def per_led_to_zone_colors(per_led_colors, zone_map, n_zones):
    """per-LED 색상 배열에서 구역별 평균 색상을 계산."""
    zone_colors = np.zeros((n_zones, 3), dtype=np.float32)
    zone_counts = np.zeros(n_zones, dtype=np.int32)

    for i in range(len(per_led_colors)):
        zi = zone_map[i]
        if 0 <= zi < n_zones:
            zone_colors[zi] += per_led_colors[i]
            zone_counts[zi] += 1

    for zi in range(n_zones):
        if zone_counts[zi] > 0:
            zone_colors[zi] /= zone_counts[zi]

    return zone_colors


# ══════════════════════════════════════════════════════════════════
#  [ADR-014] 벡터화된 오디오 렌더링 헬퍼
# ══════════════════════════════════════════════════════════════════

# 무지개 키포인트 (AudioEngineMixin._band_color과 동일)
RAINBOW_KEYPOINTS = np.array([
    [0.000, 255,   0,   0],
    [0.130, 255, 127,   0],
    [0.260, 255, 255,   0],
    [0.400,   0, 255,   0],
    [0.540,   0, 180, 255],
    [0.680,   0,  50, 255],
    [0.820,  80,   0, 255],
    [1.000, 160,   0, 220],
], dtype=np.float32)


def band_color_vectorized(t_array):
    """밴드 위치 배열 (n,) → RGB 배열 (n, 3).

    Python 루프 대신 numpy 벡터 연산으로 전체 LED의 무지개 색상을 한 번에 계산.
    """
    t = np.clip(t_array, 0.0, 1.0)
    n = len(t)
    result = np.zeros((n, 3), dtype=np.float32)

    kp = RAINBOW_KEYPOINTS
    for seg_idx in range(len(kp) - 1):
        t0 = kp[seg_idx, 0]
        t1 = kp[seg_idx + 1, 0]
        rgb0 = kp[seg_idx, 1:4]
        rgb1 = kp[seg_idx + 1, 1:4]

        mask = (t >= t0) & (t <= t1)
        if not mask.any():
            continue

        frac = np.where(
            t1 > t0,
            (t[mask] - t0) / (t1 - t0),
            np.float32(0.0)
        )
        # (k, 1) * (3,) broadcast
        result[mask] = rgb0 + frac[:, np.newaxis] * (rgb1 - rgb0)

    return result


def build_base_color_array(led_band_indices, n_bands, rainbow=True,
                           solid_color=None, screen_colors=None):
    """전체 LED의 기본 색상 배열을 한 번에 생성.

    [ADR-014] 매 프레임 Python 루프를 제거. 색상/모드 변경 시에만 재빌드.

    Args:
        led_band_indices: (n_leds,) float64 — LED별 밴드 인덱스
        n_bands: 총 밴드 수
        rainbow: 무지개 모드 여부
        solid_color: (3,) float32 — 단색 RGB (rainbow=False일 때)
        screen_colors: (n_leds, 3) float32 — 화면 색상 (하이브리드)

    Returns:
        (n_leds, 3) float32 — LED별 기본 RGB 색상
    """
    n_leds = len(led_band_indices)

    if screen_colors is not None:
        return screen_colors.copy()

    if rainbow:
        t = led_band_indices / max(1, n_bands - 1)
        return band_color_vectorized(t)

    # 단색
    if solid_color is not None:
        return np.broadcast_to(solid_color, (n_leds, 3)).copy()

    return np.full((n_leds, 3), 255.0, dtype=np.float32)


def vectorized_render_pulse(base_colors, bass, mid, high,
                            min_brightness, audio_brightness):
    """[ADR-014] 벡터화된 Pulse 렌더링.

    밝기 모델:
    - bass → 전체 밝기 (intensity)
    - mid → 채도 부스트 (1.0~1.3배, 중역 강할수록 색이 선명)
    - high → 화이트 틴트 (고역 강할수록 밝은 반짝임)
    - min_brightness=1.0이면 미러링 brightness=100%와 동일한 밝기

    Args:
        base_colors: (n_leds, 3) float32
        bass, mid, high: float 0~1
        min_brightness, audio_brightness: float

    Returns:
        (n_leds, 3) float32 — 보정 전 raw RGB
    """
    intensity = max(min_brightness, bass) * audio_brightness

    # mid → 채도 부스트: 보컬/기타가 들릴 때 색이 풍부해짐
    saturation_boost = 1.0 + mid * 0.3
    leds = np.clip(base_colors * saturation_boost, 0, 255)

    # high → 화이트 틴트: 하이햇/심벌에 반짝임
    white_mix = high * 0.15
    leds = leds * (1.0 - white_mix) + 255.0 * white_mix

    leds *= intensity
    return leds


def vectorized_render_spectrum(base_colors, led_band_indices, spectrum,
                               min_brightness, audio_brightness):
    """[ADR-014] 벡터화된 Spectrum 렌더링.

    Python 루프 없이 전체 LED를 한 번에 계산.

    Args:
        base_colors: (n_leds, 3) float32
        led_band_indices: (n_leds,) float64
        spectrum: (n_bands,) float64
        min_brightness, audio_brightness: float

    Returns:
        (n_leds, 3) float32 — 보정 전 raw RGB
    """
    n_bands = len(spectrum)

    # 밴드 인덱스에서 에너지를 보간하여 추출
    band_lo = np.clip(np.floor(led_band_indices).astype(np.int32), 0, n_bands - 1)
    band_hi = np.clip(band_lo + 1, 0, n_bands - 1)
    frac = led_band_indices - np.floor(led_band_indices)

    energy = spectrum[band_lo] * (1.0 - frac) + spectrum[band_hi] * frac
    intensity = np.maximum(min_brightness, energy) * audio_brightness

    # (n_leds, 3) * (n_leds, 1) broadcast
    leds = base_colors * intensity[:, np.newaxis]

    return leds.astype(np.float32)


def leds_to_grb(leds):
    """LED RGB 배열을 GRB bytes로 변환.

    Args:
        leds: (n_leds, 3) float32, 0~255

    Returns:
        bytes — GRB 순서
    """
    np.clip(leds, 0, 255, out=leds)
    u8 = leds.astype(np.uint8)
    grb = np.empty_like(u8)
    grb[:, 0] = u8[:, 1]  # G
    grb[:, 1] = u8[:, 0]  # R
    grb[:, 2] = u8[:, 2]  # B
    return grb.tobytes()
