# Nanoleaf Multi-Wrap Mirror (PySide6)

Nanoleaf 4D LED 스트립을 모니터 뒷면에 여러 겹 감아 화면 색상을 실시간 미러링하는 Windows 앱.

PyQt5 → PySide6 리라이트 브랜치입니다.

## 주요 변경 사항 (vs. PyQt5 원본)

### 아키텍처 개선
- **ADR-003**: frozen dataclass 파라미터 스냅샷 (`MirrorParams`/`AudioParams`) — GIL 의존 제거
- **ADR-005**: 모니터 워처를 persistent daemon thread로 변경
- **ADR-014**: 오디오 렌더링 벡터화 — per-LED Python 루프 제거
- **ADR-015**: `BaseEngine` + `MirrorEngine`/`AudioModeEngine`/`HybridEngine` 서브클래스
- **ADR-019**: `EngineController` 중재자 — MainWindow 릴레이 슬롯 13개→3개
- **ADR-029**: PySide6 빌트인 High DPI — 수동 DPI 코드 40줄 제거
- **ADR-032**: Registry Run key로 시작프로그램 등록 — PowerShell 의존성 제거
- **ADR-036/037**: `ScreenSampler`, `compute_led_colors()` 래퍼 제거
- **ADR-039**: 트레이 밝기를 시그널로 분리 — 위젯 직접 접근 제거

### 기능 동일
미러링, 오디오 비주얼라이저, 하이브리드 모드, LED 캘리브레이션,
색상 보정, 시스템 트레이, 글로벌 핫키, 잠금 감지, 디스플레이 변경 대응

## 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# Windows 전용 패키지 (수동)
pip install dxcam PyAudioWPatch

# 실행
python main.py
```

## 네이티브 캡처 (선택)

CPU 사용률을 ~1%로 줄이려면:
1. Visual Studio (C++ 데스크톱 개발 워크로드) 설치
2. `native/build.bat` 실행 → `fast_capture.dll` 생성
3. DLL을 프로젝트 루트에 복사

DLL 없이도 dxcam 폴백으로 정상 동작합니다.

## 테스트

```bash
# 전체 테스트 (103 tests)
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -v

# Windows에서
python -m pytest tests/ -v
```
