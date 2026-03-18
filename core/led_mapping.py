"""LED 매핑 — 둘레 좌표, 밴드 매핑, 구역 매핑, LED 순서

engine_utils.py에서 분리. 순수 numpy + core.layout 의존.

포함 함수:
  _remap_t                      — 균등 둘레 비율 → 대역 비율 변환
  _compute_led_perimeter_t      — LED 둘레 좌표 (하단 중앙 기준)
  _compute_led_clockwise_t      — LED 시계방향 둘레 좌표
  _compute_led_band_mapping     — LED별 밴드 인덱스
  compute_led_normalized_y      — Wave 전파 위치
  _build_led_order_from_segments — 세그먼트 순서 → LED 물리 순서
  _build_led_zone_map_by_side   — LED → 화면 구역 매핑
  per_led_to_zone_colors        — per-LED → 구역별 평균
  compute_side_t_ranges         — 면별 clockwise_t 범위
"""

import numpy as np
from core.layout import get_led_positions


# ══════════════════════════════════════════════════════════════════
#  둘레 비율 변환
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


# ══════════════════════════════════════════════════════════════════
#  LED 둘레 좌표
# ══════════════════════════════════════════════════════════════════

def _compute_led_perimeter_t(config):
    """각 LED의 균등 둘레 비율 t(0~1)를 계산.

    하단 중앙(0.0) → 좌측은 시계방향 / 우측은 반시계방향으로
    모니터 둘레를 타고 올라가서 → 상단 중앙(1.0)에서 합류.
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


def _compute_led_clockwise_t(config):
    """시계방향 한 바퀴 둘레 좌표 — 모든 LED가 고유 위치.

    상단 좌측 코너(0.0) → 상단(→) → 우측(↓) → 하단(←) → 좌측(↑) → (1.0)
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

    full_perimeter = 2.0 * (screen_w + screen_h)
    if full_perimeter <= 0:
        return np.linspace(0, 1, led_count)

    clockwise_t = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        x, y = positions[i, 0], positions[i, 1]
        side = sides[i]

        if side == "top":
            dist = x
        elif side == "right":
            dist = screen_w + y
        elif side == "bottom":
            dist = screen_w + screen_h + (screen_w - x)
        elif side == "left":
            dist = 2.0 * screen_w + screen_h + (screen_h - y)
        else:
            dist = 0.0

        clockwise_t[i] = max(0.0, min(dist / full_perimeter, 0.9999))

    return clockwise_t


def compute_led_normalized_y(config):
    """Wave 모드용 LED 전파 위치 — 둘레 경로 기반.

    Returns:
        (n_leds,) float64 — 0(하단 중앙) ~ 1(상단 중앙)
    """
    return _compute_led_perimeter_t(config)


# ══════════════════════════════════════════════════════════════════
#  밴드 매핑
# ══════════════════════════════════════════════════════════════════

def _compute_led_band_mapping(perimeter_t, n_bands, zone_weights):
    """둘레 비율 + 대역 비율 → 각 LED의 밴드 인덱스."""
    led_count = len(perimeter_t)
    band_indices = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        remapped = _remap_t(perimeter_t[i], zone_weights)
        band_indices[i] = remapped * (n_bands - 1)

    return band_indices


# ══════════════════════════════════════════════════════════════════
#  LED 순서 + 구역 매핑
# ══════════════════════════════════════════════════════════════════

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
                mapping[i] = 0 if y <= cy else 3
            else:
                mapping[i] = 1 if y <= cy else 2

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


_SIDES = ("bottom", "left", "top", "right")


def compute_side_t_ranges(config):
    """각 면의 clockwise_t 범위를 계산."""
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

    cw_t = _compute_led_clockwise_t(config)

    side_ranges = {}
    for side in _SIDES:
        t_vals = [cw_t[i] for i in range(led_count) if sides[i] == side]
        if t_vals:
            side_ranges[side] = (min(t_vals), max(t_vals))
        else:
            side_ranges[side] = (0.0, 0.0)

    return side_ranges
