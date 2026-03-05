"""test_native_capture.py - fast_capture.dll test

Usage:
    python test_native_capture.py
"""

import time
import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from native_capture import FastCapture


def test_basic():
    print()
    print("=" * 55)
    print("  fast_capture.dll test")
    print("=" * 55)
    print()

    # -- 1. Init --
    print("[1/6] DLL init...")
    try:
        cap = FastCapture(monitor_index=0, out_width=64, out_height=32)
        print("  OK")
        print("  Screen: %d x %d" % (cap.screen_w, cap.screen_h))
        print("  Output: %d x %d" % (cap.out_width, cap.out_height))
    except Exception as e:
        print("  FAIL: %s" % e)
        return False

    # -- 2. First frame --
    print()
    print("[2/6] First frame capture...")
    print("  (Move your mouse to ensure screen changes)")
    frame = None
    for attempt in range(30):
        f = cap.grab()
        if f is not None:
            frame = f.copy()  # copy! buffer gets reused
            if frame.mean() > 0:
                break
        time.sleep(0.1)

    if frame is not None and frame.mean() > 0:
        print("  OK (attempt %d)" % (attempt + 1))
        print("  shape: %s  dtype: %s" % (frame.shape, frame.dtype))
        print("  mean: %.1f  min: %d  max: %d" % (frame.mean(), frame.min(), frame.max()))
        print("  size: %d bytes" % frame.nbytes)
    elif frame is not None:
        print("  WARNING: frame captured but all zeros (mean=0)")
        print("  This may mean Desktop Duplication returned a blank frame.")
        print("  Try moving your mouse and run again.")
    else:
        print("  FAIL: no frame after 3 seconds")
        cap.close()
        return False

    # -- 3. RGB conversion --
    print()
    print("[3/6] RGB conversion...")
    # grab a new frame first
    time.sleep(0.05)
    rgb = cap.grab_rgb()
    if rgb is not None:
        rgb = rgb.copy()
        print("  OK: shape=%s dtype=%s mean=%.1f" % (rgb.shape, rgb.dtype, rgb.mean()))
        # verify BGRA->RGB swap: B and R channels should differ
        bgra = cap.grab()
        if bgra is not None:
            bgra = bgra.copy()
            print("  BGRA[0,0]: B=%d G=%d R=%d A=%d" % (
                bgra[0,0,0], bgra[0,0,1], bgra[0,0,2], bgra[0,0,3]))
    else:
        print("  No new frame (normal if screen static)")

    # -- 4. Continuous capture --
    print()
    print("[4/6] Continuous capture (30 frames, ~30fps)...")
    success = 0
    no_change = 0
    nonzero_frames = 0
    for i in range(30):
        f = cap.grab()
        if f is not None:
            success += 1
            if f.mean() > 0:
                nonzero_frames += 1
        else:
            no_change += 1
        time.sleep(0.033)

    print("  New frames: %d, No change: %d, Non-zero: %d" % (
        success, no_change, nonzero_frames))

    # -- 5. Performance (with sleep to get real frames) --
    print()
    print("[5/6] Performance (500 calls with 1ms sleep)...")

    # warmup
    for _ in range(20):
        cap.grab()
        time.sleep(0.001)

    frame_count = 0
    total_calls = 500

    t_start = time.perf_counter()
    for _ in range(total_calls):
        f = cap.grab()
        if f is not None:
            frame_count += 1
        time.sleep(0.001)  # small sleep so DXGI can produce frames
    t_end = time.perf_counter()

    elapsed = t_end - t_start
    ms_per_call = elapsed / total_calls * 1000
    effective_fps = frame_count / elapsed if elapsed > 0 else 0

    print("  Total calls: %d in %.2fs" % (total_calls, elapsed))
    print("  Per call: %.2f ms (includes 1ms sleep)" % ms_per_call)
    print("  New frames: %d / %d" % (frame_count, total_calls))
    print("  Effective fps: %.0f" % effective_fps)
    print("  Output size: %d bytes/frame (vs 14MB full frame)" % (cap.out_width * cap.out_height * 4))

    # -- 6. Raw speed (no sleep) --
    print()
    print("[6/6] Raw grab() speed (1000 calls, no sleep)...")
    t_start = time.perf_counter()
    for _ in range(1000):
        cap.grab()
    t_end = time.perf_counter()
    raw_elapsed = t_end - t_start
    raw_us = raw_elapsed / 1000 * 1_000_000

    print("  Per call: %.1f us (%.3f ms)" % (raw_us, raw_us / 1000))
    print("  Max throughput: %.0f calls/sec" % (1000 / raw_elapsed))

    # -- Cleanup --
    cap.close()

    print()
    print("=" * 55)
    print("  Test complete!")
    print("=" * 55)
    print()

    if nonzero_frames == 0 and success > 0:
        print("NOTE: Frames were captured but all had mean=0.")
        print("This is unusual. Try moving your mouse during the test.")
        print()

    return True


if __name__ == "__main__":
    success = test_basic()
    sys.exit(0 if success else 1)
