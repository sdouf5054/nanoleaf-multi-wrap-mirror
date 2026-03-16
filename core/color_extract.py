"""색상 추출 — K-means dominant color + letterbox 필터

하이브리드 구역 distinctive, 미러링 N구역 distinctive,
flowing palette, dynamic/wave screen 색에서 공통 사용.

sklearn 의존성 없이 numpy만으로 구현.

[Phase 1] 신규 파일
- extract_dominant_colors(): 픽셀 배열에서 dominant colors 추출
- extract_zone_dominant(): 구역별 dominant color (per_led_to_zone_colors 대안)
- _kmeans_numpy(): numpy-only K-means (K-means++ 초기화)
- _filter_black_pixels(): letterbox 필터

사용처:
  Phase 3 — 하이브리드/미러링 N구역 distinctive 색상
  Phase 4 — flowing palette 추출
"""

import numpy as np

# ── 상수 ──────────────────────────────────────────────────────────
BLACK_THRESHOLD_DEFAULT = 15   # max(R,G,B) 기준 — 이하는 letterbox로 간주
KMEANS_MAX_ITER = 10           # 75 LED에서 충분히 수렴
KMEANS_CONVERGE_ATOL = 1.0    # centroid 변화 < 1.0이면 수렴 판정


# ══════════════════════════════════════════════════════════════════
#  공개 API
# ══════════════════════════════════════════════════════════════════

def extract_dominant_colors(pixels, n_colors=5, black_threshold=BLACK_THRESHOLD_DEFAULT,
                            prev_centroids=None):
    """픽셀 배열에서 dominant colors 추출.

    Args:
        pixels: (N, 3) float32 — RGB 0~255
        n_colors: 추출할 색 수
        black_threshold: max(R,G,B)가 이 값 이하인 픽셀은 무시 (letterbox 필터)
        prev_centroids: (n_colors, 3) float32 또는 None — warm start용.
            이전 프레임의 centroid를 전달하면 K-means 초기화에 사용하여
            프레임 간 색상 순서가 안정적으로 유지됨 (Phase 4 flowing에서 활용).

    Returns:
        colors: (n_colors, 3) float32 — RGB, 면적 내림차순 정렬
        ratios: (n_colors,) float32 — 각 색의 면적 비율 (합≈1.0)

    엣지케이스:
        - pixels가 비어있거나 n_colors 이하 → 입력 그대로 반환 + 균등 비율
        - letterbox 필터 후 유효 픽셀 부족 → threshold 무시하고 전체 사용
        - 단색 화면 → 모든 클러스터가 같은 색 (정상 동작)
    """
    pixels = np.asarray(pixels, dtype=np.float32)

    if pixels.ndim != 2 or pixels.shape[1] != 3:
        # 잘못된 입력 → 안전한 기본값
        fallback = np.full((n_colors, 3), 128.0, dtype=np.float32)
        return fallback, np.full(n_colors, 1.0 / n_colors, dtype=np.float32)

    # 1. letterbox 필터
    valid = _filter_black_pixels(pixels, black_threshold)

    # 유효 픽셀이 부족하면 전체 사용 (완전 검은 화면 등)
    if len(valid) < max(n_colors, 3):
        valid = pixels

    # 유효 픽셀이 n_colors 이하 → K-means 불필요
    if len(valid) <= n_colors:
        colors = np.zeros((n_colors, 3), dtype=np.float32)
        ratios = np.zeros(n_colors, dtype=np.float32)
        for i in range(min(len(valid), n_colors)):
            colors[i] = valid[i]
            ratios[i] = 1.0 / len(valid)
        # 남은 슬롯은 마지막 유효 색으로 채움
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

    # 3. 면적(count) 기준 내림차순 정렬
    order = np.argsort(-counts)
    centroids = centroids[order]
    counts = counts[order]

    # 4. 비율 계산
    total = counts.sum()
    ratios = (counts / total).astype(np.float32) if total > 0 else np.full(
        n_colors, 1.0 / n_colors, dtype=np.float32
    )

    return centroids.astype(np.float32), ratios


def extract_zone_dominant(per_led_colors, zone_map, n_zones,
                          black_threshold=BLACK_THRESHOLD_DEFAULT):
    """구역별 dominant color — per_led_to_zone_colors()의 대안.

    각 구역 내 LED들에서 letterbox를 제거한 뒤,
    K=3 K-means로 가장 큰 클러스터의 centroid를 반환.

    구역 내 LED가 1~2개뿐이면 K-means가 무의미하므로 평균으로 fallback.

    Args:
        per_led_colors: (n_leds, 3) float32 — LED별 RGB
        zone_map: (n_leds,) int32 — LED → 구역 매핑
        n_zones: 구역 수
        black_threshold: letterbox 필터 기준

    Returns:
        zone_colors: (n_zones, 3) float32 — 구역별 dominant RGB
    """
    per_led_colors = np.asarray(per_led_colors, dtype=np.float32)
    zone_map = np.asarray(zone_map, dtype=np.int32)
    zone_colors = np.zeros((n_zones, 3), dtype=np.float32)

    for zi in range(n_zones):
        mask = zone_map == zi
        if not mask.any():
            continue

        zone_pixels = per_led_colors[mask]

        # LED가 3개 미만 → K-means 불필요, 평균으로 fallback
        if len(zone_pixels) < 3:
            zone_colors[zi] = zone_pixels.mean(axis=0)
            continue

        # K=3: "주요 색", "소수 이상값(흰 UI 등)", "검은 바" 분리를 위해
        # 가장 큰 클러스터의 centroid = dominant color
        colors, ratios = extract_dominant_colors(
            zone_pixels, n_colors=3,
            black_threshold=black_threshold,
        )
        zone_colors[zi] = colors[0]  # 면적 최대 클러스터

    return zone_colors


def quantize_to_dominant(per_led_colors, n_colors=3,
                         black_threshold=BLACK_THRESHOLD_DEFAULT):
    """per-LED 색상을 dominant colors로 양자화.

    각 LED를 가장 가까운 dominant color로 매핑하여
    탁한 중간색 대신 선명한 대표색으로 대체.

    Phase 3에서 dynamic/wave의 screen 색상에 사용.

    Args:
        per_led_colors: (n_leds, 3) float32
        n_colors: 추출할 dominant color 수
        black_threshold: letterbox 필터

    Returns:
        quantized: (n_leds, 3) float32 — 양자화된 LED 색상
        colors: (n_colors, 3) float32 — 추출된 dominant colors
        ratios: (n_colors,) float32 — 면적 비율
    """
    per_led_colors = np.asarray(per_led_colors, dtype=np.float32)
    colors, ratios = extract_dominant_colors(
        per_led_colors, n_colors=n_colors,
        black_threshold=black_threshold,
    )

    # 각 LED를 가장 가까운 dominant color로 매핑
    # distances: (n_colors, n_leds)
    distances = np.array([
        np.sum((per_led_colors - c) ** 2, axis=1) for c in colors
    ])
    nearest = distances.argmin(axis=0)  # (n_leds,)
    quantized = colors[nearest]  # (n_leds, 3)

    return quantized, colors, ratios


# ══════════════════════════════════════════════════════════════════
#  내부 함수
# ══════════════════════════════════════════════════════════════════

def _filter_black_pixels(pixels, threshold=BLACK_THRESHOLD_DEFAULT):
    """밝기(max channel)가 threshold 이하인 픽셀 제거.

    letterbox(검은 바), 완전 꺼진 영역 등을 무시하기 위해 사용.

    Args:
        pixels: (N, 3) float32
        threshold: max(R,G,B) 기준

    Returns:
        valid_pixels: (M, 3) float32 — threshold 초과 픽셀만
    """
    if len(pixels) == 0:
        return pixels

    brightness = pixels.max(axis=1)
    valid_mask = brightness > threshold
    valid = pixels[valid_mask]

    return valid


def _kmeans_numpy(pixels, k, max_iter=KMEANS_MAX_ITER,
                  prev_centroids=None):
    """numpy-only K-means — K-means++ 초기화 또는 warm start.

    75개 이하 픽셀에 최적화. sklearn 불필요.
    K-means++ 초기화로 빈 클러스터 확률 최소화.

    Args:
        pixels: (N, 3) float32
        k: 클러스터 수
        max_iter: 최대 반복 횟수
        prev_centroids: (k, 3) float32 또는 None
            None → K-means++ 초기화
            not None → warm start (이전 centroid에서 시작)

    Returns:
        centroids: (k, 3) float32
        labels: (N,) int32
        counts: (k,) float64 — 각 클러스터의 픽셀 수
    """
    n = len(pixels)

    # 픽셀이 k 이하 → trivial case
    if n <= k:
        centroids = np.zeros((k, 3), dtype=np.float32)
        centroids[:n] = pixels[:n]
        # 남은 centroid는 마지막 픽셀로 채움
        if n > 0:
            for i in range(n, k):
                centroids[i] = pixels[-1]
        labels = np.zeros(n, dtype=np.int32)
        for i in range(n):
            labels[i] = i
        counts = np.ones(k, dtype=np.float64)
        return centroids, labels, counts

    # 초기 centroid 결정
    if prev_centroids is not None and prev_centroids.shape == (k, 3):
        centroids = prev_centroids.copy().astype(np.float32)
    else:
        centroids = _kmeans_pp_init(pixels, k)

    labels = np.zeros(n, dtype=np.int32)
    counts = np.zeros(k, dtype=np.float64)

    for iteration in range(max_iter):
        # ── Assign: 각 픽셀을 가장 가까운 centroid에 배정 ──
        # dists: (k, N) — 각 centroid까지의 거리 제곱
        dists = np.array([
            np.sum((pixels - c) ** 2, axis=1) for c in centroids
        ])
        labels = dists.argmin(axis=0).astype(np.int32)

        # ── Update: 클러스터별 평균으로 centroid 갱신 ──
        new_centroids = np.zeros_like(centroids)
        counts = np.zeros(k, dtype=np.float64)

        for ki in range(k):
            mask = labels == ki
            count = mask.sum()
            if count > 0:
                new_centroids[ki] = pixels[mask].mean(axis=0)
                counts[ki] = count
            else:
                # 빈 클러스터 → 이전 centroid 유지
                new_centroids[ki] = centroids[ki]

        # ── 수렴 판정 ──
        if np.allclose(centroids, new_centroids, atol=KMEANS_CONVERGE_ATOL):
            centroids = new_centroids
            break

        centroids = new_centroids

    return centroids, labels, counts


def _kmeans_pp_init(pixels, k):
    """K-means++ 초기화 — 분산을 최대화하는 초기 centroid 선택.

    Args:
        pixels: (N, 3) float32
        k: 클러스터 수

    Returns:
        centroids: (k, 3) float32
    """
    n = len(pixels)
    centroids = np.zeros((k, 3), dtype=np.float32)

    # 첫 centroid: 랜덤
    idx = np.random.randint(n)
    centroids[0] = pixels[idx]

    for ci in range(1, k):
        # 각 픽셀에서 가장 가까운 기존 centroid까지의 거리 제곱
        dists = np.array([
            np.sum((pixels - centroids[j]) ** 2, axis=1)
            for j in range(ci)
        ])
        min_dists = dists.min(axis=0)  # (N,)

        # 거리에 비례한 확률로 다음 centroid 선택
        total = min_dists.sum()
        if total > 0:
            probs = min_dists / total
            idx = np.random.choice(n, p=probs)
        else:
            # 모든 픽셀이 같은 색 → 랜덤 선택
            idx = np.random.randint(n)

        centroids[ci] = pixels[idx]

    return centroids
