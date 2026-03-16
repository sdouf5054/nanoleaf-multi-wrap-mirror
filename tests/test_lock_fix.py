"""색 고착 버그 재현 + 수정 확인 테스트

시나리오: 
1. 구역이 흰색/검은색으로 수렴
2. 화면이 완전히 다른 색으로 변경
3. 구역 색이 새 색을 따라가야 함 (고착되면 안 됨)
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.color_extract import extract_zone_dominant


def test_no_lock_on_white():
    """흰색으로 수렴한 후 초록으로 변경 → 초록을 따라가야 함."""
    zone_map = np.zeros(75, dtype=np.int32)
    zone_map[:37] = 0; zone_map[37:] = 1

    # Phase 1: 흰색 화면으로 10프레임 수렴
    prev = None
    for _ in range(10):
        per_led = np.full((75, 3), 240.0, dtype=np.float32)
        per_led += np.random.uniform(-5, 5, (75, 3))
        np.clip(per_led, 0, 255, out=per_led)
        prev = extract_zone_dominant(per_led, zone_map, 2,
                                      prev_zone_colors=prev, smoothing=0.4)

    white_val = prev.copy()
    print(f"  흰색 수렴 후: zone0={white_val[0].astype(int)}, zone1={white_val[1].astype(int)}")
    assert white_val[0].mean() > 200, "Should be white-ish"

    # Phase 2: 갑자기 초록으로 변경 → 15프레임 안에 따라가야 함
    for frame in range(15):
        per_led = np.zeros((75, 3), dtype=np.float32)
        per_led[:, 1] = 200  # 초록
        per_led += np.random.uniform(-5, 5, (75, 3))
        np.clip(per_led, 0, 255, out=per_led)
        prev = extract_zone_dominant(per_led, zone_map, 2,
                                      prev_zone_colors=prev, smoothing=0.4)

    green_val = prev.copy()
    print(f"  초록 전환 후: zone0={green_val[0].astype(int)}, zone1={green_val[1].astype(int)}")

    # 초록이 되어야 함 (G채널이 R, B보다 높아야)
    assert green_val[0][1] > green_val[0][0] + 30, \
        f"Zone 0 should be green, got {green_val[0].astype(int)}"
    assert green_val[0][1] > green_val[0][2] + 30, \
        f"Zone 0 should be green, got {green_val[0].astype(int)}"

    print("  ✓ test_no_lock_on_white — 흰색→초록 전환 성공")


def test_no_lock_on_black():
    """어두운 색으로 수렴한 후 빨강으로 변경 → 빨강을 따라가야 함."""
    zone_map = np.zeros(75, dtype=np.int32)
    zone_map[:37] = 0; zone_map[37:] = 1

    # Phase 1: 어두운 화면으로 수렴 (letterbox 통과하는 정도)
    prev = None
    for _ in range(10):
        per_led = np.full((75, 3), 25.0, dtype=np.float32)
        per_led += np.random.uniform(-3, 3, (75, 3))
        np.clip(per_led, 0, 255, out=per_led)
        prev = extract_zone_dominant(per_led, zone_map, 2,
                                      prev_zone_colors=prev, smoothing=0.4)

    dark_val = prev.copy()
    print(f"  어두운 수렴 후: zone0={dark_val[0].astype(int)}, zone1={dark_val[1].astype(int)}")

    # Phase 2: 빨강으로 변경
    for frame in range(15):
        per_led = np.zeros((75, 3), dtype=np.float32)
        per_led[:, 0] = 220  # 빨강
        per_led += np.random.uniform(-5, 5, (75, 3))
        np.clip(per_led, 0, 255, out=per_led)
        prev = extract_zone_dominant(per_led, zone_map, 2,
                                      prev_zone_colors=prev, smoothing=0.4)

    red_val = prev.copy()
    print(f"  빨강 전환 후: zone0={red_val[0].astype(int)}, zone1={red_val[1].astype(int)}")

    assert red_val[0][0] > 150, f"Zone 0 should be red, got {red_val[0].astype(int)}"
    print("  ✓ test_no_lock_on_black — 어두운→빨강 전환 성공")


def test_4zone_scene_change():
    """4구역에서 화면 전체가 변할 때 모든 구역이 따라가는지."""
    np.random.seed(42)
    zone_map = np.zeros(75, dtype=np.int32)
    zone_map[0:19] = 0; zone_map[19:37] = 1
    zone_map[37:56] = 2; zone_map[56:75] = 3

    # Phase 1: 각 구역 다른 색으로 수렴
    prev = None
    for _ in range(10):
        per_led = np.zeros((75, 3), dtype=np.float32)
        per_led[0:19] = [200, 30, 30]    # zone0: 빨강
        per_led[19:37] = [30, 200, 30]   # zone1: 초록
        per_led[37:56] = [30, 30, 200]   # zone2: 파랑
        per_led[56:75] = [200, 200, 30]  # zone3: 노랑
        per_led += np.random.uniform(-5, 5, (75, 3))
        np.clip(per_led, 0, 255, out=per_led)
        prev = extract_zone_dominant(per_led, zone_map, 4,
                                      prev_zone_colors=prev, smoothing=0.4)

    print(f"  수렴 후: {[prev[i].astype(int).tolist() for i in range(4)]}")

    # Phase 2: 전체가 보라색으로 변경
    for frame in range(20):
        per_led = np.zeros((75, 3), dtype=np.float32)
        per_led[:, 0] = 150; per_led[:, 2] = 200  # 보라
        per_led += np.random.uniform(-5, 5, (75, 3))
        np.clip(per_led, 0, 255, out=per_led)
        prev = extract_zone_dominant(per_led, zone_map, 4,
                                      prev_zone_colors=prev, smoothing=0.4)

    print(f"  보라 전환: {[prev[i].astype(int).tolist() for i in range(4)]}")

    # 모든 구역이 보라색이어야 함
    for zi in range(4):
        assert prev[zi][0] > 100 and prev[zi][2] > 100, \
            f"Zone {zi} should be purple, got {prev[zi].astype(int)}"
        # 초록(G)이 dominant가 아님을 확인
        assert prev[zi][1] < prev[zi][0] or prev[zi][1] < prev[zi][2], \
            f"Zone {zi} still locked on old color: {prev[zi].astype(int)}"

    print("  ✓ test_4zone_scene_change — 4구역 전체 전환 성공")


def test_still_stable():
    """안정성 확인: 같은 화면에서 점멸하지 않는지."""
    np.random.seed(42)
    zone_map = np.zeros(75, dtype=np.int32)
    zone_map[:37] = 0; zone_map[37:] = 1

    prev = None
    max_jump = 0.0
    for frame in range(30):
        per_led = np.zeros((75, 3), dtype=np.float32)
        per_led[:37] = [180, 40, 40]
        per_led[37:] = [40, 40, 180]
        per_led += np.random.uniform(-3, 3, (75, 3))
        np.clip(per_led, 0, 255, out=per_led)

        result = extract_zone_dominant(per_led, zone_map, 2,
                                        prev_zone_colors=prev, smoothing=0.4)
        if prev is not None:
            jump = np.abs(result - prev).max()
            max_jump = max(max_jump, jump)
        prev = result.copy()

    assert max_jump < 15, f"Max jump = {max_jump:.1f} — still flickering"
    print(f"  ✓ test_still_stable (max jump: {max_jump:.1f})")


if __name__ == "__main__":
    print("=" * 55)
    print("  색 고착 버그 테스트")
    print("=" * 55)
    print()
    test_no_lock_on_white()
    print()
    test_no_lock_on_black()
    print()
    test_4zone_scene_change()
    print()
    test_still_stable()
    print()
    print("=" * 55)
    print("  All lock tests passed!")
    print("=" * 55)
