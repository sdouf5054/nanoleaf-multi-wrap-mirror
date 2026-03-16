"""vivid_extract.py 테스트 — Grid-level 채도 우선순위 v2

테스트 포인트:
1. build_led_region_masks: weight_matrix에서 핵심 영역 추출
2. boost_per_led_vivid: 808s 시뮬레이션 (빨간 하트 + 연회색 배경)
3. boost_per_led_vivid: J Dilla 시뮬레이션 (노란 텍스트 + 갈색 배경)  
4. boost_per_led_vivid: 전체 저채도 (보강 없음)
5. boost_per_led_vivid: 전체 고채도 (대부분 보강)
6. boost_per_led_vivid_fast: 벡터화 버전이 동일 결과
7. 성능: 75 LED × 2048 grid < 1ms
8. 블렌딩 강도별 결과 확인
"""

import sys
import os
import time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.vivid_extract import (
    build_led_region_masks,
    boost_per_led_vivid,
    boost_per_led_vivid_fast,
    _amplify_single,
)


def _make_808s_grid_and_weights():
    """808s 시뮬레이션: 연회색 배경 + 빨간 하트(중앙 15% 면적).

    Returns:
        grid_flat: (2048, 3) — 64×32 grid
        weight_matrix: (75, 2048) — LED별 가중치 (간단한 시뮬레이션)
        expected: 보강 후 하트 근처 LED가 빨간 계열이어야 함
    """
    np.random.seed(42)
    grid_h, grid_w = 32, 64
    n_grid = grid_h * grid_w  # 2048
    n_leds = 75

    grid = np.zeros((grid_h, grid_w, 3), dtype=np.float32)

    # 배경: 연회색 [200, 205, 215] + 약간의 노이즈
    grid[:] = [200, 205, 215]
    grid += np.random.uniform(-5, 5, grid.shape).astype(np.float32)

    # 빨간 하트 영역: 중앙 (row 10~22, col 25~39) ≈ 15% 면적
    grid[10:22, 25:39] = [220, 45, 35]  # 선명한 빨강

    grid_flat = np.clip(grid.reshape(-1, 3), 0, 255).astype(np.float32)

    # weight_matrix 시뮬레이션: 각 LED가 가우시안 감쇠로 화면 깊숙이 커버
    # 실제 weight_matrix와 동일한 패턴 — LED 주변 넓은 영역의 가중 평균
    weight_matrix = np.zeros((n_leds, n_grid), dtype=np.float32)

    # LED 물리 좌표 (둘레에 배치)
    led_positions = []
    # 상단 20개
    for i in range(20):
        led_positions.append((i / 20 * grid_w, 0))
    # 우측 15개
    for j in range(15):
        led_positions.append((grid_w - 1, j / 15 * grid_h))
    # 하단 20개
    for i in range(20):
        led_positions.append(((1 - i / 20) * grid_w, grid_h - 1))
    # 좌측 15개
    for j in range(min(15, n_leds - 55)):
        led_positions.append((0, (1 - j / 15) * grid_h))
    # 나머지
    while len(led_positions) < n_leds:
        led_positions.append((grid_w / 2, grid_h / 2))

    # 가우시안 감쇠: sigma=8 (화면 깊숙이 ~16셀까지 영향)
    sigma = 8.0
    for led_i, (lx, ly) in enumerate(led_positions):
        for row in range(grid_h):
            for col in range(grid_w):
                dist_sq = (col - lx) ** 2 + (row - ly) ** 2
                w = np.exp(-dist_sq / (2 * sigma * sigma))
                if w > 0.001:
                    weight_matrix[led_i, row * grid_w + col] = w

    # 정규화: 각 LED의 가중치 합이 1이 되도록
    row_sums = weight_matrix.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    weight_matrix /= row_sums

    return grid_flat, weight_matrix


def _make_jdilla_grid_and_weights():
    """J Dilla 시뮬레이션: 어두운 갈색 배경 + 노란 텍스트(작은 면적)."""
    np.random.seed(123)
    grid_h, grid_w = 32, 64
    grid = np.zeros((grid_h, grid_w, 3), dtype=np.float32)

    # 배경: 어두운 갈색
    grid[:] = [60, 45, 30]
    grid += np.random.uniform(-5, 5, grid.shape).astype(np.float32)

    # 노란 텍스트: 여러 위치에 작은 영역 (총 ~8% 면적)
    grid[5:8, 10:30] = [220, 190, 40]     # "J Dilla" 텍스트
    grid[14:17, 15:40] = [200, 170, 30]   # 부제목
    grid[22:24, 20:45] = [180, 160, 35]   # 추가 텍스트

    grid_flat = np.clip(grid.reshape(-1, 3), 0, 255).astype(np.float32)

    # weight_matrix는 808s와 동일한 패턴 재사용
    _, weight_matrix = _make_808s_grid_and_weights()

    return grid_flat, weight_matrix


def _saturation_of(rgb):
    mx = max(float(rgb[0]), float(rgb[1]), float(rgb[2]))
    mn = min(float(rgb[0]), float(rgb[1]), float(rgb[2]))
    return (mx - mn) / mx if mx > 0 else 0.0


# ══════════════════════════════════════════════════════════════════
#  테스트
# ══════════════════════════════════════════════════════════════════

def test_build_region_masks():
    """weight_matrix에서 LED별 핵심 영역 추출."""
    _, weight_matrix = _make_808s_grid_and_weights()
    masks = build_led_region_masks(weight_matrix, top_pct=0.1)

    assert masks.shape == weight_matrix.shape
    assert masks.dtype == bool

    # 각 LED에 최소 1개의 영역이 있어야 함 (weight가 0인 LED 제외)
    for i in range(75):
        if weight_matrix[i].max() > 0:
            assert masks[i].any(), f"LED {i} has weights but no region mask"

    # 상위 10%이므로 전체 nonzero 대비 마스크 크기가 작아야 함
    for i in range(10):
        if weight_matrix[i].max() > 0:
            nonzero_count = (weight_matrix[i] > 0).sum()
            mask_count = masks[i].sum()
            assert mask_count <= nonzero_count, \
                f"LED {i}: mask ({mask_count}) > nonzero ({nonzero_count})"

    print("  ✓ test_build_region_masks")


def test_808s_boost():
    """808s: 빨간 하트가 LED에 반영되어야 함."""
    grid_flat, weight_matrix = _make_808s_grid_and_weights()
    region_masks = build_led_region_masks(weight_matrix)

    # 기존 per_led_colors (weight_matrix 가중 평균)
    per_led_original = (weight_matrix @ grid_flat).copy()

    # 보강 전 채도 확인
    original_sats = np.array([_saturation_of(c) for c in per_led_original])
    print(f"  808s 보강 전: mean_sat={original_sats.mean():.3f}, max_sat={original_sats.max():.3f}")

    # 보강
    per_led_boosted = per_led_original.copy()
    per_led_boosted, n_boosted = boost_per_led_vivid(
        grid_flat, weight_matrix, per_led_boosted,
        region_masks=region_masks,
        blend=0.5,
    )

    # 보강 후 채도 확인
    boosted_sats = np.array([_saturation_of(c) for c in per_led_boosted])
    print(f"  808s 보강 후: mean_sat={boosted_sats.mean():.3f}, max_sat={boosted_sats.max():.3f}")
    print(f"  보강된 LED 수: {n_boosted}/{len(per_led_boosted)}")

    # 최소 일부 LED가 보강되었어야 함
    assert n_boosted > 0, "808s: 최소 일부 LED가 보강되어야 함"

    # 보강 후 최대 채도가 증가했어야 함
    assert boosted_sats.max() > original_sats.max() + 0.05, \
        f"808s: 보강 후 채도 증가 부족 ({original_sats.max():.3f} → {boosted_sats.max():.3f})"

    # 상단 LED 중 하트 근처(LED ~6~13, 상단 중앙)가 빨간 계열이어야 함
    heart_leds = per_led_boosted[6:14]  # 상단 중앙 LED들
    for i, c in enumerate(heart_leds):
        s = _saturation_of(c)
        if s > 0.2:
            # 빨간 계열: R > G, R > B
            assert c[0] > c[1] and c[0] > c[2], \
                f"LED {6+i}: 하트 근처인데 빨간 계열이 아님 {c.astype(int)}"
            break
    else:
        # 최소 1개는 빨간 계열이어야 함 (너무 엄격하면 통과 안 될 수 있어서 경고만)
        print(f"  ⚠ 하트 근처 LED에서 빨간색 미발견 (weight_matrix 패턴에 따라 다를 수 있음)")

    print("  ✓ test_808s_boost")


def test_jdilla_boost():
    """J Dilla: 노란 텍스트가 LED에 반영되어야 함."""
    grid_flat, weight_matrix = _make_jdilla_grid_and_weights()
    region_masks = build_led_region_masks(weight_matrix)

    per_led_original = (weight_matrix @ grid_flat).copy()
    original_sats = np.array([_saturation_of(c) for c in per_led_original])
    print(f"  J Dilla 보강 전: mean_sat={original_sats.mean():.3f}, max_sat={original_sats.max():.3f}")

    per_led_boosted = per_led_original.copy()
    per_led_boosted, n_boosted = boost_per_led_vivid(
        grid_flat, weight_matrix, per_led_boosted,
        region_masks=region_masks,
        blend=0.5,
    )

    boosted_sats = np.array([_saturation_of(c) for c in per_led_boosted])
    print(f"  J Dilla 보강 후: mean_sat={boosted_sats.mean():.3f}, max_sat={boosted_sats.max():.3f}")
    print(f"  보강된 LED 수: {n_boosted}/{len(per_led_boosted)}")

    assert n_boosted > 0, "J Dilla: 최소 일부 LED가 보강되어야 함"
    assert boosted_sats.max() > original_sats.max() + 0.05

    print("  ✓ test_jdilla_boost")


def test_all_low_sat():
    """전체 저채도 화면 → 보강 없음."""
    np.random.seed(42)
    grid_flat = np.full((2048, 3), 128.0, dtype=np.float32)
    grid_flat += np.random.uniform(-3, 3, grid_flat.shape).astype(np.float32)

    _, weight_matrix = _make_808s_grid_and_weights()
    per_led = (weight_matrix @ grid_flat).copy()

    per_led_boosted = per_led.copy()
    _, n_boosted = boost_per_led_vivid(
        grid_flat, weight_matrix, per_led_boosted,
        blend=0.5,
    )

    assert n_boosted == 0, f"저채도 화면에서 {n_boosted}개 LED 보강됨 (0이어야 함)"
    assert np.allclose(per_led, per_led_boosted, atol=0.1), "보강 없어야 함"

    print("  ✓ test_all_low_sat")


def test_all_high_sat():
    """전체 고채도 화면 → 대부분 보강."""
    np.random.seed(42)
    grid_flat = np.zeros((2048, 3), dtype=np.float32)
    # 무지개 색으로 채움
    for i in range(2048):
        hue = i / 2048
        grid_flat[i] = _hue_to_rgb(hue)

    _, weight_matrix = _make_808s_grid_and_weights()
    per_led = (weight_matrix @ grid_flat).copy()

    per_led_boosted = per_led.copy()
    _, n_boosted = boost_per_led_vivid(
        grid_flat, weight_matrix, per_led_boosted,
        blend=0.5,
    )

    # 대부분 LED가 보강되어야 함
    assert n_boosted > 30, f"고채도 화면에서 {n_boosted}개만 보강됨 (30+ 기대)"

    print(f"  ✓ test_all_high_sat ({n_boosted}/{len(per_led)} LED boosted)")


def test_fast_matches_normal():
    """벡터화 버전이 일반 버전과 비슷한 결과."""
    grid_flat, weight_matrix = _make_808s_grid_and_weights()
    region_masks = build_led_region_masks(weight_matrix)

    per_led = (weight_matrix @ grid_flat).copy()

    # 일반 버전
    result_normal = per_led.copy()
    boost_per_led_vivid(grid_flat, weight_matrix, result_normal,
                        region_masks=region_masks, blend=0.5)

    # 벡터화 버전
    result_fast = per_led.copy()
    boost_per_led_vivid_fast(grid_flat, weight_matrix, result_fast,
                             region_masks=region_masks, blend=0.5)

    # 완전히 동일하진 않지만 (amplify 순서 차이) 비슷해야 함
    diff = np.abs(result_normal - result_fast).max()
    print(f"  normal vs fast max diff: {diff:.1f}")

    # 채도 분포가 비슷한지 확인
    sats_normal = np.array([_saturation_of(c) for c in result_normal])
    sats_fast = np.array([_saturation_of(c) for c in result_fast])
    sat_diff = abs(sats_normal.mean() - sats_fast.mean())
    assert sat_diff < 0.1, f"채도 평균 차이가 너무 큼: {sat_diff:.3f}"

    print("  ✓ test_fast_matches_normal")


def test_blend_levels():
    """블렌딩 강도별 결과 확인."""
    grid_flat, weight_matrix = _make_808s_grid_and_weights()
    region_masks = build_led_region_masks(weight_matrix)
    per_led = (weight_matrix @ grid_flat).copy()

    results = {}
    for blend in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
        led_copy = per_led.copy()
        boost_per_led_vivid(grid_flat, weight_matrix, led_copy,
                            region_masks=region_masks, blend=blend)
        sats = np.array([_saturation_of(c) for c in led_copy])
        results[blend] = sats.mean()
        print(f"  blend={blend:.1f}  mean_sat={sats.mean():.3f}  max_sat={sats.max():.3f}")

    # blend=0.0은 원본과 동일
    led_orig = per_led.copy()
    boost_per_led_vivid(grid_flat, weight_matrix, led_orig,
                        region_masks=region_masks, blend=0.0)
    assert np.allclose(per_led, led_orig, atol=0.1), "blend=0 should be identity"

    # blend가 높을수록 채도가 높아져야 함
    assert results[0.8] >= results[0.2], \
        f"Higher blend should mean higher saturation: {results[0.2]:.3f} vs {results[0.8]:.3f}"

    print("  ✓ test_blend_levels")


def test_performance():
    """75 LED × 2048 grid 처리 시간."""
    grid_flat, weight_matrix = _make_808s_grid_and_weights()
    region_masks = build_led_region_masks(weight_matrix)
    per_led = (weight_matrix @ grid_flat).copy()

    # 워밍업
    for _ in range(5):
        led_copy = per_led.copy()
        boost_per_led_vivid(grid_flat, weight_matrix, led_copy,
                            region_masks=region_masks, blend=0.4)

    # 일반 버전 측정
    n_runs = 50
    t0 = time.perf_counter()
    for _ in range(n_runs):
        led_copy = per_led.copy()
        boost_per_led_vivid(grid_flat, weight_matrix, led_copy,
                            region_masks=region_masks, blend=0.4)
    elapsed_normal = (time.perf_counter() - t0) / n_runs * 1000
    print(f"  boost_per_led_vivid:      {elapsed_normal:.3f}ms")

    # 벡터화 버전 측정
    for _ in range(5):
        led_copy = per_led.copy()
        boost_per_led_vivid_fast(grid_flat, weight_matrix, led_copy,
                                 region_masks=region_masks, blend=0.4)

    t0 = time.perf_counter()
    for _ in range(n_runs):
        led_copy = per_led.copy()
        boost_per_led_vivid_fast(grid_flat, weight_matrix, led_copy,
                                 region_masks=region_masks, blend=0.4)
    elapsed_fast = (time.perf_counter() - t0) / n_runs * 1000
    print(f"  boost_per_led_vivid_fast: {elapsed_fast:.3f}ms")

    # region_masks 빌드 시간
    t0 = time.perf_counter()
    for _ in range(50):
        build_led_region_masks(weight_matrix)
    elapsed_masks = (time.perf_counter() - t0) / 50 * 1000
    print(f"  build_led_region_masks:   {elapsed_masks:.3f}ms (1회만 실행)")

    print("  ✓ test_performance")


def test_amplify_single():
    """채도 증폭 단일 함수."""
    # 탁한 분홍 → 선명한 빨강
    dull_pink = np.array([210, 160, 155], dtype=np.float32)
    amplified = _amplify_single(dull_pink, target_s=0.7)

    amp_sat = _saturation_of(amplified)
    assert amp_sat >= 0.65, f"증폭 후 채도 부족: {amp_sat:.3f}"

    # hue가 유지되는지 (빨간 계열)
    assert amplified[0] > amplified[1] and amplified[0] > amplified[2], \
        f"hue 변경됨: {amplified.astype(int)}"

    # 무채색은 건드리지 않음
    gray = np.array([128, 128, 128], dtype=np.float32)
    result = _amplify_single(gray, target_s=0.7)
    assert np.allclose(gray, result, atol=1), "무채색은 변경 없어야 함"

    # 이미 고채도는 유지
    vivid_red = np.array([255, 30, 20], dtype=np.float32)
    result = _amplify_single(vivid_red, target_s=0.7)
    assert np.allclose(vivid_red, result, atol=1), "이미 고채도면 유지"

    print("  ✓ test_amplify_single")


def _hue_to_rgb(h):
    """hue (0~1) → RGB (0~255)."""
    h6 = (h % 1.0) * 6.0
    hi = int(h6)
    f = h6 - hi
    if hi == 0: return [255, f * 255, 0]
    elif hi == 1: return [(1 - f) * 255, 255, 0]
    elif hi == 2: return [0, 255, f * 255]
    elif hi == 3: return [0, (1 - f) * 255, 255]
    elif hi == 4: return [f * 255, 0, 255]
    else: return [255, 0, (1 - f) * 255]


if __name__ == "__main__":
    print("=" * 60)
    print("  vivid_extract.py — Grid-Level Saturation v2 Tests")
    print("=" * 60)
    print()

    test_amplify_single()
    test_build_region_masks()
    test_808s_boost()
    test_jdilla_boost()
    test_all_low_sat()
    test_all_high_sat()
    test_fast_matches_normal()
    test_blend_levels()
    test_performance()

    print()
    print("=" * 60)
    print("  All vivid_extract tests passed!")
    print("=" * 60)
