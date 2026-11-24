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

**Phase 3의 로컬 STDIO MCP 서버까지 구현했습니다.**

- 엄격한 도구·결과·증거 계약, 검토된 MPU6050 프로필과 읽기 정책
- 로컬 등록 대상만 사용하는 Paramiko SSH 전송과 호스트 키 검증
- Pi에 설치하지 않는 표준 라이브러리 검사기와 제한된 WHO_AM_I 읽기
- 세션·신선도·시간·호출·출력 제한, 동일 operation_id 결과 재사용
- 소유자 전용 증거 저장, 원자적 보고서 갱신, 중단 복구와 대상 격리
- 가짜 SSH·로컬 자식 프로세스·저장 실패를 이용한 오프라인 테스트
- 아홉 진단 도구의 MCP 스키마·구조화 응답, 로컬 대상 설정, 취소·종료 처리

**진단 스킬과 훅은 Phase 4–5에서 연결합니다.** 별도의 대화형 CLI를 열 필요는 없습니다. 기존 AI 호스트에 서버 실행 명령을 등록하면 호스트가 MCP 프로세스를 시작하고 도구를 호출합니다. 아래 자연어 요청에 따른 스킬 자동 선택과 전체 에이전트 동작은 후속 통합 검증 대상입니다.

> EmbedSleuth로 등록된 lab-pi의 MPU6050 인식 문제를 진단해 줘.

## 로컬 개발과 테스트

Python 3.11 이상을 개발 대상으로 합니다. 실제 검증 버전과 Phase별 결과는 [구현 계획](docs/implementation-plan.md)에 기록합니다. 다음 명령은 **개발 PC**에서 실행하며 Pi에 설치하지 않습니다.

```bash
python3 -m venv .venv
.venv/bin/python -I -m pip install -e '.[dev]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -I -m pytest -q -W error
```

Python의 -I 옵션으로 외부 PYTHONPATH를 무시하고 pytest 자동 플러그인 로드를 끄므로 PC의 ROS 등 외부 패키지가 테스트 환경에 개입하지 않습니다. 테스트는 외부 네트워크·Pi·LLM·API 키를 요구하지 않습니다. asyncio의 로컬 소켓·스레드 알림과 자식 프로세스 실행은 허용되어야 합니다. 최신 테스트 수와 패키지 검증 결과는 [구현 계획](docs/implementation-plan.md#phase-3-검증)에 기록합니다.

개발 환경의 정확한 의존성 재현에는 다음을 사용할 수 있습니다. 현재 제약 파일은 Linux·Python 3.12 검증 환경의 버전입니다.

```bash
.venv/bin/python -I -m pip install -c constraints-dev.txt -e '.[dev]'
```

## MCP 연결

[로컬 설정 예](examples/targets.example.json)를 저장소 밖의 운영 설정으로 복사해 PC 경로·등록 대상·예상 연결을 지정합니다. 설정에 비밀번호나 개인키 내용을 넣지 않습니다. 물리 장치당 기존의 안정된 target_id와 상태 저장소를 계속 사용합니다. 예제의 충돌 배제 선언은 false이므로 식별 읽기는 차단됩니다.

호스트의 STDIO MCP 등록에서 사용할 실행 명령은 다음 형태입니다. 경로를 실제 절대 경로로 교체해야 합니다.

```text
/ABSOLUTE/PATH/EmbedSleuth/.venv/bin/python -I -m embedsleuth.mcp_server --config /ABSOLUTE/PATH/targets.json
```

설치 시 제공되는 `embedsleuth-mcp --config /ABSOLUTE/PATH/targets.json` 진입점도 같은 서버를 실행합니다. 이 프로세스는 STDIO 프로토콜을 기다리는 서버입니다. [설정·도구 호출·응답·취소 안내](docs/mcp-usage.md)를 참고하세요. 실제 AI 호스트의 설정은 이번 단계에서 변경하지 않았습니다.

## 진단 실행 경계

신뢰된 로컬 코드가 `RegisteredTarget`에 호스트·계정·신뢰 파일·예상 버스와 주소를 등록한 뒤 `TargetRegistry`, `RunStore`, `ParamikoTransport`, `DiagnosticCore`를 조합합니다. AI가 받는 것은 `target_id`와 `run_id`이며 SSH 접속 정보는 도구 인자로 받지 않습니다. [코어 사용 예와 운영 조건](docs/phase2-usage.md)을 참고하세요.

2026-11-24에 Pi 3 Model B의 실제 SSH·보드·I2C 환경을 확인하고, 센서 연결 후 주소 0x68의 WHO_AM_I=0x68 식별 읽기에 성공했습니다. 추가 메모리 64MiB 목표·다른 주소·오류 상황과 전체 실물 인수는 아직 검증이 필요합니다. I2C 설정 변경이나 원격 패키지 설치는 수행하지 않았습니다. 등록된 환경의 다른 I2C 사용자 배제는 별도 운영 조건이며 자동으로 증명되지 않습니다.

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
- [Phase 1 독립 검토와 수정](docs/phase1-review.md)
- [Phase 2 교차 검토와 수정](docs/phase2-review.md)
- [Phase 2 코어 사용 예](docs/phase2-usage.md)
- [Phase 3 MCP 사용과 운영 조건](docs/mcp-usage.md)
- [이전 설계 논의 기록](docs/design.md) — 현재 규격은 위 세 문서를 따릅니다.
