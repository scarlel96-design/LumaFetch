# Grok Build 전용 지침 — Luma Fetch 1.13.0

이 패키지는 기능 구현과 테스트 작성이 끝난 **빌드 준비본**이다. Grok은 새 기능 개발, 구조 변경, UI 변경, 리팩터링을 하지 말고 Windows 설치 파일 생성과 검증만 수행한다.

## 입력

- GitHub 저장소: `https://github.com/scarlel96-design/LumaFetch`
- 적용 파일: `LumaFetch-1.13.0-BUILD-READY.zip`
- 기준 브랜치: `main`

## 수행 순서

1. 저장소를 Windows x64 환경에 체크아웃한다.
2. ZIP의 내용을 저장소 루트에 덮어쓴다.
3. 기존 저장소의 `installer/LumaFetch.ico`는 반드시 유지한다.
4. 저장소 루트에서 `BUILD_RELEASE.cmd`를 실행한다.
5. 스크립트가 Python 가상환경, 의존성, Inno Setup을 자동으로 준비하도록 둔다.
6. 테스트, PyInstaller, Inno Setup, 버전 검사, SHA-256 검사가 모두 성공해야 한다.
7. 실패 시 애플리케이션 기능 코드는 수정하지 않는다. 환경·경로·패키징 설정에 직접 관련된 최소 수정만 허용하고 처음부터 다시 빌드한다.

## 성공 조건

다음 파일이 모두 존재해야 한다.

```text
outputs/LumaFetch-Setup-1.13.0.exe
outputs/SHA256SUMS.txt
outputs/BUILD_INFO.txt
```

추가로 다음을 확인한다.

- `pytest`: 전체 통과
- `LumaFetch.exe` 파일 버전: `1.13.0.x`
- 설치 파일 버전: `1.13.0.x`
- 설치 파일 크기: 1 MiB 이상
- 설치 파일 이름이 정확히 `LumaFetch-Setup-1.13.0.exe`

## 최종 전달

- `LumaFetch-Setup-1.13.0.exe`
- SHA-256 값
- 테스트 통과 개수
- 빌드 환경의 Python/PyInstaller/Inno Setup 버전
- 빌드 과정에서 실제로 수정한 파일이 있다면 파일명과 이유

GitHub Release 게시, 태그 생성, 버전 변경은 하지 않는다. 사용자가 설치 파일을 확인한 뒤 별도로 진행한다.
