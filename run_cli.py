"""
Nanoleaf Screen Mirror — CLI 실행
core 모듈 검증용. GUI 없이 기존 v222과 동일하게 동작.

사용법:
    python run_cli.py
"""

import time
import sys

from core.config import load_config
from core.device import NanoleafDevice
from core.capture import ScreenCapture
from core.layout import get_led_positions, build_weight_matrix
from core.color import compute_led_colors


def check_key():
    try:
        import msvcrt
        if msvcrt.kbhit():
            return msvcrt.getch().decode("utf-8", errors="ignore")
    except Exception:
        pass
    return None


def main():
    cfg = load_config()
    dev_cfg = cfg["device"]
    layout_cfg = cfg["layout"]
    color_cfg = cfg["color"]
    mirror_cfg = cfg["mirror"]

    led_count = dev_cfg["led_count"]
    vendor_id = int(dev_cfg["vendor_id"], 16)
    product_id = int(dev_cfg["product_id"], 16)

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Nanoleaf Screen Mirror — CLI                          ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # --- 화면 캡처 ---
    print("[1/3] 화면 캡처 초기화...")
    capture = ScreenCapture(mirror_cfg["monitor_index"])
    if not capture.start():
        sys.exit(1)
    print(f"  모니터 해상도: {capture.screen_w}×{capture.screen_h}")

    # --- 가중치 행렬 ---
    print("[2/3] 가중치 샘플링 매트릭스 생성...")
    led_positions, led_sides = get_led_positions(
        capture.screen_w, capture.screen_h,
        layout_cfg["segments"], led_count,
        orientation=mirror_cfg.get("orientation", "auto"),
        portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
    )
    weight_matrix = build_weight_matrix(
        capture.screen_w, capture.screen_h,
        led_positions, led_sides,
        mirror_cfg["grid_cols"], mirror_cfg["grid_rows"],
        mirror_cfg["decay_radius"], mirror_cfg["parallel_penalty"]
    )
    print(f"  그리드: {mirror_cfg['grid_cols']}×{mirror_cfg['grid_rows']}")
    print(f"  감쇠: {mirror_cfg['decay_radius']:.0%}, 타원: {mirror_cfg['parallel_penalty']:.1f}×")
    print(f"  행렬: {weight_matrix.shape} ({weight_matrix.nbytes / 1024:.1f}KB)")
    print()

    # --- USB 연결 ---
    print("[3/3] Nanoleaf 연결...")
    print("  ⚠️  Nanoleaf Desktop 앱을 종료하세요!")
    nanoleaf = NanoleafDevice(vendor_id, product_id, led_count)
    try:
        nanoleaf.connect()
    except ConnectionError as e:
        print(f"  {e}")
        capture.stop()
        sys.exit(1)
    print("  ✅ 연결 완료")
    nanoleaf.test_rgb()

    # 캡처 검증
    f = capture.grab()
    if f is not None:
        print(f"  캡처 검증: OK ({f.shape})")

    print()
    print("─" * 55)
    print("  조작: q=종료, p=일시정지")
    print("        1-9=밝기, 0=MAX, s=스무딩, d=디버그")
    print("─" * 55)
    print()

    # --- 미러링 루프 ---
    prev_colors = None
    paused = False
    debug_mode = False
    frame_count = 0
    start_time = time.time()
    fps_display_time = start_time
    frame_interval = 1.0 / mirror_cfg["target_fps"]

    try:
        while True:
            loop_start = time.perf_counter()

            key = check_key()
            if key == "q":
                print("\n👋 종료")
                break
            elif key == "p":
                paused = not paused
                if paused:
                    nanoleaf.turn_off()
                    print("\n  ⏸️  일시정지")
                else:
                    prev_colors = None
                    print("\n  ▶️  재개")
            elif key == "s":
                v = mirror_cfg["smoothing_factor"]
                mirror_cfg["smoothing_factor"] = 0.0 if v > 0 else 0.5
                print(f"\n  스무딩: {'ON' if mirror_cfg['smoothing_factor'] > 0 else 'OFF'}")
            elif key == "d":
                debug_mode = not debug_mode
                print(f"\n  디버그: {'ON' if debug_mode else 'OFF'}")
            elif key == "0":
                mirror_cfg["brightness"] = 1.0
                print("\n  밝기: MAX (100%)")
            elif key and key.isdigit():
                mirror_cfg["brightness"] = int(key) / 9.0
                print(f"\n  밝기: {int(mirror_cfg['brightness'] * 100)}%")

            if paused:
                time.sleep(0.05)
                continue

            frame = capture.grab()
            if frame is None:
                time.sleep(0.005)
                continue

            t1 = time.perf_counter()

            grb_data, rgb_colors = compute_led_colors(
                frame, weight_matrix, color_cfg, mirror_cfg, prev_colors
            )
            prev_colors = rgb_colors

            t2 = time.perf_counter()

            nanoleaf.send_rgb(grb_data)
            frame_count += 1

            t3 = time.perf_counter()

            now = time.time()
            if now - fps_display_time >= 1.0:
                fps = frame_count / (now - start_time)
                if debug_mode:
                    grab_ms = (t1 - loop_start) * 1000
                    calc_ms = (t2 - t1) * 1000
                    usb_ms = (t3 - t2) * 1000
                    print(
                        f"\r  {fps:.1f}fps | grab:{grab_ms:.0f}ms "
                        f"calc:{calc_ms:.0f}ms usb:{usb_ms:.0f}ms  ",
                        end="", flush=True,
                    )
                else:
                    print(f"\r  {fps:.1f} fps", end="", flush=True)
                fps_display_time = now

            elapsed = time.perf_counter() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n\n👋 Ctrl+C 종료")
    finally:
        elapsed_total = time.time() - start_time
        avg_fps = frame_count / elapsed_total if elapsed_total > 0 else 0
        print(f"\n\n  총 {frame_count}프레임 / {elapsed_total:.1f}초 = {avg_fps:.1f}fps")
        nanoleaf.disconnect()
        capture.stop()
        print("  🔌 연결 해제")


if __name__ == "__main__":
    main()
