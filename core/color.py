"""색상 처리 — 다운샘플, 감마, 비선형 채널 믹싱, WB, 밝기

[변경 사항 v4 — ColorCorrection 통합]
- ★ core.color_correction.ColorCorrection을 내부에서 활용
  감마 LUT 빌드 + 보정 파라미터를 ColorCorrection에 위임
  ColorPipeline 고유의 밝기·스무딩·GRB 변환은 그대로 유지
- 기존 인터페이스 100% 호환 (rebuild_lut, update_brightness, process 등)

[변경 사항 v3 — CPU 최적화]
- ColorPipeline 클래스: 매 프레임 반복되는 설정값 참조를 __init__에서 캐싱
- downsample: INTER_LINEAR + 직접 float32 reshape (중간 복사 제거)
- 감마/WB/밝기를 LUT(Look-Up Table) 기반으로 전환 → np.power 제거
- GRB 변환: fancy index 대신 flat view 슬라이싱
- compute_led_colors → pipeline.process() 로 호출
- 하위 호환: compute_led_colors() 함수도 유지 (기존 CLI 등)
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

    ★ v4: 감마·WB·채널 믹싱 파라미터는 ColorCorrection에 위임.
    ColorPipeline은 그 위에 밝기·스무딩·다운샘플·행렬곱·GRB 변환을 추가.

    Usage:
        pipeline = ColorPipeline(weight_matrix, color_cfg, mirror_cfg)
        grb_bytes, rgb = pipeline.process(frame, prev_colors)

        # 설정 변경 시:
        pipeline.update_brightness(0.8)
        pipeline.update_smoothing(0.5, enabled=True)
        pipeline.rebuild_lut(color_cfg, brightness)  # 색상 파라미터 변경 시
    """

    def __init__(self, weight_matrix, color_cfg, mirror_cfg):
        self.weight_matrix = weight_matrix
        self.grid_rows = mirror_cfg["grid_rows"]
        self.grid_cols = mirror_cfg["grid_cols"]
        self.n_leds = weight_matrix.shape[0]

        # 스무딩
        self.smoothing = mirror_cfg["smoothing_factor"]
        self.smoothing_enabled = True

        # 채널 믹싱 계수 (ColorCorrection에도 저장되지만, 호환성 위해 유지)
        self.green_red_bleed = color_cfg["green_red_bleed"]

        # 밝기
        self.brightness = mirror_cfg["brightness"]

        # GRB 출력 버퍼 (매 프레임 재할당 방지)
        self._grb_buf = np.empty((self.n_leds, 3), dtype=np.uint8)

        # ★ ColorCorrection 인스턴스 — 감마 LUT + WB + 채널 믹싱
        self._cc = ColorCorrection(color_cfg)

        # LUT 빌드: ColorCorrection의 감마 LUT를 기반으로
        # 밝기·WB가 통합된 미러링 전용 LUT도 생성
        self._build_lut(color_cfg, self.brightness)

    def _build_lut(self, color_cfg, brightness):
        """채널별 LUT 생성 — ColorCorrection의 감마 LUT를 기반으로
        밝기·WB를 추가 적용한 미러링 전용 LUT.

        B 채널: 감마+WB+밝기 완전 통합 (채널 믹싱에 무관)
        R, G 채널: 감마만 (채널 믹싱 후 WB·밝기 별도 적용)
        """
        # ColorCorrection 재빌드 (감마 LUT + WB + bleed 파라미터)
        self._cc.rebuild(color_cfg)

        # R, G: 감마 LUT 참조 (채널 믹싱에서 상호 의존하므로 WB·밝기는 후처리)
        self._lut_r_gamma = self._cc.lut_r  # float32 (256,)
        self._lut_g_gamma = self._cc.lut_g  # float32 (256,)

        # B: 감마+WB+밝기 완전 통합 LUT (채널 믹싱에 무관하므로 한 번에 처리 가능)
        wb_bright_b = color_cfg.get("wb_b", 1.0) * brightness
        b_full = self._cc.lut_b * wb_bright_b
        self._lut_b_full = np.clip(b_full, 0, 255).astype(np.float32)

        # WB·밝기 스칼라 (R, G 채널용 — 채널 믹싱 후 적용)
        self._wb_bright_r = np.float32(color_cfg.get("wb_r", 1.0) * brightness)
        self._wb_bright_g = np.float32(color_cfg.get("wb_g", 1.0) * brightness)

        self.green_red_bleed = color_cfg.get("green_red_bleed", 0.0)

    def rebuild_lut(self, color_cfg=None, brightness=None):
        """색상 파라미터 또는 밝기 변경 시 LUT 재빌드.

        color_cfg가 None이면 brightness만 업데이트합니다.
        """
        if brightness is not None:
            self.brightness = brightness
        if color_cfg is not None:
            self.green_red_bleed = color_cfg["green_red_bleed"]
        # color_cfg가 없으면 이전 값 기반으로 재빌드할 수 없으므로
        # 호출부에서 항상 color_cfg를 전달하는 것을 권장
        if color_cfg is not None:
            self._build_lut(color_cfg, self.brightness)

    def update_brightness(self, brightness):
        """밝기만 변경 — LUT의 WB·밝기 스칼라만 재계산.

        ★ 전체 LUT를 재빌드하지 않고 스칼라 비율만 조정.
        """
        if self.brightness == brightness:
            return
        old = self.brightness if self.brightness > 0 else 1.0
        ratio = np.float32(brightness / old)
        self._wb_bright_r *= ratio
        self._wb_bright_g *= ratio
        # B 채널 LUT는 밝기가 포함되어 있으므로 비례 조정
        self._lut_b_full = np.clip(self._lut_b_full * ratio, 0, 255)
        self.brightness = brightness

    def update_smoothing(self, factor, enabled=True):
        self.smoothing = factor
        self.smoothing_enabled = enabled

    def process(self, frame, prev_colors=None):
        """프레임 → GRB bytes + RGB float.

        최적화 포인트:
        - downsample은 한 번만 (cv2.resize → reshape → float32)
        - 행렬곱 1회 (가장 무거운 연산 — 이건 줄일 수 없음)
        - 감마: LUT take (np.power 대비 ~5-10x 빠름)
        - 채널 믹싱: in-place 벡터 연산
        - WB·밝기: 스칼라 곱 (in-place)
        - 스무딩: in-place lerp
        - GRB 변환: 사전 할당 버퍼에 슬라이스 복사
        """
        # 1. 다운샘플 + 행렬곱
        grid = cv2.resize(frame, (self.grid_cols, self.grid_rows),
                          interpolation=cv2.INTER_LINEAR)
        grid_flat = grid.reshape(-1, 3).astype(np.float32)
        rgb = self.weight_matrix @ grid_flat  # (n_leds, 3)

        # 2. 감마 (LUT take — np.power 대체)
        idx = np.clip(rgb, 0, 255).astype(np.uint8)
        R = np.take(self._lut_r_gamma, idx[:, 0])  # float32 (n_leds,)
        G = np.take(self._lut_g_gamma, idx[:, 1])  # float32
        B = np.take(self._lut_b_full,  idx[:, 2])  # float32 (감마+WB+밝기 완료)

        # 3. 채널 믹싱 (G→R bleed) — in-place
        if self.green_red_bleed > 0:
            bleed = np.maximum(0, G - R)
            bleed *= self.green_red_bleed
            R += bleed

        # 4. WB + 밝기 (R, G만 — B는 LUT에서 완료)
        R *= self._wb_bright_r
        G *= self._wb_bright_g

        # 5. rgb 배열 재조립 (in-place)
        rgb[:, 0] = R
        rgb[:, 1] = G
        rgb[:, 2] = B

        # 6. 스무딩
        if prev_colors is not None and self.smoothing_enabled and self.smoothing > 0:
            rgb *= (1.0 - self.smoothing)
            rgb += prev_colors * self.smoothing

        # 7. clamp + GRB 변환 (사전 할당 버퍼)
        np.clip(rgb, 0, 255, out=rgb)
        rgb_u8 = rgb.astype(np.uint8)

        grb = self._grb_buf
        grb[:, 0] = rgb_u8[:, 1]  # G
        grb[:, 1] = rgb_u8[:, 0]  # R
        grb[:, 2] = rgb_u8[:, 2]  # B

        return grb.tobytes(), rgb


# ── 하위 호환 함수 (CLI 등에서 사용) ─────────────────────────────────

def compute_led_colors(frame, weight_matrix, color_cfg, mirror_cfg,
                       prev_colors=None):
    """기존 인터페이스 호환 래퍼.

    ★ 매 호출마다 Pipeline을 생성하므로 LUT 캐싱 이점이 없음.
    성능이 중요한 경우 ColorPipeline을 직접 사용하세요.
    """
    pipeline = ColorPipeline(weight_matrix, color_cfg, mirror_cfg)
    return pipeline.process(frame, prev_colors)
