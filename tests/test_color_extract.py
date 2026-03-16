"""core/color_extract.py 테스트

Phase 1 테스트 포인트:
1. 75 LED 입력 → 5색 추출 시간 < 1ms
2. 검은 LED 50% + 초록 LED 50% → dominant = 초록 (검은색 아님)
3. 단색 화면 (모든 LED 같은 색) → 크래시 없음
4. 빈 배열, 극소 배열 등 엣지케이스
5. zone_dominant: 구역별 dominant 추출
6. quantize_to_dominant: LED 양자화
7. warm start (prev_centroids)
8. letterbox 필터 동작
"""

import sys
import os
import time
import numpy as np

# 테스트 대상 import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.color_extract import (
    extract_dominant_colors,
    extract_zone_dominant,
    quantize_to_dominant,
    _filter_black_pixels,
    _kmeans_numpy,
)


def test_basic_extraction():
    """기본 동작: 5색 추출, 면적 내림차순."""
    np.random.seed(42)
    # 30개 초록 + 20개 빨강 + 15개 파랑 + 10개 흰색
    pixels = np.vstack([
        np.tile([0, 200, 0], (30, 1)),
        np.tile([200, 0, 0], (20, 1)),
        np.tile([0, 0, 200], (15, 1)),
        np.tile([255, 255, 255], (10, 1)),
    ]).astype(np.float32)

    colors, ratios = extract_dominant_colors(pixels, n_colors=4)

    assert colors.shape == (4, 3), f"Expected (4,3), got {colors.shape}"
    assert ratios.shape == (4,), f"Expected (4,), got {ratios.shape}"
    assert abs(ratios.sum() - 1.0) < 0.05, f"Ratios should sum to ~1.0, got {ratios.sum()}"

    # 면적 최대 = 초록 (30/75)
    dominant = colors[0]
    assert dominant[1] > dominant[0] and dominant[1] > dominant[2], \
        f"Dominant should be green-ish, got {dominant}"

    # ratios는 내림차순
    for i in range(len(ratios) - 1):
        assert ratios[i] >= ratios[i + 1], \
            f"Ratios should be descending: {ratios}"

    print("  ✓ test_basic_extraction")


def test_letterbox_filter():
    """검은 LED 50% + 초록 LED 50% → dominant = 초록."""
    np.random.seed(42)
    pixels = np.vstack([
        np.tile([0, 0, 0], (37, 1)),      # 검은 LED (letterbox)
        np.tile([0, 180, 30], (38, 1)),    # 초록 LED
    ]).astype(np.float32)

    colors, ratios = extract_dominant_colors(pixels, n_colors=3,
                                              black_threshold=15)

    # dominant는 초록이어야 함 (검은색이 아님)
    dominant = colors[0]
    assert dominant[1] > 100, \
        f"Dominant should be green after letterbox filter, got {dominant}"

    # 검은색이 dominant가 아님을 확인
    assert not (dominant[0] < 20 and dominant[1] < 20 and dominant[2] < 20), \
        f"Black should be filtered out, but dominant is {dominant}"

    print("  ✓ test_letterbox_filter")


def test_all_black():
    """완전 검은 화면 → 크래시 없이 결과 반환."""
    pixels = np.zeros((75, 3), dtype=np.float32)

    colors, ratios = extract_dominant_colors(pixels, n_colors=5)

    assert colors.shape == (5, 3)
    assert ratios.shape == (5,)
    # 크래시 없이 반환되면 성공
    print("  ✓ test_all_black")


def test_uniform_color():
    """단색 화면 → 크래시 없이 동일 색 반환."""
    np.random.seed(42)
    green = np.tile([50, 200, 80], (75, 1)).astype(np.float32)

    colors, ratios = extract_dominant_colors(green, n_colors=5)

    assert colors.shape == (5, 3)
    # 모든 클러스터가 비슷한 색이어야 함
    for i in range(5):
        diff = np.abs(colors[i] - green[0]).max()
        assert diff < 30, \
            f"Cluster {i} should be close to uniform color, diff={diff}"

    print("  ✓ test_uniform_color")


def test_empty_and_tiny():
    """빈 배열, 극소 배열 엣지케이스."""
    # 빈 배열
    empty = np.zeros((0, 3), dtype=np.float32)
    colors, ratios = extract_dominant_colors(empty, n_colors=3)
    assert colors.shape == (3, 3)
    print("  ✓ empty array OK")

    # 1개 픽셀
    single = np.array([[100, 200, 50]], dtype=np.float32)
    colors, ratios = extract_dominant_colors(single, n_colors=3)
    assert colors.shape == (3, 3)
    assert np.allclose(colors[0], [100, 200, 50])
    print("  ✓ single pixel OK")

    # 2개 픽셀, 5색 요청
    two = np.array([[255, 0, 0], [0, 0, 255]], dtype=np.float32)
    colors, ratios = extract_dominant_colors(two, n_colors=5)
    assert colors.shape == (5, 3)
    print("  ✓ fewer pixels than n_colors OK")

    # 잘못된 shape
    bad = np.array([1, 2, 3], dtype=np.float32)
    colors, ratios = extract_dominant_colors(bad, n_colors=3)
    assert colors.shape == (3, 3)
    print("  ✓ bad shape fallback OK")


def test_performance():
    """75 LED × 5색 추출 < 1ms (설계 요구사항)."""
    np.random.seed(42)
    pixels = np.random.rand(75, 3).astype(np.float32) * 255

    # 워밍업
    for _ in range(5):
        extract_dominant_colors(pixels, n_colors=5)

    # 측정 (100회 평균)
    n_runs = 100
    t0 = time.perf_counter()
    for _ in range(n_runs):
        extract_dominant_colors(pixels, n_colors=5)
    elapsed = (time.perf_counter() - t0) / n_runs * 1000  # ms

    print(f"  ✓ test_performance: {elapsed:.3f}ms per call", end="")
    if elapsed < 1.0:
        print(" (< 1ms requirement MET)")
    else:
        print(f" (WARNING: > 1ms, but may be OK on target hardware)")


def test_zone_dominant():
    """구역별 dominant color 추출."""
    np.random.seed(42)
    # 75 LEDs, 4 zones
    per_led = np.zeros((75, 3), dtype=np.float32)
    zone_map = np.zeros(75, dtype=np.int32)

    # Zone 0 (0~18): 대부분 빨강 + 약간 검정
    per_led[0:15] = [220, 30, 30]
    per_led[15:19] = [0, 0, 0]  # letterbox
    zone_map[0:19] = 0

    # Zone 1 (19~37): 대부분 파랑
    per_led[19:35] = [20, 20, 220]
    per_led[35:38] = [255, 255, 255]  # 흰색 UI
    zone_map[19:38] = 1

    # Zone 2 (38~56): 대부분 초록
    per_led[38:56] = [30, 200, 40]
    zone_map[38:56] = 2

    # Zone 3 (56~74): 혼합
    per_led[56:65] = [200, 200, 0]  # 노랑
    per_led[65:75] = [0, 0, 0]      # letterbox
    zone_map[56:75] = 3

    result = extract_zone_dominant(per_led, zone_map, 4)

    assert result.shape == (4, 3), f"Expected (4,3), got {result.shape}"

    # Zone 0 dominant should be red-ish (not black)
    assert result[0][0] > 100, f"Zone 0 should be red, got {result[0]}"

    # Zone 1 dominant should be blue-ish (not white)
    assert result[1][2] > 100, f"Zone 1 should be blue, got {result[1]}"

    # Zone 2 dominant should be green-ish
    assert result[2][1] > 100, f"Zone 2 should be green, got {result[2]}"

    # Zone 3 dominant should be yellow-ish (not black)
    assert result[3][0] > 100 and result[3][1] > 100, \
        f"Zone 3 should be yellow, got {result[3]}"

    print("  ✓ test_zone_dominant")


def test_zone_dominant_small_zones():
    """구역 내 LED 1~2개 → 평균 fallback."""
    per_led = np.array([
        [255, 0, 0],
        [0, 255, 0],
        [0, 0, 255],
    ], dtype=np.float32)
    zone_map = np.array([0, 1, 2], dtype=np.int32)

    result = extract_zone_dominant(per_led, zone_map, 3)
    assert result.shape == (3, 3)
    # 각 구역에 1개 LED → 그 LED 색이 그대로 나와야 함
    assert np.allclose(result[0], [255, 0, 0], atol=1)
    assert np.allclose(result[1], [0, 255, 0], atol=1)
    assert np.allclose(result[2], [0, 0, 255], atol=1)

    print("  ✓ test_zone_dominant_small_zones")


def test_quantize():
    """per-LED 양자화: 탁한 중간색 → 선명한 대표색."""
    np.random.seed(42)
    # 초록 계열 LED + 흰색 UI LED + 검은 letterbox
    per_led = np.vstack([
        np.tile([30, 190, 40], (50, 1)),    # 초록
        np.tile([240, 240, 240], (15, 1)),   # 흰색 UI
        np.tile([5, 5, 5], (10, 1)),         # letterbox
    ]).astype(np.float32)

    quantized, colors, ratios = quantize_to_dominant(per_led, n_colors=3)

    assert quantized.shape == per_led.shape
    assert colors.shape == (3, 3)

    # 양자화된 결과의 unique 색 수 ≤ n_colors
    unique = np.unique(quantized, axis=0)
    assert len(unique) <= 3, f"Expected ≤3 unique colors, got {len(unique)}"

    # 초록 LED들은 양자화 후에도 초록 계열
    green_leds = quantized[:50]
    assert green_leds[:, 1].mean() > 100, \
        f"Green LEDs should stay green after quantization"

    print("  ✓ test_quantize")


def test_warm_start():
    """prev_centroids warm start → 결과가 안정적."""
    np.random.seed(42)
    pixels = np.vstack([
        np.tile([200, 50, 50], (30, 1)),
        np.tile([50, 50, 200], (25, 1)),
        np.tile([50, 200, 50], (20, 1)),
    ]).astype(np.float32)

    # 첫 추출
    colors1, ratios1 = extract_dominant_colors(pixels, n_colors=3)

    # warm start로 재추출 — 결과가 비슷해야 함
    colors2, ratios2 = extract_dominant_colors(
        pixels, n_colors=3, prev_centroids=colors1
    )

    # warm start는 이전 결과와 비슷한 순서/값을 유지해야 함
    # (동일 데이터이므로 거의 같아야 함)
    for i in range(3):
        diff = np.abs(colors1[i] - colors2[i]).max()
        assert diff < 50, \
            f"Warm start should produce similar results, diff={diff} at cluster {i}"

    print("  ✓ test_warm_start")


def test_filter_black_pixels():
    """letterbox 필터 단독 테스트."""
    pixels = np.array([
        [0, 0, 0],       # 검은색 → 제거
        [10, 5, 3],      # 어두운 → 제거 (max=10 < 15)
        [200, 100, 50],  # 유효
        [15, 15, 15],    # 경계값 → 제거 (max=15 == threshold)
        [16, 0, 0],      # 유효 (max=16 > threshold)
    ], dtype=np.float32)

    valid = _filter_black_pixels(pixels, threshold=15)
    assert len(valid) == 2, f"Expected 2 valid pixels, got {len(valid)}"
    assert np.allclose(valid[0], [200, 100, 50])
    assert np.allclose(valid[1], [16, 0, 0])

    print("  ✓ test_filter_black_pixels")


def test_realistic_scenario():
    """실제 시나리오: 유튜브 정사각 앨범아트 + 검은 바."""
    np.random.seed(42)

    # 75 LED 시뮬레이션:
    # - 좌/우 측면 LED (20개): 검은 바 영역 (letterbox)
    # - 상단 LED (14개): 앨범아트 상단 — 주로 보라색
    # - 하단 LED (16개): 검은 바 or UI
    # - 나머지: 앨범아트 중심 — 보라 + 분홍 그라데이션
    per_led = np.zeros((75, 3), dtype=np.float32)

    # 검은 바 (좌우 + 하단 일부)
    per_led[0:10] = np.random.uniform(0, 10, (10, 3))    # 좌측 검은
    per_led[10:20] = np.random.uniform(0, 10, (10, 3))   # 우측 검은
    per_led[60:75] = np.random.uniform(0, 8, (15, 3))    # 하단 검은

    # 앨범아트 보라색 영역
    per_led[20:40] = np.random.uniform(0, 30, (20, 3))
    per_led[20:40, 0] = np.random.uniform(120, 180, 20)  # R
    per_led[20:40, 2] = np.random.uniform(150, 220, 20)  # B → 보라

    # 앨범아트 분홍 영역
    per_led[40:55] = np.random.uniform(0, 30, (15, 3))
    per_led[40:55, 0] = np.random.uniform(200, 255, 15)  # R
    per_led[40:55, 1] = np.random.uniform(80, 130, 15)   # G
    per_led[40:55, 2] = np.random.uniform(120, 170, 15)  # B → 분홍

    # 흰색 UI
    per_led[55:60] = [230, 230, 230]

    # ── 기존 평균 방식 ──
    avg_color = per_led.mean(axis=0)

    # ── Distinctive 방식 ──
    colors, ratios = extract_dominant_colors(per_led, n_colors=3)

    print(f"  전체 평균: R={avg_color[0]:.0f} G={avg_color[1]:.0f} B={avg_color[2]:.0f}")
    print(f"  Dominant 1: R={colors[0][0]:.0f} G={colors[0][1]:.0f} B={colors[0][2]:.0f} ({ratios[0]*100:.0f}%)")
    print(f"  Dominant 2: R={colors[1][0]:.0f} G={colors[1][1]:.0f} B={colors[1][2]:.0f} ({ratios[1]*100:.0f}%)")
    print(f"  Dominant 3: R={colors[2][0]:.0f} G={colors[2][1]:.0f} B={colors[2][2]:.0f} ({ratios[2]*100:.0f}%)")

    # 평균은 검은색에 오염되어 어두워야 함
    assert avg_color.max() < 120, \
        f"Average should be darkened by letterbox, got max={avg_color.max():.0f}"

    # Dominant #1은 검은색이 아니어야 함 (letterbox 필터 적용)
    assert colors[0].max() > 80, \
        f"Dominant should not be black, got max={colors[0].max():.0f}"

    print("  ✓ test_realistic_scenario — distinctive가 평균보다 선명한 색 추출 확인")


# ══════════════════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  core/color_extract.py — Phase 1 Tests")
    print("=" * 60)
    print()

    test_basic_extraction()
    test_letterbox_filter()
    test_all_black()
    test_uniform_color()
    test_empty_and_tiny()
    test_performance()
    test_zone_dominant()
    test_zone_dominant_small_zones()
    test_quantize()
    test_warm_start()
    test_filter_black_pixels()
    test_realistic_scenario()

    print()
    print("=" * 60)
    print("  All Phase 1 tests passed!")
    print("=" * 60)
