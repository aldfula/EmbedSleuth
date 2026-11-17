# EmbedSleuth

데이터시트 근거와 실제 장치의 관측을 결합하는 임베디드 진단 AI 에이전트 패키지입니다. 첫 대상은 **Raspberry Pi 3와 MPU6050의 I2C 연결·인식 문제**입니다.

## 구조

사용자는 기존 AI CLI에서 진단을 요청합니다. AI 호스트의 스킬이 절차와 판단 기준을 제공하고, 훅이 호출 전후와 보고서를 검증합니다. PC의 MCP 서버가 Python 진단 코어를 호출하고, 실제 I2C 검사는 SSH를 통해 Pi에서 필요한 동안만 실행합니다.

```text
기존 AI CLI + 스킬·훅
  → PC의 EmbedSleuth MCP 서버·안전 정책
    → SSH → Pi의 단기 검사기 → I2C → MPU6050
  ← 구조화된 증거와 오류 ← 검사 결과
```

Pi에 LLM·MCP 서버·상주 에이전트를 설치하지 않습니다. EmbedSleuth 자체는 LLM API를 호출하지 않으며 모델 인증·요금은 AI 호스트의 설정을 따릅니다.

## 현재 구현 상태

**Phase 1: 데이터 계약·검토된 부품 프로필·읽기 정책을 구현했습니다.**

- Pydantic 기반 도구 요청, 검사 결과, 증거, 보고서 계약
- 미등록 필드·형식 변환·임의 실행 인자 거부
- MPU6050 IC 프로필과 문서 출처·개정·페이지·원문 SHA-256
- 제한된 식별 읽기를 위한 부작용 없는 선행 조건 판정
- 증거를 넘나드는 참조와 불완전한 결과의 성공 판정 차단
- 오프라인 pytest

**SSH 실행기, MCP 서버, 실제 스킬·훅은 아직 구현하지 않았습니다.** 아래는 향후 사용 경험이며 현재 실행 가능한 진단 명령이 아닙니다.

> EmbedSleuth로 등록된 lab-pi의 MPU6050 인식 문제를 진단해 줘.

## 로컬 개발과 테스트

Python 3.11 이상을 개발 대상으로 합니다. 실제 검증 버전과 Phase별 결과는 [구현 계획](docs/implementation-plan.md)에 기록합니다. 다음 명령은 **개발 PC**에서 실행하며 Pi에 설치하지 않습니다.

```bash
python3 -m venv .venv
.venv/bin/python -I -m pip install -e '.[dev]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -I -m pytest -q -W error
```

Python의 -I 옵션으로 외부 PYTHONPATH를 무시하고 pytest 자동 플러그인 로드를 끄므로 PC의 ROS 등 외부 패키지가 테스트 환경에 개입하지 않습니다. 테스트는 네트워크·Pi·LLM·API 키를 요구하지 않습니다. Phase 1은 **113개 테스트 통과**, 개발 모드 설치와 wheel 설치·리소스 로딩 검증을 완료했습니다.

개발 환경의 정확한 의존성 재현에는 다음을 사용할 수 있습니다. 현재 제약 파일은 Linux·Python 3.12 검증 환경의 버전입니다.

```bash
.venv/bin/python -I -m pip install -c constraints-dev.txt -e '.[dev]'
```

## 안전 원칙

- 기본은 운영 설정과 센서 제어 상태를 바꾸지 않는 제한된 읽기입니다.
- passive 모드는 센서 통신도 생략합니다.
- 전체 I2C 스캔, 임의 셸·레지스터 접근, 강제 드라이버 우회를 제공하지 않습니다.
- 훅이 없어도 진단 코어의 정책이 적용되어야 합니다.
- 설치·권한 변경·센서 초기화·재부팅은 MVP 검사 도구에 포함하지 않습니다.
- 추후 변경 실행 기능은 대상과 구체적인 계획에 대한 사용자 승인을 요구합니다.

프로필의 reviewed는 문서의 지정된 조건을 검토했다는 뜻입니다. 실제 Pi·모듈 호환성이나 하드웨어 정상 동작이 검증됐다는 뜻은 아닙니다. 식별값 일치도 정품·측정 정확도를 보장하지 않습니다.

## 문서

- [아키텍처와 도구 계약](docs/architecture.md)
- [요구사항과 인수 기준](docs/requirements.md)
- [단계별 구현 계획과 검증 결과](docs/implementation-plan.md)
- [이전 설계 논의 기록](docs/design.md) — 현재 규격은 위 세 문서를 따릅니다.
