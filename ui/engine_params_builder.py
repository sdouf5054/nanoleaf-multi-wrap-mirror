"""엔진 파라미터 빌더 — UI 위젯 값 → EngineParams 변환

tab_control.py에서 분리. UI 위젯에 직접 의존하지 않고,
collect 함수들이 반환한 dict를 받아 EngineParams를 빌드합니다.

이점:
  - QWidget 없이 단위 테스트 가능
  - tab_control.py가 UI 조립에만 집중
  - 파라미터 변환 로직을 한 곳에서 관리

사용법:
    builder = EngineParamsBuilder()
    ep = builder.build(
        display_enabled=True,
        audio_enabled=False,
        master_brightness=0.8,
        display_params=section_mirror.collect_params(),
        color_params=section_color.collect_params(),
        audio_params=section_audio.collect_params(),
    )
"""

from core.engine_params import EngineParams


class EngineParamsBuilder:
    """UI에서 수집한 dict → EngineParams 변환.

    각 섹션 패널의 collect_params()가 반환한 dict를 합쳐서
    EngineParams 필드에 맞는 키만 필터링합니다.
    """

    # EngineParams의 유효 필드 이름 (클래스 초기화 시 한 번만 계산)
    _VALID_FIELDS = {f.name for f in EngineParams.__dataclass_fields__.values()}

    def build(self, display_enabled, audio_enabled, master_brightness,
              display_params=None, color_params=None, audio_params=None):
        """EngineParams를 빌드합니다.

        Args:
            display_enabled: bool — 디스플레이 토글 상태
            audio_enabled: bool — 오디오 토글 상태
            master_brightness: float — 0~1
            display_params: dict — DisplayMirrorSection.collect_params() (D=ON)
            color_params: dict — DisplayColorSection.collect_params() (D=OFF)
            audio_params: dict — AudioReactiveSection.collect_params() (A=ON)

        Returns:
            EngineParams
        """
        raw = {
            "display_enabled": display_enabled,
            "audio_enabled": audio_enabled,
            "master_brightness": master_brightness,
        }

        # 디스플레이 ON → 미러링 파라미터, OFF → 색상 파라미터
        if display_enabled and display_params:
            raw.update(display_params)
        elif not display_enabled and color_params:
            raw.update(color_params)

        # 오디오 ON → 오디오 파라미터
        if audio_enabled and audio_params:
            raw.update(audio_params)

        # EngineParams 필드에 있는 키만 필터링
        filtered = {k: v for k, v in raw.items() if k in self._VALID_FIELDS}
        return EngineParams(**filtered)

    def build_from_collected(self, collected_params):
        """이미 합쳐진 dict에서 EngineParams를 빌드합니다.

        collect_engine_init_params()의 결과를 직접 전달할 때 사용.
        """
        filtered = {k: v for k, v in collected_params.items()
                    if k in self._VALID_FIELDS}
        return EngineParams(**filtered)
