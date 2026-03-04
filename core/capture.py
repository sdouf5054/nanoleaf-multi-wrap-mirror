"""화면 캡처 — dxcam 래퍼 (부팅 시 지연 대응)

[최적화 변경 사항]
기존 방식: camera.grab() 폴링
  - 미러링 루프가 매 프레임마다 직접 GPU 메모리에 접근하여 캡처 요청
  - 캡처 자체에 OS 레벨 비용 발생, 모니터 주사율(120Hz)에 끌려가는 구조

변경 방식: camera.start(target_fps) + get_latest_frame() 연속 캡처 모드
  - dxcam 내부 백그라운드 스레드가 target_fps에 맞춰 캡처를 독립 수행
  - Windows 고해상도 타이머로 정밀하게 캡처 주기를 제어하여 불필요한 캡처 차단
  - 미러링 루프는 버퍼에서 최신 프레임만 꺼내면 되므로 grab() 비용 제거
"""

import dxcam
import time
import numpy as np


class ScreenCapture:
    def __init__(self, monitor_index=0):
        self.camera = None
        self.monitor_index = monitor_index
        self.screen_w = 0
        self.screen_h = 0
        self.last_frame = None
        self._started = False  # 연속 캡처 모드 활성화 여부

    def start(self, max_wait=30, target_fps=60):
        """캡처 초기화. 유효한 프레임이 잡힐 때까지 대기.

        부팅 직후 화면이 아직 준비 안 된 경우 최대 max_wait초 대기.
        dxcam이 죽으면 재생성 시도.

        Args:
            max_wait: 초기화 최대 대기 시간(초)
            target_fps: 연속 캡처 목표 FPS — 이 값으로 백그라운드 캡처 주기를 고정.
                        미러링 루프의 Target FPS와 같거나 약간 높게 설정하면
                        불필요한 초과 캡처를 막아 CPU 부하를 낮출 수 있음.
        """
        self._target_fps = target_fps
        deadline = time.time() + max_wait
        attempt = 0

        while time.time() < deadline:
            try:
                if self.camera is None:
                    # max_buffer_len=2 를 추가하여 메모리 점유율 대폭 축소
                    self.camera = dxcam.create(device_idx=0, output_idx=self.monitor_index, max_buffer_len=2)
                    attempt += 1

                # 해상도 확인용으로 grab() 1회 사용
                f = self.camera.grab()
                if f is not None and f.mean() > 0:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    # ★ 연속 캡처 모드 시작 — target_fps로 캡처 주기 고정
                    self._start_continuous(target_fps)
                    return True

                time.sleep(0.5)

            except Exception:
                self._destroy_camera()
                time.sleep(1.0)

        # 최후 수단
        if self.camera is not None:
            for _ in range(5):
                f = self.camera.grab()
                if f is not None:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._start_continuous(target_fps)
                    return True
                time.sleep(0.2)

        self.screen_w, self.screen_h = 2560, 1440
        return True

    def _start_continuous(self, target_fps):
        """dxcam 연속 캡처 모드 시작"""
        if not self._started:
            try:
                self.camera.start(target_fps=target_fps, video_mode=False)
                self._started = True
            except Exception:
                # start() 실패 시 기존 grab() 폴링으로 폴백
                self._started = False

    def grab(self):
        """최신 프레임 반환.

        연속 캡처 모드: get_latest_frame()으로 버퍼에서 즉시 꺼냄 (저비용)
        폴백 모드: 기존 grab() 폴링
        """
        try:
            if self._started:
                # ★ 버퍼에서 최신 프레임만 꺼냄 — GPU 직접 접근 없음
                frame = self.camera.get_latest_frame()
            else:
                frame = self.camera.grab()

            if frame is not None:
                self.last_frame = frame
                return frame
            return self.last_frame
        except Exception:
            self._recreate()
            return self.last_frame

    def _recreate(self):
        """dxcam 재생성 및 모니터 구성 변경 대응.

        디스플레이 구성이 물리적으로 바뀐 경우(주 모니터 변경, 전원 OFF→ON 등)
        dxcam 내부 장치 연결이 끊어져 Exception이 발생할 수 있음.
        재생성 후 첫 프레임을 뽑아 해상도 정보까지 함께 갱신.
        """
        self._destroy_camera()
        try:
            self.camera = dxcam.create(
                device_idx=0, output_idx=self.monitor_index, max_buffer_len=2
            )
            # ★ 재생성 후 첫 프레임으로 해상도 정보 즉시 갱신
            f = self.camera.grab()
            if f is not None:
                self.screen_h, self.screen_w = f.shape[:2]
                self.last_frame = f

            # 연속 캡처 모드 재시작
            self._start_continuous(getattr(self, '_target_fps', 60))
        except Exception:
            pass

    def _destroy_camera(self):
        if self.camera is not None:
            try:
                if self._started:
                    self.camera.stop()
            except Exception:
                pass
            try:
                del self.camera
            except Exception:
                pass
            self.camera = None
            self._started = False

    def stop(self):
        self._destroy_camera()
