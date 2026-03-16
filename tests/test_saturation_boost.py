"""채도 우선순위 테스트 — 실제 장면 시뮬레이션

4개 장면:
1. J Dilla 창모드: 베이지 건물 + 흰 UI + 노란 텍스트
2. J Dilla 전체화면: 베이지 건물 + 노란 텍스트 + 빨간 줄무늬
3. Nujabes: 어두운 도시 + 금색 텍스트
4. 808s: 연회청색 배경 + 빨간 하트
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.color_extract import extract_zone_dominant, _saturation_of


def _make_zone(zone_pixels_list, zone_map_value, start_idx):
    """구역별 LED 데이터 구성 헬퍼."""
    pixels = []
    zone_ids = []
    for color, count in zone_pixels_list:
        for _ in range(count):
            c = np.array(color, dtype=np.float32) + np.random.uniform(-3, 3, 3)
            pixels.append(np.clip(c, 0, 255))
            zone_ids.append(zone_map_value)
    return pixels, zone_ids


def test_808s_heart():
    """808s & Heartbreak: 연회청색 배경 + 빨간 하트.
    
    4구역 기준, 중앙 구역에 빨간 하트 LED 3~4개.
    boost=0 → 연회색, boost=0.4 → 빨강 기운이 느껴져야 함.
    """
    np.random.seed(42)
    # 4구역, 각 ~19개 LED
    all_pixels = []
    all_zones = []
    
    # Zone 0 (좌상): 연회청 배경만
    p, z = _make_zone([([200, 205, 215], 19)], 0, 0)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 1 (우상): 연회청 + 빨간 하트 일부
    p, z = _make_zone([([200, 205, 215], 15), ([220, 45, 35], 4)], 1, 19)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 2 (우하): 연회청 + 빨간 하트 일부
    p, z = _make_zone([([200, 205, 215], 16), ([220, 45, 35], 3)], 2, 38)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 3 (좌하): 검은 사이드바 + 연회청
    p, z = _make_zone([([5, 5, 5], 8), ([200, 205, 215], 11)], 3, 57)
    all_pixels.extend(p); all_zones.extend(z)
    
    per_led = np.array(all_pixels, dtype=np.float32)
    zone_map = np.array(all_zones, dtype=np.int32)
    
    # boost=0 (기존)
    r0 = extract_zone_dominant(per_led, zone_map, 4, saturation_boost=0.0)
    # boost=0.4
    r4 = extract_zone_dominant(per_led, zone_map, 4, saturation_boost=0.4)
    
    print("  808s & Heartbreak (4구역):")
    for zi in range(4):
        s0 = _saturation_of(r0[zi])
        s4 = _saturation_of(r4[zi])
        print(f"    Zone {zi}: boost=0 {r0[zi].astype(int)} (sat={s0:.2f})"
              f"  →  boost=0.4 {r4[zi].astype(int)} (sat={s4:.2f})")
    
    # Zone 1,2에서 빨강 기운이 올라왔는지 확인
    assert r4[1][0] > r0[1][0] + 5, \
        f"Zone 1 R should increase with boost: {r0[1][0]:.0f} → {r4[1][0]:.0f}"
    assert _saturation_of(r4[1]) > _saturation_of(r0[1]), \
        "Zone 1 saturation should increase"
    
    # Zone 0 (하트 없음)은 거의 변화 없어야 함
    diff_z0 = np.abs(r4[0] - r0[0]).max()
    assert diff_z0 < 10, f"Zone 0 (no heart) should barely change: diff={diff_z0:.1f}"
    
    print("  ✓ 빨간 하트 구역에서 채도 상승 확인")


def test_jdilla_yellow():
    """J Dilla: 베이지 건물 + 노란 "J DILLA" 텍스트.
    
    전체화면 기준, 상단 구역에 노란 텍스트 LED.
    """
    np.random.seed(42)
    all_pixels = []
    all_zones = []
    
    # Zone 0 (좌상): 베이지 건물 + 어두운 창문
    p, z = _make_zone([([195, 185, 160], 14), ([60, 55, 50], 5)], 0, 0)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 1 (우상): 베이지 + 노란 텍스트 LED
    p, z = _make_zone([([195, 185, 160], 14), ([220, 190, 40], 5)], 1, 19)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 2 (우하): 베이지 + 빨간 줄무늬
    p, z = _make_zone([([195, 185, 160], 15), ([200, 60, 50], 4)], 2, 38)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 3 (좌하): 베이지 건물
    p, z = _make_zone([([195, 185, 160], 19)], 3, 57)
    all_pixels.extend(p); all_zones.extend(z)
    
    per_led = np.array(all_pixels, dtype=np.float32)
    zone_map = np.array(all_zones, dtype=np.int32)
    
    r0 = extract_zone_dominant(per_led, zone_map, 4, saturation_boost=0.0)
    r4 = extract_zone_dominant(per_led, zone_map, 4, saturation_boost=0.4)
    
    print("  J Dilla (4구역):")
    for zi in range(4):
        s0 = _saturation_of(r0[zi])
        s4 = _saturation_of(r4[zi])
        print(f"    Zone {zi}: boost=0 {r0[zi].astype(int)} (sat={s0:.2f})"
              f"  →  boost=0.4 {r4[zi].astype(int)} (sat={s4:.2f})")
    
    # Zone 1에서 노란 기운 상승
    assert r4[1][0] > r0[1][0] or r4[1][1] > r0[1][1], \
        "Zone 1 should get more yellow"
    assert _saturation_of(r4[1]) > _saturation_of(r0[1]) + 0.02, \
        "Zone 1 saturation should increase"
    
    print("  ✓ 노란 텍스트 구역에서 채도 상승 확인")


def test_nujabes_gold():
    """Nujabes: 어두운 도시 야경 + 금색 텍스트."""
    np.random.seed(42)
    all_pixels = []
    all_zones = []
    
    # Zone 0 (좌상): 어두운 건물
    p, z = _make_zone([([40, 40, 45], 19)], 0, 0)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 1 (우상): 어두운 건물 + 금색 텍스트
    p, z = _make_zone([([40, 40, 45], 14), ([190, 160, 50], 5)], 1, 19)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 2 (우하): 어두운 도로 + 금색 텍스트
    p, z = _make_zone([([50, 50, 50], 15), ([190, 160, 50], 4)], 2, 38)
    all_pixels.extend(p); all_zones.extend(z)
    
    # Zone 3 (좌하): 어두운 도로
    p, z = _make_zone([([50, 50, 50], 19)], 3, 57)
    all_pixels.extend(p); all_zones.extend(z)
    
    per_led = np.array(all_pixels, dtype=np.float32)
    zone_map = np.array(all_zones, dtype=np.int32)
    
    r0 = extract_zone_dominant(per_led, zone_map, 4, saturation_boost=0.0)
    r4 = extract_zone_dominant(per_led, zone_map, 4, saturation_boost=0.4)
    
    print("  Nujabes (4구역):")
    for zi in range(4):
        s0 = _saturation_of(r0[zi])
        s4 = _saturation_of(r4[zi])
        print(f"    Zone {zi}: boost=0 {r0[zi].astype(int)} (sat={s0:.2f})"
              f"  →  boost=0.4 {r4[zi].astype(int)} (sat={s4:.2f})")
    
    # Zone 1,2에서 금색 기운 상승
    assert _saturation_of(r4[1]) > _saturation_of(r0[1]) + 0.05, \
        "Zone 1 saturation should increase significantly"
    
    print("  ✓ 금색 텍스트 구역에서 채도 상승 확인")


def test_no_boost_no_regression():
    """boost=0일 때 기존 median과 동일한 결과."""
    np.random.seed(42)
    per_led = np.random.rand(75, 3).astype(np.float32) * 255
    zone_map = np.zeros(75, dtype=np.int32)
    zone_map[:37] = 0; zone_map[37:] = 1
    
    r_no = extract_zone_dominant(per_led, zone_map, 2, saturation_boost=0.0)
    r_default = extract_zone_dominant(per_led, zone_map, 2)  # 기본값도 0
    
    assert np.allclose(r_no, r_default), "boost=0 should match default"
    print("  ✓ boost=0 == 기존 동작 (regression 없음)")


def test_all_low_saturation():
    """전체 저채도 화면 → boost가 있어도 큰 변화 없음."""
    np.random.seed(42)
    # 모두 회색 계열
    per_led = np.tile([140, 140, 140], (75, 1)).astype(np.float32)
    per_led += np.random.uniform(-10, 10, (75, 3))
    np.clip(per_led, 0, 255, out=per_led)
    zone_map = np.zeros(75, dtype=np.int32)
    zone_map[:37] = 0; zone_map[37:] = 1
    
    r0 = extract_zone_dominant(per_led, zone_map, 2, saturation_boost=0.0)
    r7 = extract_zone_dominant(per_led, zone_map, 2, saturation_boost=0.7)
    
    diff = np.abs(r0 - r7).max()
    assert diff < 5, f"All-gray should barely change with boost: diff={diff:.1f}"
    print(f"  ✓ 전체 저채도 → boost 영향 없음 (diff={diff:.1f})")


def test_deterministic():
    """채도 부스트가 결정론적인지 확인 (랜덤 없음)."""
    per_led = np.vstack([
        np.tile([200, 205, 215], (15, 1)),
        np.tile([220, 45, 35], (4, 1)),
    ]).astype(np.float32)
    zone_map = np.zeros(19, dtype=np.int32)
    
    r1 = extract_zone_dominant(per_led, zone_map, 1, saturation_boost=0.4)
    r2 = extract_zone_dominant(per_led, zone_map, 1, saturation_boost=0.4)
    
    assert np.allclose(r1, r2), "Should be deterministic"
    print("  ✓ 결정론적 (같은 입력 → 같은 출력)")


if __name__ == "__main__":
    print("=" * 55)
    print("  채도 우선순위 테스트")
    print("=" * 55)
    print()
    test_808s_heart()
    print()
    test_jdilla_yellow()
    print()
    test_nujabes_gold()
    print()
    test_no_boost_no_regression()
    test_all_low_saturation()
    test_deterministic()
    print()
    print("=" * 55)
    print("  All saturation boost tests passed!")
    print("=" * 55)
