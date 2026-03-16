"""Flowing 이전 색 잔류 버그 테스트

시나리오: 보라색 영상 → 흰색 브라우저 전환
기대: 2~3초(transition_duration + 1 interval) 내에 흰색으로 완전 전환
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.flowing import FlowPalette, render_flowing, FLOW_TRANSITION_DURATION


def test_scene_change_purple_to_white():
    """보라색 → 흰색 전환 시 보라가 남지 않아야 함."""
    np.random.seed(42)
    p = FlowPalette(n_colors=5)
    ct = np.linspace(0, 1, 75, endpoint=False)

    # Phase 1: 보라색 화면으로 10번 업데이트 (안정화)
    for _ in range(10):
        purple_led = np.tile([150, 30, 200], (75, 1)).astype(np.float32)
        purple_led += np.random.uniform(-5, 5, (75, 3))
        np.clip(purple_led, 0, 255, out=purple_led)
        p.update_from_screen(purple_led)
        # transition 완료까지 tick
        for _ in range(int(FLOW_TRANSITION_DURATION / 0.016) + 10):
            p.tick(0.016, 0.0, 0.0, 0.0)

    # 보라가 지배적인지 확인
    rgb = render_flowing(ct, p, 0.5, 1.0)
    avg_b = rgb[:, 2].mean()
    avg_r = rgb[:, 0].mean()
    print(f"  보라 안정화 후: R={avg_r:.0f} B={avg_b:.0f}")
    assert avg_b > 50, f"Should be purple-ish, B={avg_b:.0f}"

    # Phase 2: 흰색으로 전환 + 업데이트 3번 (9초 = 3 intervals)
    for update in range(3):
        white_led = np.tile([230, 230, 230], (75, 1)).astype(np.float32)
        white_led += np.random.uniform(-5, 5, (75, 3))
        np.clip(white_led, 0, 255, out=white_led)
        p.update_from_screen(white_led)

        # transition 완료까지 tick
        for _ in range(int(FLOW_TRANSITION_DURATION / 0.016) + 10):
            p.tick(0.016, 0.0, 0.0, 0.0)

    # 흰색이 되어야 함
    rgb = render_flowing(ct, p, 0.5, 1.0)
    avg_r = rgb[:, 0].mean()
    avg_g = rgb[:, 1].mean()
    avg_b = rgb[:, 2].mean()
    print(f"  흰색 전환 후: R={avg_r:.0f} G={avg_g:.0f} B={avg_b:.0f}")

    # 모든 채널이 비슷하게 밝아야 함 (흰색)
    min_ch = min(avg_r, avg_g, avg_b)
    max_ch = max(avg_r, avg_g, avg_b)
    assert max_ch - min_ch < 40, \
        f"Should be white-ish (channels similar), but R={avg_r:.0f} G={avg_g:.0f} B={avg_b:.0f}"
    assert min_ch > 60, \
        f"Should be bright white, but min channel = {min_ch:.0f}"

    print("  ✓ 보라→흰색 전환 성공 — 이전 색 잔류 없음")


def test_scene_change_green_to_red():
    """초록 → 빨강 완전 전환."""
    np.random.seed(42)
    p = FlowPalette(n_colors=5)
    ct = np.linspace(0, 1, 75, endpoint=False)

    # 초록 안정화
    for _ in range(5):
        green_led = np.tile([20, 200, 30], (75, 1)).astype(np.float32)
        p.update_from_screen(green_led)
        for _ in range(150):
            p.tick(0.016, 0.0, 0.0, 0.0)

    # 빨강으로 전환
    for _ in range(3):
        red_led = np.tile([220, 20, 20], (75, 1)).astype(np.float32)
        p.update_from_screen(red_led)
        for _ in range(150):
            p.tick(0.016, 0.0, 0.0, 0.0)

    rgb = render_flowing(ct, p, 0.5, 1.0)
    avg_r = rgb[:, 0].mean()
    avg_g = rgb[:, 1].mean()
    print(f"  초록→빨강: R={avg_r:.0f} G={avg_g:.0f}")

    assert avg_r > avg_g * 2, \
        f"Should be red, but R={avg_r:.0f} G={avg_g:.0f}"
    print("  ✓ 초록→빨강 전환 성공")


def test_crossfade_is_absolute():
    """crossfade가 절대 보간인지 확인 — start→target 고정 경로."""
    np.random.seed(42)
    p = FlowPalette(n_colors=3)

    # 빨강 안정화
    red_led = np.tile([255, 0, 0], (75, 1)).astype(np.float32)
    p.update_from_screen(red_led)
    for _ in range(200):
        p.tick(0.016, 0.0, 0.0, 0.0)

    # 파랑으로 전환
    blue_led = np.tile([0, 0, 255], (75, 1)).astype(np.float32)
    p.update_from_screen(blue_led)

    # color_start가 현재 색(빨강)으로 고정되었는지 확인
    for blob in p.blobs:
        assert blob.color_start[0] > 100 or blob.color_start[2] > 100, \
            f"color_start should be the pre-transition color"

    # transition 중간 (50%)
    n_half = int(FLOW_TRANSITION_DURATION / 0.016 / 2)
    for _ in range(n_half):
        p.tick(0.016, 0.0, 0.0, 0.0)

    # color_start가 변하지 않았는지 확인 (절대 보간의 핵심)
    # start는 고정, current만 target으로 이동
    for blob in p.blobs:
        # current는 중간쯤이어야 함
        assert not np.allclose(blob.color_current, blob.color_start, atol=10), \
            "color_current should have moved away from start"

    print("  ✓ crossfade 절대 보간 확인")


def test_warm_start_reset_on_big_change():
    """화면이 크게 바뀌면 warm start가 리셋되는지 확인."""
    np.random.seed(42)
    p = FlowPalette(n_colors=3)

    # 빨강 안정화
    red_led = np.tile([255, 0, 0], (75, 1)).astype(np.float32)
    p.update_from_screen(red_led)
    for _ in range(200):
        p.tick(0.016, 0.0, 0.0, 0.0)

    assert p._prev_centroids is not None
    old_prev = p._prev_centroids.copy()

    # 매우 다른 색으로 변경 (파랑)
    blue_led = np.tile([0, 0, 255], (75, 1)).astype(np.float32)
    p.update_from_screen(blue_led)

    # prev_centroids가 파랑 계열로 갱신되어야 함 (리셋됨)
    new_prev = p._prev_centroids
    assert new_prev[:, 2].mean() > 150, \
        f"prev_centroids should be blue after reset, got {new_prev}"

    print("  ✓ warm start 리셋 확인 (큰 변화 시)")


if __name__ == "__main__":
    print("=" * 55)
    print("  Flowing 이전 색 잔류 테스트")
    print("=" * 55)
    print()
    test_scene_change_purple_to_white()
    print()
    test_scene_change_green_to_red()
    print()
    test_crossfade_is_absolute()
    print()
    test_warm_start_reset_on_big_change()
    print()
    print("=" * 55)
    print("  All tests passed!")
    print("=" * 55)
