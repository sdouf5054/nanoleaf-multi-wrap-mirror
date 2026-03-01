"""색상 처리 — 다운샘플, 감마, 비선형 채널 믹싱, WB, 밝기"""

import numpy as np
import cv2


def downsample_frame(frame, grid_rows, grid_cols):
    """프레임을 grid 크기로 다운샘플 (cv2.resize INTER_AREA)"""
    small = cv2.resize(frame, (grid_cols, grid_rows), interpolation=cv2.INTER_AREA)
    return small.reshape(-1, 3).astype(np.float32)


def compute_led_colors(frame, weight_matrix, color_cfg, mirror_cfg,
                       prev_colors=None):
    """
    가중치 샘플링 → 색상 보정 파이프라인.

    Args:
        frame: (H, W, 3) 캡처 프레임
        weight_matrix: (N, cells) 가중치 행렬
        color_cfg: config["color"] dict
        mirror_cfg: config["mirror"] dict
        prev_colors: 이전 프레임 RGB (스무딩용)

    Returns:
        grb_bytes: bytes — LED 전송용
        rgb_colors: np.array (N, 3) — 현재 프레임 RGB (다음 스무딩용)
    """
    grid_rows = mirror_cfg["grid_rows"]
    grid_cols = mirror_cfg["grid_cols"]
    smoothing = mirror_cfg["smoothing_factor"]
    brightness = mirror_cfg["brightness"]

    # 1. 다운샘플
    grid_colors = downsample_frame(frame, grid_rows, grid_cols)

    # 2. 가중치 행렬곱
    rgb_colors = weight_matrix @ grid_colors

    # 3. 감마 보정
    rgb_norm = rgb_colors / 255.0
    rgb_norm[:, 0] = np.power(rgb_norm[:, 0], color_cfg["gamma_r"])
    rgb_norm[:, 1] = np.power(rgb_norm[:, 1], color_cfg["gamma_g"])
    rgb_norm[:, 2] = np.power(rgb_norm[:, 2], color_cfg["gamma_b"])
    rgb_colors = rgb_norm * 255.0

    # 4. 비선형 채널 믹싱 (초록-노랑 구간 왜곡 방지)
    # Green이 Red보다 강할 때만, 차이에 비례하여 Red에 추가
    R = rgb_colors[:, 0]
    G = rgb_colors[:, 1]
    R_add = np.maximum(0, G - R) * color_cfg["green_red_bleed"]
    rgb_colors[:, 0] = R + R_add

    # 5. 화이트밸런스
    rgb_colors[:, 0] *= color_cfg["wb_r"]
    rgb_colors[:, 1] *= color_cfg["wb_g"]
    rgb_colors[:, 2] *= color_cfg["wb_b"]

    # 6. 밝기 스케일
    if brightness < 1.0:
        rgb_colors *= brightness

    # 7. 스무딩
    if prev_colors is not None and smoothing > 0:
        rgb_colors = prev_colors * smoothing + rgb_colors * (1 - smoothing)

    rgb_colors = np.clip(rgb_colors, 0, 255)

    # RGB → GRB (벡터화)
    rgb_uint8 = rgb_colors.astype(np.uint8)
    grb = np.empty_like(rgb_uint8)
    grb[:, 0] = rgb_uint8[:, 1]  # G
    grb[:, 1] = rgb_uint8[:, 0]  # R
    grb[:, 2] = rgb_uint8[:, 2]  # B

    return grb.tobytes(), rgb_colors
