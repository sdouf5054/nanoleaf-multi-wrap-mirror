"""색상 추출 — K-means dominant color + letterbox 필터

하이브리드 구역 distinctive, 미러링 N구역 distinctive,
flowing palette, dynamic/wave screen 색에서 공통 사용.

sklearn 의존성 없이 numpy만으로 구현.

[Phase 1] 신규 파일
[Hotfix] 점멸 방지 — warm start 순서 매칭 + EMA 스무딩 지원
  - prev_centroids 전달 시 greedy matching으로 클러스터 순서 안정화
  - extract_zone_dominant()에 prev_zone_colors + smoothing 파라미터 추가
  - 채도 가중 정렬 옵션 (saturation_weight)

[Refactor] HSV 변환 함수를 core.hsv_utils로 통합
- 로컬 _saturation_of, _saturation_array, _amplify_saturation 제거
- hsv_utils.saturation_of, saturation_array, amplify_saturation 사용
"""

import numpy as np

from core.hsv_utils import (
    saturation_of as _saturation_of,
    saturation_array as _saturation_array,
    amplify_saturation as _amplify_saturation,
)

# ── 상수 ──────────────────────────────────────────────────────────
BLACK_THRESHOLD_DEFAULT = 15   # max(R,G,B) 기준 — 이하는 letterbox로 간주
KMEANS_MAX_ITER = 10           # 75 LED에서 충분히 수렴
KMEANS_CONVERGE_ATOL = 1.0    # centroid 변화 < 1.0이면 수렴 판정


# ══════════════════════════════════════════════════════════════════
#  공개 API
# ══════════════════════════════════════════════════════════════════

def extract_dominant_colors(pixels, n_colors=5, black_threshold=BLACK_THRESHOLD_DEFAULT,
                            prev_centroids=None, saturation_weight=0.0):
    """픽셀 배열에서 dominant colors 추출.

    Args:
        pixels: (N, 3) float32 — RGB 0~255
        n_colors: 추출할 색 수
        black_threshold: max(R,G,B)가 이 값 이하인 픽셀은 무시
        prev_centroids: (n_colors, 3) float32 또는 None — warm start용.
        saturation_weight: float 0~1 — 정렬 시 채도 가중치.

    Returns:
        colors: (n_colors, 3) float32 — RGB, 스코어 내림차순
        ratios: (n_colors,) float32 — 각 색의 면적 비율 (합≈1.0)
    """
    pixels = np.asarray(pixels, dtype=np.float32)

    if pixels.ndim != 2 or pixels.shape[1] != 3:
        fallback = np.full((n_colors, 3), 128.0, dtype=np.float32)
        return fallback, np.full(n_colors, 1.0 / n_colors, dtype=np.float32)

    # 1. letterbox 필터
    valid = _filter_black_pixels(pixels, black_threshold)

    if len(valid) < max(n_colors, 3):
        valid = pixels

    if len(valid) <= n_colors:
        colors = np.zeros((n_colors, 3), dtype=np.float32)
        ratios = np.zeros(n_colors, dtype=np.float32)
        for i in range(min(len(valid), n_colors)):
            colors[i] = valid[i]
            ratios[i] = 1.0 / len(valid)
        if len(valid) > 0:
            for i in range(len(valid), n_colors):
                colors[i] = valid[-1]
        return colors, ratios

    # 2. K-means
    centroids, labels, counts = _kmeans_numpy(
        valid, n_colors,
        max_iter=KMEANS_MAX_ITER,
        prev_centroids=prev_centroids,
    )

    # 3. 정렬
    if prev_centroids is not None and prev_centroids.shape == (n_colors, 3):
        # ★ warm start: 이전 centroid와 가장 가까운 순서로 매칭
        order = _match_to_previous(centroids, prev_centroids)
    else:
        # 스코어 기반 (면적 + 선택적 채도 가중)
        order = _score_order(centroids, counts, saturation_weight)

    centroids = centroids[order]
    counts = counts[order]

    # 4. 비율 계산
    total = counts.sum()
    ratios = (counts / total).astype(np.float32) if total > 0 else np.full(
        n_colors, 1.0 / n_colors, dtype=np.float32
    )

    return centroids.astype(np.float32), ratios


def extract_zone_dominant(per_led_colors, zone_map, n_zones,
                          black_threshold=BLACK_THRESHOLD_DEFAULT,
                          prev_zone_colors=None, smoothing=0.0,
                          saturation_boost=0.0):
    """구역별 dominant color — per_led_to_zone_colors()의 대안.

    [Hotfix v4] 결정론적 median 기반 + 채도 우선순위.
    """
    per_led_colors = np.asarray(per_led_colors, dtype=np.float32)
    zone_map = np.asarray(zone_map, dtype=np.int32)
    zone_colors = np.zeros((n_zones, 3), dtype=np.float32)

    for zi in range(n_zones):
        mask = zone_map == zi
        if not mask.any():
            continue

        zone_pixels = per_led_colors[mask]

        # letterbox 필터
        valid = _filter_black_pixels(zone_pixels, black_threshold)
        if len(valid) < 1:
            valid = zone_pixels
        if len(valid) == 0:
            continue

        # 1. median (면적 기반 대표색)
        median_color = np.median(valid, axis=0)

        # 2. 채도 부스트
        if saturation_boost > 0 and len(valid) >= 3:
            sats = _saturation_array(valid)
            median_sat = float(np.median(sats))
            max_sat = float(sats.max())

            if max_sat > median_sat + 0.08 and max_sat > 0.10:
                high_threshold = median_sat + 0.05
                high_sat_mask = sats >= high_threshold

                if high_sat_mask.sum() >= 1:
                    high_pixels = valid[high_sat_mask]
                    high_sats = sats[high_sat_mask]

                    # ★ 채도 증폭: HSV의 S를 끌어올려서 선명하게 복원
                    amplified = _amplify_saturation(high_pixels, high_sats)

                    # 채도 가중 평균 (더 채도 높은 픽셀이 더 기여)
                    weights = high_sats / high_sats.sum()
                    high_sat_color = (amplified * weights[:, np.newaxis]).sum(axis=0)

                    zone_colors[zi] = np.clip(
                        median_color * (1.0 - saturation_boost)
                        + high_sat_color * saturation_boost,
                        0, 255,
                    )
                else:
                    zone_colors[zi] = median_color
            else:
                zone_colors[zi] = median_color
        else:
            zone_colors[zi] = median_color

    # 적응형 EMA 스무딩
    if prev_zone_colors is not None and smoothing > 0:
        prev = np.asarray(prev_zone_colors, dtype=np.float32)
        for zi in range(n_zones):
            if prev[zi].max() <= 0:
                continue

            diff = float(np.abs(zone_colors[zi] - prev[zi]).max())

            if diff < 20:
                effective = smoothing
            elif diff < 80:
                effective = smoothing * (1.0 - (diff - 20) / 60.0)
            else:
                effective = 0.0

            zone_colors[zi] = (
                prev[zi] * effective
                + zone_colors[zi] * (1.0 - effective)
            )

    return zone_colors


def quantize_to_dominant(per_led_colors, n_colors=3,
                         black_threshold=BLACK_THRESHOLD_DEFAULT,
                         prev_centroids=None):
    """per-LED 색상을 dominant colors로 양자화."""
    per_led_colors = np.asarray(per_led_colors, dtype=np.float32)
    colors, ratios = extract_dominant_colors(
        per_led_colors, n_colors=n_colors,
        black_threshold=black_threshold,
        prev_centroids=prev_centroids,
    )

    distances = np.array([
        np.sum((per_led_colors - c) ** 2, axis=1) for c in colors
    ])
    nearest = distances.argmin(axis=0)
    quantized = colors[nearest]

    return quantized, colors, ratios


# ══════════════════════════════════════════════════════════════════
#  내부 함수
# ══════════════════════════════════════════════════════════════════

def _score_order(centroids, counts, saturation_weight=0.0):
    """면적 + 선택적 채도 가중으로 정렬 순서 결정."""
    k = len(centroids)
    if saturation_weight <= 0:
        return np.argsort(-counts)

    total = counts.sum()
    if total <= 0:
        return np.arange(k)

    area_norm = counts / total
    sats = np.array([_saturation_of(centroids[i]) for i in range(k)])
    score = area_norm * (1.0 - saturation_weight) + sats * saturation_weight
    return np.argsort(-score)


def _match_to_previous(centroids, prev_centroids):
    """현재 centroid를 이전 centroid에 greedy matching."""
    k = len(centroids)
    used = set()
    order = np.full(k, -1, dtype=np.int32)

    for pi in range(k):
        best_ci = -1
        best_dist = float('inf')
        for ci in range(k):
            if ci in used:
                continue
            dist = float(np.sum((centroids[ci] - prev_centroids[pi]) ** 2))
            if dist < best_dist:
                best_dist = dist
                best_ci = ci
        if best_ci >= 0:
            order[pi] = best_ci
            used.add(best_ci)

    # 미매칭 잔여 처리
    remaining = [ci for ci in range(k) if ci not in used]
    empty_slots = [pi for pi in range(k) if order[pi] == -1]
    for pi, ci in zip(empty_slots, remaining):
        order[pi] = ci

    return order


def _filter_black_pixels(pixels, threshold=BLACK_THRESHOLD_DEFAULT):
    """밝기(max channel)가 threshold 이하인 픽셀 제거."""
    if len(pixels) == 0:
        return pixels
    brightness = pixels.max(axis=1)
    return pixels[brightness > threshold]


def _kmeans_numpy(pixels, k, max_iter=KMEANS_MAX_ITER, prev_centroids=None):
    """numpy-only K-means — K-means++ 초기화 또는 warm start."""
    n = len(pixels)

    if n <= k:
        centroids = np.zeros((k, 3), dtype=np.float32)
        centroids[:n] = pixels[:n]
        if n > 0:
            for i in range(n, k):
                centroids[i] = pixels[-1]
        labels = np.arange(min(n, k), dtype=np.int32)
        counts = np.ones(k, dtype=np.float64)
        return centroids, labels, counts

    if prev_centroids is not None and prev_centroids.shape == (k, 3):
        centroids = prev_centroids.copy().astype(np.float32)
    else:
        centroids = _kmeans_pp_init(pixels, k)

    labels = np.zeros(n, dtype=np.int32)
    counts = np.zeros(k, dtype=np.float64)

    for _ in range(max_iter):
        dists = np.array([np.sum((pixels - c) ** 2, axis=1) for c in centroids])
        labels = dists.argmin(axis=0).astype(np.int32)

        new_centroids = np.zeros_like(centroids)
        counts = np.zeros(k, dtype=np.float64)

        for ki in range(k):
            mask = labels == ki
            count = mask.sum()
            if count > 0:
                new_centroids[ki] = pixels[mask].mean(axis=0)
                counts[ki] = count
            else:
                new_centroids[ki] = centroids[ki]

        if np.allclose(centroids, new_centroids, atol=KMEANS_CONVERGE_ATOL):
            centroids = new_centroids
            break
        centroids = new_centroids

    return centroids, labels, counts


def _kmeans_pp_init(pixels, k):
    """K-means++ 초기화."""
    n = len(pixels)
    centroids = np.zeros((k, 3), dtype=np.float32)
    idx = np.random.randint(n)
    centroids[0] = pixels[idx]

    for ci in range(1, k):
        dists = np.array([np.sum((pixels - centroids[j]) ** 2, axis=1) for j in range(ci)])
        min_dists = dists.min(axis=0)
        total = min_dists.sum()
        if total > 0:
            idx = np.random.choice(n, p=min_dists / total)
        else:
            idx = np.random.randint(n)
        centroids[ci] = pixels[idx]

    return centroids
