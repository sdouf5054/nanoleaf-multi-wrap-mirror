"""Grid-level 채도 우선순위 추출 (v2)

weight_matrix 이전 단계인 64×32 raw grid에서 고채도 픽셀을 찾아
해당 위치의 LED에 직접 보강하는 모듈.

문제:
  weight_matrix @ grid_flat → per_led_colors 에서 가중 평균 때문에
  채도가 크게 떨어짐 (808s 빨간 하트: sat 0.84 → 0.15)
  
해결:
  grid에서 고채도 픽셀을 먼저 찾고, weight_matrix의 같은 패턴으로
  고채도 픽셀만 골라 LED에 매핑 → 기존 per_led_colors와 블렌딩

사용처:
  engine_hybrid_mode.py — _run_loop() 화면 색상 계산
  engine_mirror.py      — _compute_zone_colors()

성능: ~0.2ms per frame (75 LED × 2048 grid)
"""

import numpy as np


# ── 상수 ────────────────────────────────────────────────────────
VIVID_SAT_THRESHOLD = 0.30    # 그리드에서 "고채도"로 간주하는 최소 채도
VIVID_MIN_BRIGHTNESS = 20.0   # max(R,G,B) — letterbox 필터
VIVID_TARGET_SAT = 0.70       # 채도 증폭 목표
VIVID_DEFAULT_BLEND = 0.40    # per_led_colors와의 기본 블렌딩 강도 (방법 A: 위치 기반)
VIVID_REGION_TOP_PCT = 0.10   # weight_matrix 상위 10% = LED 핵심 영역

# 방법 B: 전역 분위기 색 상수
AMBIENT_BLEND = 0.20           # 분위기 색 블렌딩 강도 (전 LED에 약하게)
AMBIENT_SAT_THRESHOLD = 0.35   # 분위기 색 추출용 채도 기준 (A보다 약간 높게)
AMBIENT_MIN_AREA_PCT = 0.02    # 최소 면적 비율 — 이 이상 차지해야 분위기 색으로 인정


# ══════════════════════════════════════════════════════════════════
#  공개 API
# ══════════════════════════════════════════════════════════════════

def build_led_region_masks(weight_matrix, top_pct=VIVID_REGION_TOP_PCT):
    """weight_matrix에서 각 LED의 핵심 영향 영역 추출.

    weight_matrix[i]의 상위 top_pct 가중치만 True로 마스킹.
    엔진 초기화 시 한 번만 호출하고 결과를 캐시.

    Args:
        weight_matrix: (n_leds, n_grid) float — LED별 그리드 가중치
        top_pct: float 0~1 — 상위 몇 %를 핵심 영역으로 볼 것인지

    Returns:
        region_masks: (n_leds, n_grid) bool — LED별 핵심 영역 마스크
    """
    n_leds, n_grid = weight_matrix.shape
    masks = np.zeros((n_leds, n_grid), dtype=bool)

    for i in range(n_leds):
        row = weight_matrix[i]
        if row.max() <= 0:
            continue

        # 상위 top_pct에 해당하는 threshold 계산
        # — weight_matrix는 대부분 0에 가까운 sparse 분포이므로
        #   nonzero 값 중에서 상위를 잡아야 함
        nonzero_mask = row > 0
        if not nonzero_mask.any():
            continue

        nonzero_vals = row[nonzero_mask]
        threshold = np.percentile(nonzero_vals, (1.0 - top_pct) * 100)
        masks[i] = row >= threshold

    return masks


def boost_per_led_vivid(grid_flat, weight_matrix, per_led_colors,
                        region_masks=None,
                        sat_threshold=VIVID_SAT_THRESHOLD,
                        min_brightness=VIVID_MIN_BRIGHTNESS,
                        target_sat=VIVID_TARGET_SAT,
                        blend=VIVID_DEFAULT_BLEND,
                        ambient_blend=AMBIENT_BLEND,
                        prev_ambient_color=None):
    """grid에서 고채도 색을 추출하여 per_led_colors를 보강.

    두 가지 보강을 동시에 적용:

    방법 A (위치 기반, blend):
      각 LED의 weight_matrix 영향 영역에서 고채도 픽셀만 추출하여 해당 LED에 보강.
      사이드바 썸네일, 가장자리 UI 요소 등을 잘 잡음.

    방법 B (전역 분위기, ambient_blend):
      LED 위치와 무관하게 전체 grid에서 고채도 대표색을 추출하여 전 LED에 약하게 깔기.
      화면 중앙의 앨범아트, 큰 이미지 등 어떤 LED에서도 멀지만 눈에 띄는 요소를 반영.
      prev_ambient_color를 전달하면 적응형 EMA로 부드럽게 전환됨.

    Args:
        grid_flat: (n_grid, 3) float32 — raw 캡처 그리드 (reshape된 상태)
        weight_matrix: (n_leds, n_grid) float — LED별 그리드 가중치
        per_led_colors: (n_leds, 3) float32 — weight_matrix 가중 평균 결과 (in-place 수정됨)
        region_masks: (n_leds, n_grid) bool 또는 None
        sat_threshold: float — 위치 기반 "고채도" 기준
        min_brightness: float — letterbox 필터 기준
        target_sat: float — 채도 증폭 목표
        blend: float 0~1 — 위치 기반 블렌딩 강도 (방법 A)
        ambient_blend: float 0~1 — 전역 분위기 블렌딩 강도 (방법 B)
        prev_ambient_color: (3,) float32 또는 None — 이전 프레임 분위기 색.
            전달 시 적응형 EMA로 급변 방지. None이면 스무딩 없이 즉시 반영.

    Returns:
        per_led_colors: (n_leds, 3) float32 — 보강된 결과 (입력 배열 수정)
        n_boosted: int — 위치 기반으로 보강된 LED 수 (디버그용)
        ambient_color: (3,) float32 또는 None — 이번 프레임 분위기 색 (다음 호출에 전달용)
    """
    if blend <= 0 and ambient_blend <= 0:
        return per_led_colors, 0, prev_ambient_color

    grid_flat = np.asarray(grid_flat, dtype=np.float32)
    n_leds = weight_matrix.shape[0]

    # 1. 전체 그리드 채도 계산 (벡터화) — A, B 공통
    grid_max = grid_flat.max(axis=1)  # (n_grid,)
    grid_min = grid_flat.min(axis=1)  # (n_grid,)
    grid_sats = np.zeros_like(grid_max)
    nz = grid_max > 0
    grid_sats[nz] = (grid_max[nz] - grid_min[nz]) / grid_max[nz]

    # 2. 고채도 + 밝기 필터
    vivid_mask = (grid_sats >= sat_threshold) & (grid_max >= min_brightness)

    n_vivid = vivid_mask.sum()
    if n_vivid == 0:
        return per_led_colors, 0, prev_ambient_color

    # ══════════════════════════════════════════════════════════════
    #  방법 B: 전역 분위기 색 — LED 위치 무관, 전체 grid에서 추출
    # ══════════════════════════════════════════════════════════════
    current_ambient = prev_ambient_color  # 반환용

    if ambient_blend > 0:
        n_grid = len(grid_flat)
        ambient_vivid = (grid_sats >= AMBIENT_SAT_THRESHOLD) & (grid_max >= min_brightness)
        n_ambient = ambient_vivid.sum()

        # 최소 면적 기준: grid의 2% 이상이 고채도여야 분위기 색으로 인정
        if n_ambient >= max(n_grid * AMBIENT_MIN_AREA_PCT, 3):
            amb_pixels = grid_flat[ambient_vivid]    # (k, 3)
            amb_sats = grid_sats[ambient_vivid]      # (k,)

            # 채도 가중 평균
            amb_weights = amb_sats / amb_sats.sum()
            raw_ambient = (amb_pixels * amb_weights[:, np.newaxis]).sum(axis=0)

            # 채도 증폭
            raw_ambient = _amplify_single(raw_ambient, target_sat)

            # ★ 적응형 EMA: 분위기 색 급변 방지
            #   색 차이가 작으면 강하게 스무딩 (smoothing=0.7)
            #   색 차이가 크면 빠르게 전환 (smoothing=0.1)
            #   → 스크롤 한 틱으로 썸네일이 바뀌는 정도는 부드럽게,
            #     화면 전환(앨범 변경 등)은 빠르게 따라감
            if prev_ambient_color is not None:
                diff = float(np.abs(raw_ambient - prev_ambient_color).max())
                if diff < 30:
                    ema = 0.7    # 작은 변화: 강하게 스무딩
                elif diff < 80:
                    ema = 0.7 * (1.0 - (diff - 30) / 50.0)  # 점진적 감소
                else:
                    ema = 0.0    # 큰 변화: 즉시 전환

                ambient_color = (
                    prev_ambient_color * ema + raw_ambient * (1.0 - ema)
                )
            else:
                ambient_color = raw_ambient

            current_ambient = ambient_color.copy()

            # 전 LED에 약하게 블렌딩
            per_led_colors[:] = np.clip(
                per_led_colors * (1.0 - ambient_blend)
                + ambient_color[np.newaxis, :] * ambient_blend,
                0, 255,
            )

    # ══════════════════════════════════════════════════════════════
    #  방법 A: 위치 기반 — 각 LED 영향 영역 내 고채도 픽셀
    # ══════════════════════════════════════════════════════════════
    n_boosted = 0

    if blend > 0:
        for led_i in range(n_leds):
            # LED의 영향 영역 내 고채도 픽셀 찾기
            if region_masks is not None:
                led_region = region_masks[led_i]  # (n_grid,) bool
            else:
                led_region = weight_matrix[led_i] > 0  # fallback

            # 영향 영역 ∩ 고채도 마스크
            local_vivid = vivid_mask & led_region
            if not local_vivid.any():
                continue

            # 해당 영역의 고채도 픽셀 추출
            local_indices = np.where(local_vivid)[0]
            local_pixels = grid_flat[local_indices]    # (k, 3)
            local_sats = grid_sats[local_indices]      # (k,)

            # weight_matrix 가중치도 반영 (원래 위치의 중요도)
            local_weights = weight_matrix[led_i, local_indices]  # (k,)

            # 복합 가중치: 채도 × weight_matrix 가중치
            combined_w = local_sats * local_weights
            w_sum = combined_w.sum()
            if w_sum <= 0:
                continue

            combined_w_norm = combined_w / w_sum

            # 채도 가중 평균
            vivid_color = (local_pixels * combined_w_norm[:, np.newaxis]).sum(axis=0)

            # 채도 증폭
            vivid_color = _amplify_single(vivid_color, target_sat)

            # 블렌딩 (ambient 위에 추가 적용)
            per_led_colors[led_i] = np.clip(
                per_led_colors[led_i] * (1.0 - blend) + vivid_color * blend,
                0, 255,
            )
            n_boosted += 1

    return per_led_colors, n_boosted, current_ambient


def smooth_per_led(per_led_colors, prev_per_led, smoothing=0.5):
    """per_led_colors에 적응형 EMA 스무딩 적용.

    vivid 보강 후 구역 분할 전에 적용하면:
    - 방법 A의 프레임 간 급변을 완화
    - 구역 경계의 불연속을 자연스럽게 줄임

    적응형 EMA:
      색 차이가 작으면(스크롤 한 틱) → 강하게 스무딩
      색 차이가 크면(화면 전환) → 빠르게 따라감

    Args:
        per_led_colors: (n_leds, 3) float32 — 현재 프레임 (in-place 수정)
        prev_per_led: (n_leds, 3) float32 또는 None — 이전 프레임
        smoothing: float 0~1 — 최대 EMA 계수 (작은 변화 시)

    Returns:
        per_led_colors: (n_leds, 3) float32 — 스무딩된 결과
    """
    if prev_per_led is None or smoothing <= 0:
        return per_led_colors

    # LED별 최대 채널 차이
    diff_per_led = np.abs(per_led_colors - prev_per_led).max(axis=1)  # (n_leds,)

    # 적응형 EMA 계수: 차이 작으면 강하게 스무딩, 크면 빠르게 전환
    #   diff < 40   → ema = smoothing (강하게 — 스크롤 한 틱, 썸네일 교체)
    #   40~120      → ema 점진 감소 (중간 변화)
    #   diff > 120  → ema = 0 (화면 전환 등 큰 변화 — 즉시)
    ema = np.where(
        diff_per_led < 40,
        smoothing,
        np.where(
            diff_per_led < 120,
            smoothing * (1.0 - (diff_per_led - 40) / 80.0),
            0.0,
        ),
    )

    # 벡터화 EMA 적용
    ema_3d = ema[:, np.newaxis]  # (n_leds, 1)
    per_led_colors[:] = prev_per_led * ema_3d + per_led_colors * (1.0 - ema_3d)

    return per_led_colors


def boost_per_led_vivid_fast(grid_flat, weight_matrix, per_led_colors,
                             region_masks=None,
                             sat_threshold=VIVID_SAT_THRESHOLD,
                             min_brightness=VIVID_MIN_BRIGHTNESS,
                             target_sat=VIVID_TARGET_SAT,
                             blend=VIVID_DEFAULT_BLEND):
    """boost_per_led_vivid의 벡터화 버전 — 더 빠름.

    LED별 Python 루프 대신 행렬 연산으로 처리.
    region_masks가 있을 때 가장 효율적.

    Args/Returns: boost_per_led_vivid와 동일
    """
    if blend <= 0:
        return per_led_colors, 0

    grid_flat = np.asarray(grid_flat, dtype=np.float32)
    n_leds = weight_matrix.shape[0]

    # 1. 전체 그리드 채도
    grid_max = grid_flat.max(axis=1)
    grid_min = grid_flat.min(axis=1)
    grid_sats = np.zeros_like(grid_max)
    nz = grid_max > 0
    grid_sats[nz] = (grid_max[nz] - grid_min[nz]) / grid_max[nz]

    # 2. 고채도 마스크
    vivid_mask = (grid_sats >= sat_threshold) & (grid_max >= min_brightness)
    if not vivid_mask.any():
        return per_led_colors, 0

    # 3. weight_matrix에 채도 마스크 적용
    #    masked_weights[led_i, grid_j] = weight_matrix[led_i, j] * sat[j]  if vivid
    #                                  = 0                                  if not vivid
    # 이렇게 하면 고채도 영역의 가중 평균을 한 번에 계산 가능

    # vivid 영역의 가중치: weight × saturation (채도 높을수록 가중)
    vivid_sats_float = grid_sats.astype(np.float32)
    vivid_sats_float[~vivid_mask] = 0.0

    if region_masks is not None:
        # region_masks로 마스킹
        effective_w = weight_matrix * region_masks  # (n_leds, n_grid)
    else:
        effective_w = weight_matrix.copy()

    # 채도 가중
    effective_w = effective_w * vivid_sats_float[np.newaxis, :]  # (n_leds, n_grid)

    # 행 합
    row_sums = effective_w.sum(axis=1)  # (n_leds,)
    has_vivid = row_sums > 0

    if not has_vivid.any():
        return per_led_colors, 0

    # 고채도 LED만 처리
    active_leds = np.where(has_vivid)[0]

    # 정규화
    row_sums_safe = np.where(has_vivid, row_sums, 1.0)  # div-by-zero 방지
    normalized_w = effective_w / row_sums_safe[:, np.newaxis]  # (n_leds, n_grid)

    # 고채도 가중 평균: (n_leds, n_grid) @ (n_grid, 3) = (n_leds, 3)
    vivid_colors = normalized_w @ grid_flat  # (n_leds, 3)

    # 채도 증폭 (활성 LED만)
    for led_i in active_leds:
        vivid_colors[led_i] = _amplify_single(vivid_colors[led_i], target_sat)

    # 블렌딩 (활성 LED만)
    blend_mask = has_vivid.astype(np.float32) * blend
    per_led_colors = np.clip(
        per_led_colors * (1.0 - blend_mask[:, np.newaxis])
        + vivid_colors * blend_mask[:, np.newaxis],
        0, 255,
    )

    return per_led_colors, int(has_vivid.sum())


# ══════════════════════════════════════════════════════════════════
#  내부 함수
# ══════════════════════════════════════════════════════════════════

def _amplify_single(rgb, target_s=0.70):
    """단일 RGB 색상의 채도를 target_s까지 증폭.

    hue와 value는 유지, saturation만 올림.

    Args:
        rgb: (3,) float32 — RGB 0~255
        target_s: float — 목표 최소 채도

    Returns:
        (3,) float32 — 채도 증폭된 RGB
    """
    r, g, b = float(rgb[0]) / 255.0, float(rgb[1]) / 255.0, float(rgb[2]) / 255.0
    mx = max(r, g, b)
    mn = min(r, g, b)
    diff = mx - mn

    if diff <= 0.001 or mx <= 0.001:
        return rgb.copy()

    # 현재 HSV
    if mx == r:
        h = ((g - b) / diff) % 6.0 / 6.0
    elif mx == g:
        h = ((b - r) / diff + 2.0) / 6.0
    else:
        h = ((r - g) / diff + 4.0) / 6.0
    s = diff / mx
    v = mx

    # 이미 충분히 높으면 유지
    if s >= target_s:
        return rgb.copy()

    new_s = target_s

    # HSV → RGB
    h6 = (h % 1.0) * 6.0
    hi = int(h6)
    f = h6 - hi
    p = v * (1.0 - new_s)
    q = v * (1.0 - new_s * f)
    t = v * (1.0 - new_s * (1.0 - f))

    if hi == 0:   nr, ng, nb = v, t, p
    elif hi == 1: nr, ng, nb = q, v, p
    elif hi == 2: nr, ng, nb = p, v, t
    elif hi == 3: nr, ng, nb = p, q, v
    elif hi == 4: nr, ng, nb = t, p, v
    else:         nr, ng, nb = v, p, q

    return np.array([nr * 255.0, ng * 255.0, nb * 255.0], dtype=np.float32)