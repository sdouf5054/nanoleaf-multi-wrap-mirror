"""색상 처리 — 다운샘플, 감마, 비선형 채널 믹싱, WB, 밝기

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


def downsample_frame(frame, grid_rows, grid_cols):
    """프레임을 grid 크기로 다운샘플 → (cells, 3) float32"""
    small = cv2.resize(frame, (grid_cols, grid_rows), interpolation=cv2.INTER_LINEAR)
    return small.reshape(-1, 3).astype(np.float32)


class ColorPipeline:
    """프레임별 색상 연산 파이프라인 — 설정값 캐싱 + LUT 기반.

    매 프레임 config dict를 참조하는 오버헤드를 제거하고,
    감마·WB·밝기를 하나의 채널별 LUT로 통합합니다.

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

        # 채널 믹싱 계수
        self.green_red_bleed = color_cfg["green_red_bleed"]

        # 밝기
        self.brightness = mirror_cfg["brightness"]

        # GRB 출력 버퍼 (매 프레임 재할당 방지)
        self._grb_buf = np.empty((self.n_leds, 3), dtype=np.uint8)

        # LUT 빌드: 0~255 입력 → 감마·WB·밝기 적용된 0~255 출력
        self._build_lut(color_cfg, self.brightness)

    def _build_lut(self, color_cfg, brightness):
        """채널별 256-entry LUT 생성.

        LUT[ch][v] = clamp(((v/255)^gamma * wb * brightness) * 255, 0, 255)

        이렇게 하면 매 프레임의 감마·WB·밝기 연산이
        단순 테이블 룩업(np.take)으로 대체됩니다.

        ★ 채널 믹싱(green_red_bleed)은 R/G 상호의존이라 LUT 불가
           → process()에서 별도 처리 (하지만 np.maximum + 곱셈 1회로 경량)
        """
        x = np.arange(256, dtype=np.float32) / 255.0

        gamma_r = color_cfg["gamma_r"]
        gamma_g = color_cfg["gamma_g"]
        gamma_b = color_cfg["gamma_b"]

        # 감마 적용
        r_curve = np.power(x, gamma_r) if gamma_r != 1.0 else x.copy()
        g_curve = np.power(x, gamma_g) if gamma_g != 1.0 else x.copy()
        b_curve = np.power(x, gamma_b) if gamma_b != 1.0 else x.copy()

        # WB + 밝기를 한번에 곱
        wb_bright_r = color_cfg["wb_r"] * brightness
        wb_bright_g = color_cfg["wb_g"] * brightness
        wb_bright_b = color_cfg["wb_b"] * brightness

        r_curve *= wb_bright_r * 255.0
        g_curve *= wb_bright_g * 255.0
        b_curve *= wb_bright_b * 255.0

        # clamp + uint8
        self._lut_r = np.clip(r_curve, 0, 255).astype(np.uint8)
        self._lut_g = np.clip(g_curve, 0, 255).astype(np.uint8)
        self._lut_b = np.clip(b_curve, 0, 255).astype(np.uint8)

        # ★ 채널 믹싱용: R 채널에 bleed 추가분을 적용하기 위해
        # LUT만으로는 G값에 의존하는 R 보정이 불가능하므로,
        # bleed 적용 후 WB·밝기·감마가 적용된 R값이 필요함.
        # → bleed는 감마 적용 '후'의 값에서 계산하므로,
        #   R채널 LUT는 WB·밝기만 적용하고, 감마+bleed는 process()에서 처리?
        #
        # 아니, 더 나은 접근: 원본 코드 순서를 보면
        #   1) 행렬곱 (float) → 2) 감마 → 3) 채널 믹싱 → 4) WB → 5) 밝기
        # 채널 믹싱이 감마와 WB 사이에 있으므로, R채널은 LUT로 완전히 통합 불가.
        #
        # 전략 변경: 채널 믹싱이 있을 때는 R채널만 2단계 LUT 사용
        #   - lut_r_pre: 감마만 적용 (float LUT)
        #   - lut_r_post: WB·밝기만 적용 (채널 믹싱 후)
        #   - G, B: 감마+WB+밝기 완전 통합 LUT
        #
        # 하지만 이러면 R채널에 float 연산이 남아서 이점이 줄어듦.
        # 실측해보면 green_red_bleed가 있는 경우가 대부분이므로...
        #
        # ★ 최종 전략: LUT를 감마 후 단계까지만 적용 (uint8),
        #   채널 믹싱은 uint8 상태에서 정수 연산,
        #   WB·밝기는 고정소수점 정수 연산으로 처리.
        #   → 실제로 float 연산 0회 달성 (행렬곱 제외)
        #
        # ... 하지만 이렇게까지 하면 코드 복잡도가 너무 올라감.
        # 현실적 타협: 전체를 하나의 효율적인 float 파이프라인으로 유지하되,
        # 불필요한 배열 생성/복사를 최소화하는 방향.

        # === 현실적 최적화: 3채널 통합 LUT (float32) ===
        # 채널 믹싱이 있으므로 중간에 float가 필요 → float LUT로 통합
        # np.power를 매 프레임 75개 LED에 대해 호출하는 것 vs
        # 256-entry LUT에서 take하는 것 → LUT가 확실히 빠름

        # R채널: 감마만 (채널 믹싱 후 WB·밝기 별도)
        self._lut_r_gamma = (np.power(x, gamma_r) * 255.0).astype(np.float32) \
            if gamma_r != 1.0 else (x * 255.0).astype(np.float32)

        # G채널: 감마만 (채널 믹싱에서 G값 참조 필요)
        self._lut_g_gamma = (np.power(x, gamma_g) * 255.0).astype(np.float32) \
            if gamma_g != 1.0 else (x * 255.0).astype(np.float32)

        # B채널: 감마+WB+밝기 완전 통합 (채널 믹싱에 무관)
        b_full = np.power(x, gamma_b) * wb_bright_b * 255.0 if gamma_b != 1.0 \
            else x * wb_bright_b * 255.0
        self._lut_b_full = np.clip(b_full, 0, 255).astype(np.float32)

        # WB·밝기 스칼라 (R, G 채널용 — 채널 믹싱 후 적용)
        self._wb_bright_r = np.float32(wb_bright_r)
        self._wb_bright_g = np.float32(wb_bright_g)

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
        #    rgb 값을 0~255 범위의 uint8 인덱스로 변환하여 LUT 참조
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
