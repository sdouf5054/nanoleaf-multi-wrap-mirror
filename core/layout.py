"""LED 레이아웃 — 위치 계산 + 가중치 행렬 빌드"""

import numpy as np

# 세로 모드 시 변 매핑 회전 (시계방향 90도 회전된 모니터 기준)
# 모니터를 시계방향 90도 회전: 물리적 left → 화면상 top
_ROTATE_CW = {"left": "top", "top": "right", "right": "bottom", "bottom": "left"}
# 반시계방향 90도 회전: 물리적 left → 화면상 bottom
_ROTATE_CCW = {"left": "bottom", "top": "left", "right": "top", "bottom": "right"}


def _rotate_segments(segments, rotation):
    """segments의 side를 회전 방향에 따라 변환"""
    if rotation == "cw":
        rmap = _ROTATE_CW
    elif rotation == "ccw":
        rmap = _ROTATE_CCW
    else:
        return segments

    return [
        {"start": s["start"], "end": s["end"], "side": rmap.get(s["side"], s["side"])}
        for s in segments
    ]


def get_led_positions(screen_w, screen_h, segments, led_count, orientation="auto",
                      portrait_rotation="cw"):
    """
    설정의 segments 정보로 각 LED의 화면 좌표와 변 방향을 계산.

    Args:
        screen_w, screen_h: 화면 해상도
        segments: list of {"start": int, "end": int, "side": str}
        led_count: 총 LED 수
        orientation: "landscape", "portrait", "auto"
        portrait_rotation: "cw" (시계방향) 또는 "ccw" (반시계방향)

    Returns:
        positions: np.array (led_count, 2)
        sides: list of str
    """
    # 세로 모드 감지 및 변 회전
    is_portrait = False
    if orientation == "auto":
        is_portrait = screen_h > screen_w
    elif orientation == "portrait":
        is_portrait = True

    if is_portrait:
        segments = _rotate_segments(segments, portrait_rotation)

    positions = np.zeros((led_count, 2), dtype=np.float32)
    sides = [None] * led_count

    for seg in segments:
        start, end, side = seg["start"], seg["end"], seg["side"]
        n = start - end
        if n <= 0:
            continue
        for i in range(n):
            led_idx = start - i
            t = (i + 0.5) / n
            if side == "left":
                x, y = 0, screen_h * (1 - t)
            elif side == "top":
                x, y = screen_w * t, 0
            elif side == "right":
                x, y = screen_w, screen_h * t
            elif side == "bottom":
                x, y = screen_w * (1 - t), screen_h
            else:
                continue
            if 0 <= led_idx < led_count:
                positions[led_idx] = [x, y]
                sides[led_idx] = side

    # 미할당 LED → 인접 LED에서 복사
    for i in range(led_count):
        if positions[i, 0] == 0 and positions[i, 1] == 0:
            for offset in range(1, led_count):
                if i + offset < led_count and not (
                    positions[i + offset, 0] == 0 and positions[i + offset, 1] == 0
                ):
                    positions[i] = positions[i + offset]
                    sides[i] = sides[i + offset]
                    break
                if i - offset >= 0 and not (
                    positions[i - offset, 0] == 0 and positions[i - offset, 1] == 0
                ):
                    positions[i] = positions[i - offset]
                    sides[i] = sides[i - offset]
                    break

    return positions, sides


def build_weight_matrix(screen_w, screen_h, led_positions, led_sides,
                        grid_cols, grid_rows, decay_radius, parallel_penalty):
    """
    타원형 감쇠 가중치 행렬.
    변에 수직 방향은 넓게, 평행 방향은 좁게.

    decay_radius / parallel_penalty:
        float → 모든 변에 동일 적용
        dict  → {"top": v, "bottom": v, "left": v, "right": v} 변별 값
    """
    n_leds = led_positions.shape[0]
    n_cells = grid_rows * grid_cols

    cell_w = screen_w / grid_cols
    cell_h = screen_h / grid_rows
    cell_centers = np.zeros((n_cells, 2), dtype=np.float32)
    for r in range(grid_rows):
        for c in range(grid_cols):
            cell_centers[r * grid_cols + c] = [(c + 0.5) * cell_w, (r + 0.5) * cell_h]

    diag = np.sqrt(screen_w ** 2 + screen_h ** 2)

    # 변별 값 해석 헬퍼
    def _get(param, side, default):
        if isinstance(param, dict):
            return param.get(side, default)
        return param

    weight_matrix = np.zeros((n_leds, n_cells), dtype=np.float32)

    for i in range(n_leds):
        led_pos = led_positions[i]
        dx = cell_centers[:, 0] - led_pos[0]
        dy = cell_centers[:, 1] - led_pos[1]

        side = led_sides[i]
        pp = _get(parallel_penalty, side, 5.0)
        dr = _get(decay_radius, side, 0.3)
        max_dist = diag * dr

        if side in ("top", "bottom"):
            distances = np.sqrt((dx * pp) ** 2 + dy ** 2)
        elif side in ("left", "right"):
            distances = np.sqrt(dx ** 2 + (dy * pp) ** 2)
        else:
            distances = np.sqrt(dx ** 2 + dy ** 2)

        weights = np.maximum(0, 1.0 - distances / max_dist)

        total = weights.sum()
        if total > 0:
            weights /= total

        weight_matrix[i] = weights

    return weight_matrix
