# EmbedSleuth v0.1.0

Raspberry Pi 3와 MPU6050의 I2C 연결·인식 문제를 기존 AI 호스트의 스킬·MCP·훅으로 진단하는 첫 MVP 배포다. PC에 설치하며 Pi에는 LLM·상주 에이전트·패키지를 설치하지 않는다.

## 포함 기능

- 등록 대상만 사용하는 SSH 신뢰 검증과 제한된 읽기 진단, 센서 통신 없는 passive 모드.
- 구조화된 증거·보고서, 예산·신선도·취소·불명확한 원격 완료의 격리 정책.
- 검토된 MPU6050 프로필·스킬·참조, Pre/Post/Stop 훅.
- wheel에 포함된 연결 자료와 `embedsleuth-setup`의 새 작업 폴더 준비 기능. 기존 등록/증거/호스트 신뢰를 보존한다.

## 설치

2026-11-27에 기존 Release와 같은 wheel·sdist를 [PyPI](https://pypi.org/project/embedsleuth/0.1.0/)에도 게시했다. Linux 개발 PC의 전용 가상환경에서 `pip install embedsleuth==0.1.0`으로 설치하고 [최신 설치 안내](https://github.com/aldfula/EmbedSleuth/blob/main/docs/installation.md)를 따른다. GitHub Assets의 wheel·constraints-runtime.txt·SHA256SUMS와 첨부 INSTALL.md를 이용하는 설치도 가능하다. 의존성 다운로드에 인터넷이 필요하고, SSH 준비·대상 등록·훅 신뢰는 운영자가 수행한다. 기존 v0.1.0 태그와 첨부 문서는 변경하지 않았다.

## 검증 범위

PC: Linux x86_64 / Python 3.12.14 / Codex CLI 0.172.0. Pi: Pi 3 Model B / Debian 12 / aarch64 / Python 3.11.2. 0x68 WHO_AM_I 식별, 승인된 오류/복구·취소, 64MiB 이하 과금 메모리 상한과 실제 모델 정상 passive 1턴을 검증했다. [인수 결과](https://github.com/aldfula/EmbedSleuth/blob/v0.1.0/docs/phase6-acceptance.md)를 따른다.

전체 자동 회귀 **769 passed in 49.10s**(경고 오류 처리). 새 PC 설치 회귀 19개가 기존 750개에 추가됐다. 저장소 밖의 빈 가상환경에서 고정 런타임 설치·pip check·새 작업 폴더 생성·생산 STDIO MCP 9개 스키마·프로필·Pre/Post/Stop 실제 명령을 확인했다. pytest·PyYAML·빌드 도구는 런타임에 설치되지 않았다. 격리한 임시 Codex 홈에서 설치본 스킬 1개·세 훅 untrusted 검색도 확인했다. 설치 확인에는 실제 Pi·LLM·운영 설정을 사용하지 않았다. 소스 커밋은 v0.1.0 태그로 고정한다.

## 한계

센서 계측값 수집·전기적 호환 확정·시스템 변경은 지원하지 않는다. 0x69 실물은 사용자 결정으로 제외했다. 전체 추가 RSS·미관측 자식 전체 종료·다른 OS/호스트·일반 라우팅 정확도는 보장하지 않는다. 최소 Python 메타데이터는 3.11이며 PC Python 3.11은 별도 실행 미검증이다.
