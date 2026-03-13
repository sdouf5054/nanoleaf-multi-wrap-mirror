"""엔진 유틸리티 — LED 둘레 매핑, 밴드 계산, 구역 매핑, 공용 상수

engine.py에서 분리된 순수 함수 모듈입니다.
UnifiedEngine과 UI 위젯(gradient_preview, spectrum) 양쪽에서 사용됩니다.
"""

import numpy as np
from core.layout import get_led_positions

# ══════════════════════════════════════════════════════════════════
#  공용 상수 — engine.py, tab_control.py, main_window.py 등에서 사용
# ══════════════════════════════════════════════════════════════════

# 엔진 모드
MODE_MIRROR = "mirror"
MODE_AUDIO = "audio"
MODE_HYBRID = "hybrid"

# 오디오 서브모드
AUDIO_PULSE = "pulse"
AUDIO_SPECTRUM = "spectrum"
AUDIO_BASS_DETAIL = "bass_detail"

# 색상 소스 (hybrid 모드)
COLOR_SOURCE_SOLID = "solid"
COLOR_SOURCE_SCREEN = "screen"

# 특수 구역 수: LED 개별 미러링
N_ZONES_PER_LED = -1

# 스크린 갱신 간격 (오디오 프레임 수 기준)
SCREEN_UPDATE_INTERVAL = 3

# stale 복구 관련
_STALE_RECREATE_COOLDOWN = 3.0
_STALE_LED_OFF_THRESHOLD = 10.0

# 오디오 관련
DEFAULT_FPS = 60
MIN_BRIGHTNESS = 0.02
DEFAULT_ZONE_WEIGHTS = (33, 33, 34)

# 저역 세밀 모드 주파수 범위
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
    """각 LED가 어느 screen zone에 매핑되는지 계산.

    멀티랩 자동 처리: get_led_positions()가 모든 세그먼트의
    LED 위치와 side를 반환하므로, 같은 side의 LED는
    바깥/안쪽 바퀴 무관하게 같은 zone 그룹에 속합니다.
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
        pass  # 이미 0으로 초기화됨

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
        side_to_zone = {"top": 0, "right": 1, "bottom": 2, "left": 3}
        for i in range(led_count):
            mapping[i] = side_to_zone.get(sides[i], 0)

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
    """per-LED 색상 배열에서 구역별 평균 색상을 계산.

    Args:
        per_led_colors: (n_leds, 3) float32 — LED별 RGB
        zone_map: (n_leds,) int32 — LED→zone 매핑
        n_zones: 구역 수

    Returns:
        (n_zones, 3) float32 — 구역별 평균 RGB
    """
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
