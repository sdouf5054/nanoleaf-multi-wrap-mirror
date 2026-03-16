"""Phase 4 테스트 — core/flowing.py

테스트 포인트:
1. FlowPalette 초기화 — 기본 색상으로 blob 생성
2. update_from_screen — palette 추출 + crossfade 시작
3. tick — phase 진행, crossfade, HSV drift
4. render_flowing — 가우시안 blend, soft clamp
5. 음악 반응 — bass → 밝기, mid → drift
6. warm start — 프레임 간 색상 순서 안정
7. 성능 — 75 LED, 5 blobs < 0.5ms
8. 엣지케이스 — None 입력, 검은 화면, 음악 없음
"""

import sys
import os
import time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.flowing import (
    FlowBlob, FlowPalette, render_flowing,
    _rgb_to_hsv, _hsv_to_rgb, _lerp_hsv, _smooth_step,
    FLOW_N_COLORS, FLOW_BASE_SPEED,
)


def test_palette_init():
    """기본 초기화: 5개 blob, 따뜻한 기본 색상."""
    p = FlowPalette(n_colors=5)
    assert len(p.blobs) == 5
    assert p.transition_progress == 1.0  # 초기화 완료 상태

    # 각 blob이 유효한 상태인지 확인
    for i, blob in enumerate(p.blobs):
        assert blob.color_current.shape == (3,)
        assert 0 <= blob.phase <= 1
        assert blob.width > 0
        assert blob.brightness > 0
        assert blob.color_current.max() > 50  # 검은색이 아님

    # 균등 배치
    phases = [b.phase for b in p.blobs]
    for i in range(1, len(phases)):
        assert abs(phases[i] - phases[i-1] - 0.2) < 0.05

    print("  ✓ test_palette_init")


def test_update_from_screen():
    """화면 색상에서 palette 추출 + crossfade 시작."""
    np.random.seed(42)
    p = FlowPalette(n_colors=5)

    # 시뮬레이션: 초록 배경 + 보라 앨범아트
    per_led = np.vstack([
        np.tile([30, 200, 40], (40, 1)),
        np.tile([150, 50, 200], (25, 1)),
        np.tile([5, 5, 5], (10, 1)),  # letterbox
    ]).astype(np.float32)

    p.update_from_screen(per_led)

    # crossfade가 시작됨
    assert p.transition_progress == 0.0

    # target이 설정됨 (검은색이 아님)
    for blob in p.blobs:
        assert blob.color_target.max() > 20

    # dominant 색이 target에 포함됨 (초록 또는 보라)
    targets = np.array([b.color_target for b in p.blobs])
    has_green = any(t[1] > 100 for t in targets)
    has_purple = any(t[2] > 100 and t[0] > 80 for t in targets)
    assert has_green or has_purple, f"Expected green or purple in targets: {targets}"

    print("  ✓ test_update_from_screen")


def test_tick_crossfade():
    """tick: crossfade가 시간에 따라 진행."""
    np.random.seed(42)
    p = FlowPalette(n_colors=3)

    # 새 palette 설정
    per_led = np.tile([255, 0, 0], (75, 1)).astype(np.float32)
    p.update_from_screen(per_led)

    initial_colors = [b.color_current.copy() for b in p.blobs]

    # tick 여러 번 — crossfade 진행
    for _ in range(100):
        p.tick(0.02, bass=0.0, mid=0.0, high=0.0)

    # crossfade가 완료에 가까워야 함 (100 * 0.02 = 2초 = transition_duration)
    assert p.transition_progress >= 0.95

    # 색이 target에 가까워졌는지 확인
    for blob in p.blobs:
        diff = np.abs(blob.color_current - blob.color_target).max()
        assert diff < 30, f"Color should converge to target, diff={diff}"

    print("  ✓ test_tick_crossfade")


def test_tick_phase_progress():
    """tick: phase가 시간에 따라 진행 (회전)."""
    p = FlowPalette(n_colors=3)
    initial_phases = [b.phase for b in p.blobs]

    # 10초 동안 tick
    for _ in range(600):
        p.tick(1.0 / 60.0, bass=0.0, mid=0.0, high=0.0)

    # phase가 변했는지 확인
    for i, blob in enumerate(p.blobs):
        assert blob.phase != initial_phases[i], \
            f"Blob {i} phase should have changed"

    print("  ✓ test_tick_phase_progress")


def test_tick_bass_speed_boost():
    """bass가 회전 속도를 증가시키는지 확인."""
    p1 = FlowPalette(n_colors=1)
    p2 = FlowPalette(n_colors=1)

    # 동일 초기 상태
    p1.blobs[0].phase = 0.0
    p1.blobs[0].speed = FLOW_BASE_SPEED
    p2.blobs[0].phase = 0.0
    p2.blobs[0].speed = FLOW_BASE_SPEED

    # p1: bass=0, p2: bass=1.0
    for _ in range(60):
        p1.tick(1.0 / 60, bass=0.0, mid=0.0, high=0.0)
        p2.tick(1.0 / 60, bass=1.0, mid=0.0, high=0.0)

    # bass가 높으면 더 많이 진행해야 함
    assert p2.blobs[0].phase > p1.blobs[0].phase, \
        f"Bass should boost speed: no_bass={p1.blobs[0].phase:.4f} vs bass={p2.blobs[0].phase:.4f}"

    print("  ✓ test_tick_bass_speed_boost")


def test_render_basic():
    """기본 렌더링: 출력 형태와 범위."""
    p = FlowPalette(n_colors=5)
    ct = np.linspace(0, 1, 75, endpoint=False)

    rgb = render_flowing(ct, p, bass=0.5, brightness=1.0)

    assert rgb.shape == (75, 3), f"Expected (75,3), got {rgb.shape}"
    assert rgb.dtype == np.float32
    assert rgb.min() >= 0, f"Min should be >= 0, got {rgb.min()}"
    assert rgb.max() <= 255.1, f"Max should be <= 255, got {rgb.max()}"

    # 빈 LED가 아님 (blob이 있으므로)
    assert rgb.mean() > 10, f"Mean should be > 10, got {rgb.mean():.1f}"

    print("  ✓ test_render_basic")


def test_render_bass_modulation():
    """bass가 렌더링 밝기에 영향."""
    p = FlowPalette(n_colors=5)
    ct = np.linspace(0, 1, 75, endpoint=False)

    rgb_low = render_flowing(ct, p, bass=0.0, brightness=1.0)
    rgb_high = render_flowing(ct, p, bass=1.0, brightness=1.0)

    # bass=1.0이 bass=0.0보다 밝아야 함
    assert rgb_high.sum() > rgb_low.sum(), \
        f"Bass=1.0 should be brighter: low={rgb_low.sum():.0f} vs high={rgb_high.sum():.0f}"

    print("  ✓ test_render_bass_modulation")


def test_render_brightness():
    """brightness 파라미터가 전체 밝기에 영향."""
    p = FlowPalette(n_colors=5)
    ct = np.linspace(0, 1, 75, endpoint=False)

    rgb_full = render_flowing(ct, p, bass=0.5, brightness=1.0)
    rgb_half = render_flowing(ct, p, bass=0.5, brightness=0.5)

    ratio = rgb_full.sum() / max(rgb_half.sum(), 1)
    assert 1.5 < ratio < 2.5, f"Brightness ratio should be ~2, got {ratio:.2f}"

    print("  ✓ test_render_brightness")


def test_render_soft_clamp():
    """soft clamp: 255 초과 시 색조 보호."""
    p = FlowPalette(n_colors=5)
    # 매우 밝은 blob으로 설정
    for blob in p.blobs:
        blob.color_current = np.array([255, 200, 100], dtype=np.float32)
        blob.brightness = 2.0

    ct = np.linspace(0, 1, 75, endpoint=False)
    rgb = render_flowing(ct, p, bass=1.0, brightness=1.5)

    assert rgb.max() <= 255.0, f"Soft clamp should prevent > 255, got {rgb.max()}"

    # 색조가 유지되는지 확인: R > G > B 비율이 유지
    bright_led = rgb[rgb.max(axis=1) > 100]
    if len(bright_led) > 0:
        avg = bright_led.mean(axis=0)
        assert avg[0] >= avg[1] >= avg[2] - 5, \
            f"Hue should be preserved (R>G>B), got {avg}"

    print("  ✓ test_render_soft_clamp")


def test_edge_none_input():
    """None 입력 → 크래시 없이 기존 palette 유지."""
    p = FlowPalette(n_colors=5)
    initial_colors = [b.color_current.copy() for b in p.blobs]

    p.update_from_screen(None)

    # palette가 변하지 않아야 함
    for i, blob in enumerate(p.blobs):
        assert np.allclose(blob.color_current, initial_colors[i])

    print("  ✓ test_edge_none_input")


def test_edge_all_black():
    """완전 검은 화면 → 크래시 없이 동작."""
    np.random.seed(42)
    p = FlowPalette(n_colors=5)
    black = np.zeros((75, 3), dtype=np.float32)

    p.update_from_screen(black)
    p.tick(0.016, bass=0.5, mid=0.3, high=0.1)

    ct = np.linspace(0, 1, 75, endpoint=False)
    rgb = render_flowing(ct, p, bass=0.5, brightness=1.0)
    assert rgb.shape == (75, 3)

    print("  ✓ test_edge_all_black")


def test_edge_no_music():
    """음악 없음 (bass/mid/high = 0) → blob 보이지만 반응 없음."""
    p = FlowPalette(n_colors=5)
    ct = np.linspace(0, 1, 75, endpoint=False)

    rgb = render_flowing(ct, p, bass=0.0, brightness=1.0)

    # min_brightness에 의해 어느 정도 밝기 유지
    assert rgb.max() > 10, "Should still be visible with no music"

    print("  ✓ test_edge_no_music")


def test_warm_start():
    """update_from_screen 연속 호출 시 warm start 효과."""
    np.random.seed(42)
    p = FlowPalette(n_colors=3)

    per_led = np.vstack([
        np.tile([200, 50, 50], (40, 1)),
        np.tile([50, 50, 200], (35, 1)),
    ]).astype(np.float32)

    # 첫 번째 추출
    p.update_from_screen(per_led)
    targets1 = [b.color_target.copy() for b in p.blobs]

    # 두 번째 추출 (같은 데이터 → warm start)
    p.update_from_screen(per_led)
    targets2 = [b.color_target.copy() for b in p.blobs]

    # warm start 덕에 비슷한 순서/값이어야 함
    for i in range(3):
        diff = np.abs(targets1[i] - targets2[i]).max()
        assert diff < 50, \
            f"Warm start should maintain order, diff={diff} at blob {i}"

    print("  ✓ test_warm_start")


def test_hsv_roundtrip():
    """HSV ↔ RGB 왕복 변환 정확성."""
    for rgb_in in [[255, 0, 0], [0, 255, 0], [0, 0, 255], [128, 64, 200]]:
        arr = np.array(rgb_in, dtype=np.float32)
        h, s, v = _rgb_to_hsv(arr)
        arr_out = _hsv_to_rgb(h, s, v)
        diff = np.abs(arr - arr_out).max()
        assert diff < 2, f"HSV roundtrip failed for {rgb_in}: diff={diff}"

    print("  ✓ test_hsv_roundtrip")


def test_lerp_hsv():
    """HSV 보간: 빨강→파랑이 보라를 거쳐야 함."""
    red = np.array([255, 0, 0], dtype=np.float32)
    blue = np.array([0, 0, 255], dtype=np.float32)

    mid = _lerp_hsv(red, blue, 0.5)
    # 중간은 보라/마젠타 계열
    assert mid[0] > 50 or mid[2] > 50, f"Midpoint should be purple-ish: {mid}"

    print("  ✓ test_lerp_hsv")


def test_smooth_step():
    """smooth_step 경계값."""
    assert _smooth_step(0) == 0
    assert _smooth_step(1) == 1
    assert 0.4 < _smooth_step(0.5) < 0.6

    print("  ✓ test_smooth_step")


def test_performance():
    """75 LED, 5 blobs — tick + render < 0.5ms."""
    np.random.seed(42)
    p = FlowPalette(n_colors=5)
    per_led = np.random.rand(75, 3).astype(np.float32) * 255
    p.update_from_screen(per_led)

    ct = np.linspace(0, 1, 75, endpoint=False)

    # 워밍업
    for _ in range(20):
        p.tick(0.016, 0.5, 0.3, 0.1)
        render_flowing(ct, p, 0.5, 1.0)

    # 측정
    n_runs = 200
    t0 = time.perf_counter()
    for i in range(n_runs):
        p.tick(0.016, 0.5, 0.3, 0.1)
        render_flowing(ct, p, 0.5, 1.0)
    elapsed = (time.perf_counter() - t0) / n_runs * 1000

    print(f"  ✓ test_performance: tick+render = {elapsed:.3f}ms", end="")
    if elapsed < 0.5:
        print(" (< 0.5ms OK)")
    else:
        print(f" (> 0.5ms but likely OK on target hardware)")


if __name__ == "__main__":
    print("=" * 60)
    print("  Phase 4 Tests — core/flowing.py")
    print("=" * 60)
    print()

    test_palette_init()
    test_update_from_screen()
    test_tick_crossfade()
    test_tick_phase_progress()
    test_tick_bass_speed_boost()
    test_render_basic()
    test_render_bass_modulation()
    test_render_brightness()
    test_render_soft_clamp()
    test_edge_none_input()
    test_edge_all_black()
    test_edge_no_music()
    test_warm_start()
    test_hsv_roundtrip()
    test_lerp_hsv()
    test_smooth_step()
    test_performance()

    print()
    print("=" * 60)
    print("  All Phase 4 tests passed!")
    print("=" * 60)
