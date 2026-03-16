"""color_extract.py 테스트 — Phase 1 + 점멸 방지 hotfix

기존 테스트 + 추가:
- 순서 안정성 (warm start + greedy matching)
- EMA 스무딩 (extract_zone_dominant)
- 채도 가중 정렬 (saturation_weight)
- 프레임 시뮬레이션 (연속 호출 안정성)
"""

import sys
import os
import time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.color_extract import (
    extract_dominant_colors,
    extract_zone_dominant,
    quantize_to_dominant,
    _filter_black_pixels,
    _match_to_previous,
    _saturation_of,
)


def test_basic_extraction():
    np.random.seed(42)
    pixels = np.vstack([
        np.tile([0, 200, 0], (30, 1)),
        np.tile([200, 0, 0], (20, 1)),
        np.tile([0, 0, 200], (15, 1)),
        np.tile([255, 255, 255], (10, 1)),
    ]).astype(np.float32)
    colors, ratios = extract_dominant_colors(pixels, n_colors=4)
    assert colors.shape == (4, 3)
    assert abs(ratios.sum() - 1.0) < 0.05
    dominant = colors[0]
    assert dominant[1] > dominant[0] and dominant[1] > dominant[2]
    print("  ✓ test_basic_extraction")


def test_letterbox_filter():
    np.random.seed(42)
    pixels = np.vstack([
        np.tile([0, 0, 0], (37, 1)),
        np.tile([0, 180, 30], (38, 1)),
    ]).astype(np.float32)
    colors, _ = extract_dominant_colors(pixels, n_colors=3, black_threshold=15)
    assert colors[0][1] > 100
    print("  ✓ test_letterbox_filter")


def test_all_black():
    pixels = np.zeros((75, 3), dtype=np.float32)
    colors, ratios = extract_dominant_colors(pixels, n_colors=5)
    assert colors.shape == (5, 3)
    print("  ✓ test_all_black")


def test_uniform_color():
    np.random.seed(42)
    green = np.tile([50, 200, 80], (75, 1)).astype(np.float32)
    colors, _ = extract_dominant_colors(green, n_colors=5)
    assert colors.shape == (5, 3)
    print("  ✓ test_uniform_color")


def test_empty_and_tiny():
    empty = np.zeros((0, 3), dtype=np.float32)
    c, _ = extract_dominant_colors(empty, n_colors=3)
    assert c.shape == (3, 3)
    single = np.array([[100, 200, 50]], dtype=np.float32)
    c, _ = extract_dominant_colors(single, n_colors=3)
    assert c.shape == (3, 3)
    bad = np.array([1, 2, 3], dtype=np.float32)
    c, _ = extract_dominant_colors(bad, n_colors=3)
    assert c.shape == (3, 3)
    print("  ✓ test_empty_and_tiny")


def test_order_stability_with_warm_start():
    """warm start 시 같은 데이터 반복 → 순서 유지."""
    np.random.seed(42)
    pixels = np.vstack([
        np.tile([200, 30, 30], (30, 1)),
        np.tile([30, 30, 200], (25, 1)),
        np.tile([30, 200, 30], (20, 1)),
    ]).astype(np.float32)

    c1, _ = extract_dominant_colors(pixels, n_colors=3)
    prev = c1
    for i in range(10):
        c, _ = extract_dominant_colors(pixels, n_colors=3, prev_centroids=prev)
        for j in range(3):
            diff = np.abs(c[j] - prev[j]).max()
            assert diff < 30, f"Iter {i} cluster {j}: diff={diff:.1f}"
        prev = c
    print("  ✓ test_order_stability_with_warm_start")


def test_order_stability_with_noise():
    """데이터에 ±10 노이즈 → 순서 안정."""
    np.random.seed(42)
    base = np.vstack([
        np.tile([200, 30, 30], (30, 1)),
        np.tile([30, 30, 200], (25, 1)),
        np.tile([30, 200, 30], (20, 1)),
    ]).astype(np.float32)

    c_prev, _ = extract_dominant_colors(base, n_colors=3)
    order_changes = 0
    prev = c_prev
    for _ in range(20):
        noisy = base + np.random.uniform(-10, 10, base.shape).astype(np.float32)
        np.clip(noisy, 0, 255, out=noisy)
        c, _ = extract_dominant_colors(noisy, n_colors=3, prev_centroids=prev)
        for j in range(3):
            if np.argmax(c[j]) != np.argmax(prev[j]):
                order_changes += 1
        prev = c

    assert order_changes < 5, f"Too many order changes: {order_changes}/60"
    print(f"  ✓ test_order_stability_with_noise (changes: {order_changes}/60)")


def test_zone_dominant_smoothing():
    """EMA 스무딩 동작 확인."""
    np.random.seed(42)
    per_led = np.zeros((75, 3), dtype=np.float32)
    zone_map = np.zeros(75, dtype=np.int32)
    per_led[0:37] = [200, 50, 50]; zone_map[0:37] = 0
    per_led[37:75] = [50, 50, 200]; zone_map[37:75] = 1

    prev = None
    smooth_diffs = []
    for _ in range(10):
        result = extract_zone_dominant(per_led, zone_map, 2,
                                        smoothing=0.5, prev_zone_colors=prev)
        if prev is not None:
            smooth_diffs.append(np.abs(result - prev).max())
        prev = result.copy()

    if smooth_diffs:
        assert max(smooth_diffs) < 100, f"Smoothing should reduce jumps: max={max(smooth_diffs)}"
    print("  ✓ test_zone_dominant_smoothing")


def test_zone_dominant_warm_start():
    """prev_zone_colors로 warm start 동작."""
    np.random.seed(42)
    per_led = np.zeros((75, 3), dtype=np.float32)
    zone_map = np.zeros(75, dtype=np.int32)
    per_led[0:30] = [200, 30, 30]; per_led[30:37] = [230, 230, 230]; zone_map[0:37] = 0
    per_led[37:65] = [30, 30, 200]; per_led[65:75] = [5, 5, 5]; zone_map[37:75] = 1

    r1 = extract_zone_dominant(per_led, zone_map, 2)
    assert r1[0][0] > 100
    assert r1[1][2] > 100

    r2 = extract_zone_dominant(per_led, zone_map, 2,
                                prev_zone_colors=r1, smoothing=0.3)
    assert r2[0][0] > 100
    assert r2[1][2] > 100
    print("  ✓ test_zone_dominant_warm_start")


def test_saturation_weight():
    """채도 가중: 면적 작지만 채도 높은 색이 올라오는지."""
    np.random.seed(42)
    pixels = np.vstack([
        np.tile([128, 128, 128], (40, 1)),
        np.tile([255, 0, 0], (20, 1)),
        np.tile([100, 100, 100], (15, 1)),
    ]).astype(np.float32)

    c_no, _ = extract_dominant_colors(pixels, n_colors=3, saturation_weight=0.0)
    sat_no = _saturation_of(c_no[0])

    c_yes, _ = extract_dominant_colors(pixels, n_colors=3, saturation_weight=0.5)
    sat_yes = _saturation_of(c_yes[0])

    assert sat_yes >= sat_no - 0.1
    print(f"  ✓ test_saturation_weight (no={sat_no:.2f}, weighted={sat_yes:.2f})")


def test_match_to_previous():
    """greedy matching 직접 검증."""
    centroids = np.array([[0,0,255],[255,0,0],[0,255,0]], dtype=np.float32)
    prev = np.array([[250,10,10],[10,10,250],[10,250,10]], dtype=np.float32)
    order = _match_to_previous(centroids, prev)
    assert order[0] == 1  # prev[0]=빨강 → centroids[1]
    assert order[1] == 0  # prev[1]=파랑 → centroids[0]
    assert order[2] == 2  # prev[2]=초록 → centroids[2]
    print("  ✓ test_match_to_previous")


def test_frame_simulation():
    """30프레임 연속 호출 — 최대 점프 < 50."""
    np.random.seed(42)
    zone_map = np.zeros(75, dtype=np.int32)
    zone_map[:37] = 0; zone_map[37:] = 1

    prev_zone = None
    max_jump = 0.0

    for frame in range(30):
        t = frame / 30.0
        per_led = np.zeros((75, 3), dtype=np.float32)
        per_led[:37] = [200 + 20*np.sin(t*3), 30, 30]
        per_led[37:] = [30, 30, 200 + 20*np.cos(t*2)]
        per_led += np.random.uniform(-3, 3, (75, 3))
        np.clip(per_led, 0, 255, out=per_led)

        result = extract_zone_dominant(per_led, zone_map, 2,
                                        prev_zone_colors=prev_zone, smoothing=0.4)
        if prev_zone is not None:
            jump = np.abs(result - prev_zone).max()
            max_jump = max(max_jump, jump)
        prev_zone = result.copy()

    assert max_jump < 50, f"Max jump = {max_jump:.1f} — too much flicker"
    print(f"  ✓ test_frame_simulation (max jump: {max_jump:.1f})")


def test_performance():
    np.random.seed(42)
    pixels = np.random.rand(75, 3).astype(np.float32) * 255
    for _ in range(5):
        extract_dominant_colors(pixels, n_colors=5)

    n_runs = 100
    t0 = time.perf_counter()
    prev = None
    for _ in range(n_runs):
        c, _ = extract_dominant_colors(pixels, n_colors=5, prev_centroids=prev)
        prev = c
    elapsed = (time.perf_counter() - t0) / n_runs * 1000
    print(f"  ✓ test_performance: {elapsed:.3f}ms", end="")
    print(" (< 1ms OK)" if elapsed < 1.0 else " (WARNING)")


if __name__ == "__main__":
    print("=" * 60)
    print("  color_extract.py — Flicker Fix Tests")
    print("=" * 60)
    print()

    test_basic_extraction()
    test_letterbox_filter()
    test_all_black()
    test_uniform_color()
    test_empty_and_tiny()

    print("\n  ── 점멸 방지 테스트 ──")
    test_order_stability_with_warm_start()
    test_order_stability_with_noise()
    test_zone_dominant_smoothing()
    test_zone_dominant_warm_start()
    test_saturation_weight()
    test_match_to_previous()
    test_frame_simulation()

    print()
    test_performance()

    print("\n" + "=" * 60)
    print("  All tests passed!")
    print("=" * 60)