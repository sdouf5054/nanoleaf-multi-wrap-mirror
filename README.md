# Nanoleaf Multi-Wrap Mirror (PySide6)

Nanoleaf 4D LED 스트립을 자르지 않고 모니터 뒷면에 **여러 겹 감아** 화면 색상을 실시간으로 미러링하는 Windows 앱입니다.

> 24인치 모니터 기준, LED를 자르지 않고 두 바퀴 감는 레이아웃을 지원합니다.

---

## 주요 기능

- **화면 미러링** — DXGI Desktop Duplication으로 화면 색상을 실시간으로 LED에 반영
- **오디오 반응** — WASAPI Loopback + FFT로 음악에 맞춰 LED 반응 (Pulse / Spectrum / Bass Detail / Wave / Dynamic / Flowing)
- **하이브리드 모드** — 화면 색 + 오디오 반응 동시 활성
- **미디어 연동** — 재생 중인 음악의 앨범 아트를 LED에 반영 (SMTC)
- **색상 보정** — 감마, 화이트밸런스, 채널 믹싱 개별 조정
- **글로벌 핫키** — 앱 포커스 없이 On/Off, 밝기 조절
- **시스템 트레이** — 백그라운드 상주, 최소화 지원
- **Windows 시작프로그램** — 부팅 시 트레이로 자동 실행

---

## 요구사항

- Windows 10/11
- Python 3.11+
- Nanoleaf 4D LED 스트립 (USB HID 연결)

---

## 설치

```bash
# 기본 의존성
pip install -r requirements.txt

# Windows 전용 패키지 (수동 설치)
pip install dxcam PyAudioWPatch

# 미디어 연동 기능 (선택)
pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Media
```

## 실행

```bash
python main.py
```

---

## CPU 사용률 줄이기 (선택)

네이티브 캡처 모듈을 빌드하면 CPU 사용률을 약 1% 수준으로 낮출 수 있습니다.

1. [Visual Studio](https://visualstudio.microsoft.com/ko/) 설치 시 **"C++를 사용한 데스크톱 개발"** 워크로드 체크
2. `native/build.bat` 실행 → `fast_capture.dll` 생성
3. DLL을 프로젝트 루트에 복사

DLL이 없으면 dxcam으로 자동 폴백됩니다.

---

## 화면 구성

| 탭 | 설명 |
|---|---|
| **컨트롤** | 미러링 / 오디오 / 미디어 연동 토글, 밝기, 모드별 파라미터 |
| **색상 보정** | 화이트밸런스, 감마, 채널 믹싱 실시간 조정 |
| **LED 설정** | LED 코너 위치 캘리브레이션, 세그먼트 자동 생성 |
| **옵션** | 핫키, 트레이, 시작프로그램 설정 |

---

## 오디오 모드 설명

| 모드 | 설명 |
|---|---|
| **Pulse** | 베이스에 맞춰 전체 밝기가 반응 |
| **Spectrum** | 16밴드 주파수를 LED 둘레에 매핑 |
| **Bass Detail** | 저역(20~500Hz)을 16밴드로 세밀하게 표현 |
| **Wave** | 베이스 온셋마다 펄스가 아래→위로 이동 |
| **Dynamic** | 비트마다 LED 둘레 특정 위치에서 리플 발생 |
| **Flowing** | 화면 색상을 팔레트로 추출해 천천히 순환 (디스플레이 ON 필요) |

---

## 설정 파일

`config.json`이 프로젝트 루트에 자동 생성됩니다. 앱을 통해 저장하는 것을 권장하며, 직접 편집도 가능합니다.

**기기 설정 예시 (`device` 섹션):**
```json
{
  "device": {
    "vendor_id": "0x37FA",
    "product_id": "0x8202",
    "led_count": 75
  }
}
```

Vendor/Product ID는 기기마다 다를 수 있으니 장치 관리자에서 확인하세요.

---

## 테스트

```bash
# 전체 테스트 (103개)
python -m pytest tests/ -v

# Linux / CI 환경
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -v
```

---

## 폴더 구조

```
core/       엔진, 오디오, 색상 처리 등 핵심 로직
ui/         PySide6 GUI
native/     C++ DXGI 캡처 DLL 소스 및 빌드 스크립트
tests/      단위 테스트
assets/     아이콘
```

---

## 라이선스

개인 사용 목적으로 공개