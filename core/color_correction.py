"""색상 보정 — 감마·채널 믹싱·화이트밸런스 (단일 소스)

[목적]
ColorPipeline(미러링), AudioVisualizer, HybridVisualizer에서
각각 복사·유지하던 색상 보정 로직을 하나로 통합합니다.

[보정 순서] (모든 모드에서 동일)
1. 감마 — 채널별 256-entry LUT (np.power 대체)
2. 채널 믹싱 — Green → Red bleed (비선형)
3. 화이트밸런스 — 채널별 스칼라 곱

[사용법]
    cc = ColorCorrection(color_cfg)
    cc.apply(leds)  # (n_leds, 3) float32, 0~255, in-place

    # 파라미터 변경 시:
    cc.rebuild(new_color_cfg)

[ColorPipeline과의 관계]
ColorPipeline은 이 모듈 위에 다운샘플·행렬곱·스무딩·밝기·GRB 변환을
추가한 상위 파이프라인입니다. ColorPipeline._build_lut()에서
이 모듈의 LUT를 내부적으로 재활용합니다.
"""

import numpy as np


class ColorCorrection:
    """감마·채널 믹싱·WB 보정 — LUT 기반.

    Attributes:
        enabled: False로 설정하면 apply()가 no-op
        green_red_bleed: G→R bleed 계수 (0=없음)
        wb_r, wb_g, wb_b: 화이트밸런스 스칼라
        lut_r, lut_g, lut_b: 감마 LUT (float32, 256 entries, 0~255 출력)
    """

    def __init__(self, color_cfg=None):
        self.enabled = True

        # LUT (감마만 — WB·밝기는 별도 적용)
        self.lut_r = None  # (256,) float32
        self.lut_g = None
        self.lut_b = None

        # WB 스칼라
        self.wb_r = np.float32(1.0)
        self.wb_g = np.float32(1.0)
        self.wb_b = np.float32(1.0)

        # 채널 믹싱
        self.green_red_bleed = np.float32(0.0)

        if color_cfg is not None:
            self.rebuild(color_cfg)

    def rebuild(self, color_cfg):
        """color_cfg dict에서 파라미터를 읽어 LUT + 스칼라를 재빌드.

        Args:
            color_cfg: dict with keys:
                gamma_r, gamma_g, gamma_b (float, default 1.0)
                wb_r, wb_g, wb_b (float, default 1.0)
                green_red_bleed (float, default 0.0)
        """
        x = np.arange(256, dtype=np.float32) / 255.0

        gamma_r = color_cfg.get("gamma_r", 1.0)
        gamma_g = color_cfg.get("gamma_g", 1.0)
        gamma_b = color_cfg.get("gamma_b", 1.0)

        self.lut_r = (
            (np.power(x, gamma_r) * 255.0).astype(np.float32)
            if gamma_r != 1.0
            else (x * 255.0).astype(np.float32)
        )
        self.lut_g = (
            (np.power(x, gamma_g) * 255.0).astype(np.float32)
            if gamma_g != 1.0
            else (x * 255.0).astype(np.float32)
        )
        self.lut_b = (
            (np.power(x, gamma_b) * 255.0).astype(np.float32)
            if gamma_b != 1.0
            else (x * 255.0).astype(np.float32)
        )

        self.wb_r = np.float32(color_cfg.get("wb_r", 1.0))
        self.wb_g = np.float32(color_cfg.get("wb_g", 1.0))
        self.wb_b = np.float32(color_cfg.get("wb_b", 1.0))
        self.green_red_bleed = np.float32(color_cfg.get("green_red_bleed", 0.0))

    def apply(self, leds):
        """LED RGB 배열에 감마→채널 믹싱→WB 보정 적용 (in-place).

        Args:
            leds: (n_leds, 3) float32, 0~255 범위

        Returns:
            leds: 보정된 배열 (동일 객체, in-place 수정)
        """
        if not self.enabled or self.lut_r is None:
            return leds

        # 1. 감마 (LUT take)
        idx = np.clip(leds, 0, 255).astype(np.uint8)
        R = np.take(self.lut_r, idx[:, 0])
        G = np.take(self.lut_g, idx[:, 1])
        B = np.take(self.lut_b, idx[:, 2])

        # 2. 채널 믹싱 (G→R bleed)
        if self.green_red_bleed > 0:
            bleed = np.maximum(0, G - R)
            bleed *= self.green_red_bleed
            R += bleed

        # 3. 화이트밸런스
        R *= self.wb_r
        G *= self.wb_g
        B *= self.wb_b

        # 재조립
        leds[:, 0] = R
        leds[:, 1] = G
        leds[:, 2] = B

        return leds
