"""core/hsv_utils.py 통합 테스트

검증 포인트:
1. 스칼라 HSV↔RGB 왕복 변환 정확성
2. 벡터 HSV↔RGB 왕복 변환 정확성
3. saturation_of / saturation_array 일관성
4. lerp_hsv 경계값 + 보간 정확성
5. amplify_saturation 동작 확인
6. 엣지케이스: 무채색, 검정, 흰색, 순색
"""

import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.hsv_utils import (
    rgb_to_hsv, hsv_to_rgb,
    rgb_array_to_hsv, hsv_to_rgb_array,
    saturation_of, saturation_array,
    lerp_hsv, amplify_saturation,
)


def test_scalar_roundtrip():
    """스칼라 RGB → HSV → RGB 왕복 변환."""
    test_cases = [
        [255, 0, 0],      # 순 빨강
        [0, 255, 0],      # 순 초록
        [0, 0, 255],      # 순 파랑
        [255, 255, 0],    # 노랑
        [255, 0, 255],    # 마젠타
        [0, 255, 255],    # 시안
        [128, 64, 200],   # 임의 색
        [255, 255, 255],  # 흰색
        [0, 0, 0],        # 검정
        [128, 128, 128],  # 회색
    ]
    for rgb_in in test_cases:
        arr = np.array(rgb_in, dtype=np.float32)
        h, s, v = rgb_to_hsv(arr)
        arr_out = hsv_to_rgb(h, s, v)
        diff = np.abs(arr - arr_out).max()
        assert diff < 2, f"Scalar roundtrip failed for {rgb_in}: diff={diff}, out={arr_out}"

    print("  ✓ test_scalar_roundtrip")


def test_vector_roundtrip():
    """벡터 RGB → HSV → RGB 왕복 변환."""
    np.random.seed(42)
    rgb = np.random.rand(100, 3).astype(np.float32) * 255

    h, s, v = rgb_array_to_hsv(rgb)
    rgb_out = hsv_to_rgb_array(h, s, v)

    diff = np.abs(rgb - rgb_out).max()
    assert diff < 2, f"Vector roundtrip max diff = {diff}"

    print("  ✓ test_vector_roundtrip")


def test_scalar_vector_consistency():
    """스칼라와 벡터 변환 결과가 일치하는지 확인."""
    test_colors = np.array([
        [255, 0, 0], [0, 255, 0], [0, 0, 255],
        [128, 64, 200], [50, 150, 100],
    ], dtype=np.float32)

    h_vec, s_vec, v_vec = rgb_array_to_hsv(test_colors)

    for i in range(len(test_colors)):
        h_s, s_s, v_s = rgb_to_hsv(test_colors[i])
        assert abs(h_s - h_vec[i]) < 0.001, f"H mismatch at {i}: {h_s} vs {h_vec[i]}"
        assert abs(s_s - s_vec[i]) < 0.001, f"S mismatch at {i}: {s_s} vs {s_vec[i]}"
        assert abs(v_s - v_vec[i]) < 0.001, f"V mismatch at {i}: {v_s} vs {v_vec[i]}"

    print("  ✓ test_scalar_vector_consistency")


def test_hsv_ranges():
    """HSV 값이 항상 0~1 범위인지 확인."""
    np.random.seed(42)
    rgb = np.random.rand(200, 3).astype(np.float32) * 255

    h, s, v = rgb_array_to_hsv(rgb)
    assert np.all(h >= 0) and np.all(h <= 1), f"H out of range: [{h.min()}, {h.max()}]"
    assert np.all(s >= 0) and np.all(s <= 1), f"S out of range: [{s.min()}, {s.max()}]"
    assert np.all(v >= 0) and np.all(v <= 1), f"V out of range: [{v.min()}, {v.max()}]"

    print("  ✓ test_hsv_ranges")


def test_pure_colors_hue():
    """순색의 hue 값이 정확한지 확인."""
    # 빨강: h ≈ 0.0, 초록: h ≈ 0.333, 파랑: h ≈ 0.667
    h_r, _, _ = rgb_to_hsv([255, 0, 0])
    h_g, _, _ = rgb_to_hsv([0, 255, 0])
    h_b, _, _ = rgb_to_hsv([0, 0, 255])

    assert abs(h_r - 0.0) < 0.01, f"Red hue = {h_r}"
    assert abs(h_g - 1.0/3.0) < 0.01, f"Green hue = {h_g}"
    assert abs(h_b - 2.0/3.0) < 0.01, f"Blue hue = {h_b}"

    print("  ✓ test_pure_colors_hue")


def test_saturation_of_basic():
    """채도 계산 기본 검증."""
    assert abs(saturation_of([255, 0, 0]) - 1.0) < 0.01  # 순색 = 1.0
    assert abs(saturation_of([128, 128, 128]) - 0.0) < 0.01  # 무채색 = 0.0
    assert abs(saturation_of([0, 0, 0]) - 0.0) < 0.01  # 검정 = 0.0
    assert abs(saturation_of([255, 255, 255]) - 0.0) < 0.01  # 흰색 = 0.0
    assert 0.0 < saturation_of([200, 100, 50]) < 1.0  # 탁한 색 = 중간

    print("  ✓ test_saturation_of_basic")


def test_saturation_array_matches_scalar():
    """saturation_array와 saturation_of 결과 일치."""
    pixels = np.array([
        [255, 0, 0], [128, 128, 128], [200, 100, 50], [0, 255, 128],
    ], dtype=np.float32)

    arr_result = saturation_array(pixels)
    for i in range(len(pixels)):
        scalar_result = saturation_of(pixels[i])
        assert abs(arr_result[i] - scalar_result) < 0.001, \
            f"Mismatch at {i}: array={arr_result[i]}, scalar={scalar_result}"

    print("  ✓ test_saturation_array_matches_scalar")


def test_lerp_hsv_endpoints():
    """lerp_hsv 경계값: t=0 → color_a, t=1 → color_b."""
    red = np.array([255, 0, 0], dtype=np.float32)
    blue = np.array([0, 0, 255], dtype=np.float32)

    result_0 = lerp_hsv(red, blue, 0.0)
    result_1 = lerp_hsv(red, blue, 1.0)

    assert np.abs(result_0 - red).max() < 2, f"t=0 should be red: {result_0}"
    assert np.abs(result_1 - blue).max() < 2, f"t=1 should be blue: {result_1}"

    print("  ✓ test_lerp_hsv_endpoints")


def test_lerp_hsv_midpoint():
    """lerp_hsv 중간값: 빨강→파랑의 중간은 보라/마젠타 계열."""
    red = np.array([255, 0, 0], dtype=np.float32)
    blue = np.array([0, 0, 255], dtype=np.float32)

    mid = lerp_hsv(red, blue, 0.5)
    # 중간은 보라/마젠타 계열 — R과 B가 모두 높아야 함
    assert mid[0] > 50 or mid[2] > 50, f"Midpoint should be purple-ish: {mid}"

    print("  ✓ test_lerp_hsv_midpoint")


def test_lerp_hsv_shortest_path():
    """lerp_hsv가 hue shortest path를 사용하는지 확인."""
    # 빨강(h=0) → 마젠타(h≈0.83): 시계방향보다 반시계방향이 짧음
    red = np.array([255, 0, 0], dtype=np.float32)
    magenta = np.array([255, 0, 255], dtype=np.float32)

    mid = lerp_hsv(red, magenta, 0.5)
    # shortest path: 빨강→마젠타(반시계) = 핑크/마젠타 계열
    # R이 높아야 함 (초록을 거치지 않음)
    assert mid[0] > 100, f"Should go via pink, not green: {mid}"

    print("  ✓ test_lerp_hsv_shortest_path")


def test_amplify_saturation_basic():
    """채도 증폭 기본 검증."""
    # 탁한 분홍 → 선명하게
    dull_pink = np.array([[210, 160, 155]], dtype=np.float32)
    sats = np.array([saturation_of(dull_pink[0])])
    amplified = amplify_saturation(dull_pink, sats, target_s=0.7)

    amp_sat = saturation_of(amplified[0])
    assert amp_sat >= 0.65, f"Amplified saturation too low: {amp_sat}"

    # hue 유지 (빨간 계열)
    assert amplified[0][0] > amplified[0][1] and amplified[0][0] > amplified[0][2], \
        f"Hue changed: {amplified[0]}"

    print("  ✓ test_amplify_saturation_basic")


def test_amplify_saturation_passthrough():
    """이미 고채도인 색은 변경 없음."""
    vivid = np.array([[255, 30, 20]], dtype=np.float32)
    sats = np.array([saturation_of(vivid[0])])
    result = amplify_saturation(vivid, sats, target_s=0.7)
    assert np.allclose(vivid, result, atol=1), f"Already vivid should pass through: {result}"

    print("  ✓ test_amplify_saturation_passthrough")


def test_amplify_saturation_gray():
    """무채색은 건드리지 않음."""
    gray = np.array([[128, 128, 128]], dtype=np.float32)
    sats = np.array([0.0])
    result = amplify_saturation(gray, sats, target_s=0.7)
    assert np.allclose(gray, result, atol=1), f"Gray should not change: {result}"

    print("  ✓ test_amplify_saturation_gray")


def test_vector_empty():
    """빈 배열 처리."""
    empty = np.zeros((0, 3), dtype=np.float32)
    h, s, v = rgb_array_to_hsv(empty)
    assert len(h) == 0
    rgb = hsv_to_rgb_array(h, s, v)
    assert rgb.shape == (0, 3)

    print("  ✓ test_vector_empty")


if __name__ == "__main__":
    print("=" * 60)
    print("  core/hsv_utils.py — 통합 테스트")
    print("=" * 60)
    print()

    test_scalar_roundtrip()
    test_vector_roundtrip()
    test_scalar_vector_consistency()
    test_hsv_ranges()
    test_pure_colors_hue()
    test_saturation_of_basic()
    test_saturation_array_matches_scalar()
    test_lerp_hsv_endpoints()
    test_lerp_hsv_midpoint()
    test_lerp_hsv_shortest_path()
    test_amplify_saturation_basic()
    test_amplify_saturation_passthrough()
    test_amplify_saturation_gray()
    test_vector_empty()

    print()
    print("=" * 60)
    print("  All hsv_utils tests passed!")
    print("=" * 60)
