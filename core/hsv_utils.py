"""HSV ↔ RGB 변환 유틸리티 — 단일 소스 (Single Source of Truth)

기존 3곳에 분산되어 있던 HSV 변환 코드를 통합합니다:
  - engine_utils.py: _rgb_to_hsv_single, _hsv_to_rgb_single,
                     _hsv_to_rgb_array, _rgb_array_to_hsv
  - flowing.py: _rgb_to_hsv, _hsv_to_rgb, _lerp_hsv
  - color_extract.py: _amplify_saturation 내부 수동 HSV 변환

제공 API:
  스칼라 (단일 색상):
    rgb_to_hsv(rgb)           → (h, s, v)
    hsv_to_rgb(h, s, v)       → (3,) float32

  벡터 (N개 색상 배열):
    rgb_array_to_hsv(rgb)     → (h, s, v) 각 (N,) float64
    hsv_to_rgb_array(h, s, v) → (N, 3) float32

  유틸:
    lerp_hsv(color_a, color_b, t) → (3,) float32  (HSV 공간 보간)
    saturation_of(rgb)             → float         (단일 색 채도)
    saturation_array(pixels)       → (N,) float    (배열 채도)
    amplify_saturation(pixels, sats, target_s) → (N, 3) float32

순수 numpy 모듈. Qt 의존성 없음.
"""

import numpy as np


# ══════════════════════════════════════════════════════════════════
#  스칼라 변환 (단일 색상)
# ══════════════════════════════════════════════════════════════════

def rgb_to_hsv(rgb):
    """(3,) RGB 0~255 → (h, s, v) 각각 0~1.

    Args:
        rgb: (3,) array-like — [R, G, B] 0~255

    Returns:
        (h, s, v) tuple of float, 각각 0~1
    """
    r, g, b = float(rgb[0]) / 255.0, float(rgb[1]) / 255.0, float(rgb[2]) / 255.0
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


def hsv_to_rgb(h, s, v):
    """스칼라 H, S, V (0~1) → (3,) RGB float32 0~255.

    Args:
        h, s, v: float, 각각 0~1

    Returns:
        np.array (3,) float32, 0~255
    """
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


# ══════════════════════════════════════════════════════════════════
#  벡터 변환 (N개 색상 배열)
# ══════════════════════════════════════════════════════════════════

def rgb_array_to_hsv(rgb):
    """(N, 3) RGB float32 0~255 → (N,) H, (N,) S, (N,) V 각각 0~1.

    Args:
        rgb: (N, 3) array-like — RGB 0~255

    Returns:
        (h, s, v) tuple of np.array (N,) float64
    """
    rgb_norm = np.asarray(rgb, dtype=np.float64) / 255.0
    r, g, b = rgb_norm[:, 0], rgb_norm[:, 1], rgb_norm[:, 2]

    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    diff = mx - mn

    h = np.zeros_like(mx)
    mask_r = (mx == r) & (diff > 0)
    mask_g = (mx == g) & (diff > 0) & ~mask_r
    mask_b = (mx == b) & (diff > 0) & ~mask_r & ~mask_g

    h[mask_r] = (((g[mask_r] - b[mask_r]) / diff[mask_r]) % 6.0) / 6.0
    h[mask_g] = ((b[mask_g] - r[mask_g]) / diff[mask_g] + 2.0) / 6.0
    h[mask_b] = ((r[mask_b] - g[mask_b]) / diff[mask_b] + 4.0) / 6.0

    # mx==0인 원소에서 0/0 경고 방지: 안전한 분모로 나눈 뒤 마스킹
    mx_safe = np.where(mx > 0, mx, 1.0)
    s = np.where(mx > 0, diff / mx_safe, 0.0)
    v = mx
    return h, s, v


def hsv_to_rgb_array(h, s, v):
    """(N,) H, S, V (0~1) → (N, 3) RGB float32 0~255. 벡터화.

    Args:
        h, s, v: (N,) array-like, 각각 0~1

    Returns:
        (N, 3) np.array float32, 0~255
    """
    h = np.asarray(h, dtype=np.float64) % 1.0
    s = np.asarray(s, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)

    h6 = h * 6.0
    i = h6.astype(np.int32) % 6
    f = h6 - np.floor(h6)
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))

    n = len(h)
    rgb = np.zeros((n, 3), dtype=np.float64)

    m0 = i == 0; m1 = i == 1; m2 = i == 2
    m3 = i == 3; m4 = i == 4; m5 = i == 5

    rgb[m0] = np.column_stack([v[m0], t[m0], p[m0]])
    rgb[m1] = np.column_stack([q[m1], v[m1], p[m1]])
    rgb[m2] = np.column_stack([p[m2], v[m2], t[m2]])
    rgb[m3] = np.column_stack([p[m3], q[m3], v[m3]])
    rgb[m4] = np.column_stack([t[m4], p[m4], v[m4]])
    rgb[m5] = np.column_stack([v[m5], p[m5], q[m5]])

    return (rgb * 255.0).astype(np.float32)


# ══════════════════════════════════════════════════════════════════
#  유틸리티
# ══════════════════════════════════════════════════════════════════

def saturation_of(rgb):
    """(3,) RGB 0~255 → 채도 0~1 (HSV의 S).

    Args:
        rgb: (3,) array-like — [R, G, B] 0~255

    Returns:
        float, 0~1
    """
    mx = max(float(rgb[0]), float(rgb[1]), float(rgb[2]))
    mn = min(float(rgb[0]), float(rgb[1]), float(rgb[2]))
    return (mx - mn) / mx if mx > 0 else 0.0


def saturation_array(pixels):
    """(N, 3) RGB 0~255 → (N,) 채도 0~1. 벡터화.

    Args:
        pixels: (N, 3) array-like — RGB 0~255

    Returns:
        (N,) np.array float
    """
    pixels = np.asarray(pixels, dtype=np.float32)
    mx = pixels.max(axis=1)
    mn = pixels.min(axis=1)
    result = np.zeros_like(mx)
    nz = mx > 0
    result[nz] = (mx[nz] - mn[nz]) / mx[nz]
    return result


def lerp_hsv(color_a, color_b, t):
    """HSV 공간에서 두 RGB 색상을 보간. hue shortest path.

    Args:
        color_a: (3,) array-like — RGB 0~255
        color_b: (3,) array-like — RGB 0~255
        t: float 0~1 — 보간 비율

    Returns:
        (3,) np.array float32 — 보간된 RGB 0~255
    """
    t = max(0.0, min(1.0, t))
    h1, s1, v1 = rgb_to_hsv(color_a)
    h2, s2, v2 = rgb_to_hsv(color_b)

    # hue shortest path
    dh = h2 - h1
    if dh > 0.5:
        dh -= 1.0
    elif dh < -0.5:
        dh += 1.0

    h = (h1 + dh * t) % 1.0
    s = s1 + (s2 - s1) * t
    v = v1 + (v2 - v1) * t
    return hsv_to_rgb(h, max(0, min(1, s)), max(0, min(1, v)))


def amplify_saturation(pixels, sats, target_s=0.85):
    """고채도 픽셀의 채도를 target_s까지 끌어올림.

    weight_matrix를 거치면서 떨어진 채도를 복원.
    hue와 value는 유지하고 saturation만 증폭.

    Args:
        pixels: (N, 3) float32 — RGB 0~255
        sats: (N,) float — 현재 채도 (0~1)
        target_s: float — 목표 최소 채도

    Returns:
        amplified: (N, 3) float32 — 채도 증폭된 RGB 0~255
    """
    result = pixels.copy()
    rgb_norm = pixels / 255.0

    for i in range(len(pixels)):
        if sats[i] < 0.01:
            continue  # 무채색은 건드리지 않음

        r, g, b = float(rgb_norm[i, 0]), float(rgb_norm[i, 1]), float(rgb_norm[i, 2])
        mx = max(r, g, b)
        mn = min(r, g, b)
        diff = mx - mn

        if diff <= 0 or mx <= 0:
            continue

        # 현재 HSV
        if mx == r:
            h = ((g - b) / diff) % 6.0 / 6.0
        elif mx == g:
            h = ((b - r) / diff + 2.0) / 6.0
        else:
            h = ((r - g) / diff + 4.0) / 6.0
        s = diff / mx
        v = mx

        # S를 target_s까지 끌어올림 (이미 높으면 유지)
        new_s = max(s, target_s)

        # HSV → RGB
        h6 = (h % 1.0) * 6.0
        hi = int(h6)
        f = h6 - hi
        p = v * (1.0 - new_s)
        q = v * (1.0 - new_s * f)
        t_val = v * (1.0 - new_s * (1.0 - f))

        if hi == 0:   nr, ng, nb = v, t_val, p
        elif hi == 1: nr, ng, nb = q, v, p
        elif hi == 2: nr, ng, nb = p, v, t_val
        elif hi == 3: nr, ng, nb = p, q, v
        elif hi == 4: nr, ng, nb = t_val, p, v
        else:         nr, ng, nb = v, p, q

        result[i] = [nr * 255.0, ng * 255.0, nb * 255.0]

    return result