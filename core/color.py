"""색상 처리 — 다운샘플, 감마, 비선형 채널 믹싱, WB, 밝기

[ADR-037] compute_led_colors() 호환 래퍼 제거 (REMOVE)

순수 numpy + cv2 모듈. Qt 의존성 없음.
"""

import numpy as np
import cv2

from core.color_correction import ColorCorrection


def downsample_frame(frame, grid_rows, grid_cols):
    """프레임을 grid 크기로 다운샘플 → (cells, 3) float32"""
    small = cv2.resize(frame, (grid_cols, grid_rows), interpolation=cv2.INTER_LINEAR)
    return small.reshape(-1, 3).astype(np.float32)


class ColorPipeline:
    """프레임별 색상 연산 파이프라인 — 설정값 캐싱 + LUT 기반.

    감마·WB·채널 믹싱 파라미터는 ColorCorrection에 위임.
    ColorPipeline은 그 위에 밝기·스무딩·다운샘플·행렬곱·GRB 변환을 추가.
    """

    def __init__(self, weight_matrix, color_cfg, mirror_cfg):
        self.weight_matrix = weight_matrix
        self.grid_rows = mirror_cfg["grid_rows"]
        self.grid_cols = mirror_cfg["grid_cols"]
        self.n_leds = weight_matrix.shape[0]

        self.smoothing = mirror_cfg["smoothing_factor"]
        self.smoothing_enabled = True

        self.green_red_bleed = color_cfg["green_red_bleed"]
        self.brightness = mirror_cfg["brightness"]

        self._grb_buf = np.empty((self.n_leds, 3), dtype=np.uint8)

        self._cc = ColorCorrection(color_cfg)
        self._build_lut(color_cfg, self.brightness)

    def _build_lut(self, color_cfg, brightness):
        """채널별 LUT 생성."""
        self._cc.rebuild(color_cfg)

        self._lut_r_gamma = self._cc.lut_r
        self._lut_g_gamma = self._cc.lut_g

        wb_bright_b = color_cfg.get("wb_b", 1.0) * brightness
        b_full = self._cc.lut_b * wb_bright_b
        self._lut_b_full = np.clip(b_full, 0, 255).astype(np.float32)

        self._wb_bright_r = np.float32(color_cfg.get("wb_r", 1.0) * brightness)
        self._wb_bright_g = np.float32(color_cfg.get("wb_g", 1.0) * brightness)

        self.green_red_bleed = color_cfg.get("green_red_bleed", 0.0)

    def rebuild_lut(self, color_cfg=None, brightness=None):
        """색상 파라미터 또는 밝기 변경 시 LUT 재빌드."""
        if brightness is not None:
            self.brightness = brightness
        if color_cfg is not None:
            self.green_red_bleed = color_cfg["green_red_bleed"]
            self._build_lut(color_cfg, self.brightness)

    def update_brightness(self, brightness):
        """밝기만 변경 — LUT의 WB·밝기 스칼라만 재계산."""
        if self.brightness == brightness:
            return
        old = self.brightness if self.brightness > 0 else 1.0
        ratio = np.float32(brightness / old)
        self._wb_bright_r *= ratio
        self._wb_bright_g *= ratio
        self._lut_b_full = np.clip(self._lut_b_full * ratio, 0, 255)
        self.brightness = brightness

    def update_smoothing(self, factor, enabled=True):
        self.smoothing = factor
        self.smoothing_enabled = enabled

    def process(self, frame, prev_colors=None):
        """프레임 → GRB bytes + RGB float."""
        # 1. 다운샘플 + 행렬곱
        grid = cv2.resize(frame, (self.grid_cols, self.grid_rows),
                          interpolation=cv2.INTER_LINEAR)
        grid_flat = grid.reshape(-1, 3).astype(np.float32)
        rgb = self.weight_matrix @ grid_flat

        # 2. 감마 (LUT take)
        idx = np.clip(rgb, 0, 255).astype(np.uint8)
        R = np.take(self._lut_r_gamma, idx[:, 0])
        G = np.take(self._lut_g_gamma, idx[:, 1])
        B = np.take(self._lut_b_full,  idx[:, 2])

        # 3. 채널 믹싱 (G→R bleed)
        if self.green_red_bleed > 0:
            bleed = np.maximum(0, G - R)
            bleed *= self.green_red_bleed
            R += bleed

        # 4. WB + 밝기 (R, G만)
        R *= self._wb_bright_r
        G *= self._wb_bright_g

        # 5. rgb 배열 재조립
        rgb[:, 0] = R
        rgb[:, 1] = G
        rgb[:, 2] = B

        # 6. 스무딩
        if prev_colors is not None and self.smoothing_enabled and self.smoothing > 0:
            rgb *= (1.0 - self.smoothing)
            rgb += prev_colors * self.smoothing

        # 7. clamp + GRB 변환
        np.clip(rgb, 0, 255, out=rgb)
        rgb_u8 = rgb.astype(np.uint8)

        grb = self._grb_buf
        grb[:, 0] = rgb_u8[:, 1]  # G
        grb[:, 1] = rgb_u8[:, 0]  # R
        grb[:, 2] = rgb_u8[:, 2]  # B

        return grb.tobytes(), rgb

    def process_raw(self, raw_rgb, prev_colors=None):
        """이미 weight_matrix를 거친 raw RGB에 보정 파이프라인 적용.
 
        process()와 동일한 감마/WB/밝기/채널믹싱/스무딩/GRB 변환을 적용하되,
        다운샘플과 행렬곱 단계를 건너뜀.
 
        미러링 그라데이션 효과에서 사용:
        weight_matrix 결과에 HSV 변조를 적용한 후,
        process()와 동일한 보정 경로를 보장.
 
        Args:
            raw_rgb: (n_leds, 3) float32 — RGB 0~255 (변조된 LED 색상)
            prev_colors: (n_leds, 3) float32 또는 None — 이전 프레임 (스무딩용)
 
        Returns:
            (grb_bytes, rgb_float) — process()와 동일한 형식
        """
        rgb = raw_rgb.copy()
 
        # 2. 감마 (LUT take)
        idx = np.clip(rgb, 0, 255).astype(np.uint8)
        R = np.take(self._lut_r_gamma, idx[:, 0])
        G = np.take(self._lut_g_gamma, idx[:, 1])
        B = np.take(self._lut_b_full,  idx[:, 2])
 
        # 3. 채널 믹싱 (G→R bleed)
        if self.green_red_bleed > 0:
            bleed = np.maximum(0, G - R)
            bleed *= self.green_red_bleed
            R += bleed
 
        # 4. WB + 밝기 (R, G만)
        R *= self._wb_bright_r
        G *= self._wb_bright_g
 
        # 5. rgb 배열 재조립
        rgb[:, 0] = R
        rgb[:, 1] = G
        rgb[:, 2] = B
 
        # 6. 스무딩
        if prev_colors is not None and self.smoothing_enabled and self.smoothing > 0:
            rgb *= (1.0 - self.smoothing)
            rgb += prev_colors * self.smoothing
 
        # 7. clamp + GRB 변환
        np.clip(rgb, 0, 255, out=rgb)
        rgb_u8 = rgb.astype(np.uint8)
 
        grb = self._grb_buf
        grb[:, 0] = rgb_u8[:, 1]  # G
        grb[:, 1] = rgb_u8[:, 0]  # R
        grb[:, 2] = rgb_u8[:, 2]  # B
 
        return grb.tobytes(), rgb