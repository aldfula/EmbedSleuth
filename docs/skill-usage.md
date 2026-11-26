# MPU6050 진단 스킬 사용

`diagnose-mpu6050`는 기존 AI 호스트가 EmbedSleuth MCP 도구로 진단하도록 안내한다. 검사 실행과 안전 정책은 기존 코어가 담당한다. 별도 대화형 CLI·LLM API 키·자체 에이전트 루프를 추가하지 않았다.

## 저장소에서 발견

스킬의 배포 원본은 `src/embedsleuth/data/skills/diagnose-mpu6050/`이고 `skills/diagnose-mpu6050/`은 같은 내용의 저장소 별칭이다. `.agents/skills/diagnose-mpu6050`의 저장소 내부 상대 링크로 Codex의 저장소 스킬 검색에 연결한다. 저장소를 복제한 뒤 그 안에서 Codex를 사용한다. 다른 프로젝트의 전역 스킬은 변경하지 않는다. Python wheel에도 같은 스킬·참조·호스트 메타데이터가 포함된다. 저장소 없이 설치한 사용자는 [설치 준비 명령](installation.md)으로 새 작업 폴더의 .agents/skills에 복사한다. 기존 Codex 설정이나 신뢰는 덮어쓰지 않는다.

Codex는 `.agents/skills`와 링크된 스킬을 검색하고 `$diagnose-mpu6050` 명시 호출과 description 기반 자동 선택을 지원한다. 업데이트가 보이지 않으면 호스트를 새로고침하거나 재시작한다. [공식 스킬 문서](https://learn.chatgpt.com/docs/build-skills)

자동 선택은 허용 상태다. 명시 호출을 먼저 점검하는 것이 자동 선택 비활성화를 뜻하지 않는다. 메타데이터는 `agents/openai.yaml`, 상세 MCP 계약과 오류 분기는 스킬의 references에 있다.

## MCP 연결과 운영 대상

스킬 의존성의 서버 ID는 `embedsleuth`다. `transport: stdio`를 선언했지만 운영자의 실행 경로나 SSH 대상 등록을 자동 생성하지 않는다. [MCP 사용 안내](mcp-usage.md)에 따라 운영 설정을 먼저 준비한다. 물리 장치당 기존 target_id와 공유 저장소를 유지한다.

[Codex 설정 예](../examples/codex-mcp.toml)의 절대 경로를 교체해 호스트 설정의 `mcp_servers.embedsleuth`에 등록한다. 사용자 전역 설정 또는 신뢰된 프로젝트 `.codex/config.toml`을 사용할 수 있다. 기존 설정을 덮어쓰지 말고 해당 서버 항목만 추가한다. 기존 도구 승인 정책은 예제에서 변경하지 않는다. [공식 MCP 연결 문서](https://learn.chatgpt.com/docs/extend/mcp)

Phase 4는 이 저장소의 스킬 검색 링크만 추가했다. 실제 운영 대상·사용자 Codex 설정을 바꾸지 않았다. 호스트 연결 시험은 별도의 임시 설정과 빈 대상 목록으로 수행했다. 2026-11-24의 다른 I2C 작업 중지 선언을 현재 상태로 재사용하지 않는다.

## 요청 예

처음에는 장치 접근 없는 확인부터 시작할 수 있다.

```text
$diagnose-mpu6050로 MPU6050 프로필의 식별 조건과 문서 근거만 보여 줘. 장치 검사는 하지 마.
```

실제 운영 설정을 등록하고 대상·검사 범위를 확인한 뒤에는 다음처럼 요청한다. 아래 lab-pi는 예시 ID이며 실제 등록 ID를 사용해야 한다.

```text
$diagnose-mpu6050로 등록된 lab-pi의 환경만 passive로 조사해 줘. 센서 통신은 하지 마.
```

```text
$diagnose-mpu6050로 등록된 lab-pi의 MPU6050 인식 문제를 진단해 줘.
예상 주소는 0x68이고, 현재 다른 I2C 프로그램은 모두 중지했어.
```

기본 모드는 read_only다. 현재 사용자 확인만으로 로컬 등록의 `conflicting_use_excluded=false`를 덮어쓸 수 없다. 코어가 차단하면 스킬은 오류·부분 결과를 설명하고 종료한다. 비밀번호·개인키 내용은 요청에 넣지 않는다.

```text
$diagnose-mpu6050로 실제 run_id의 저장된 보고서만 조회해 줘. 하드웨어 검사는 하지 마.
```

스킬은 설치·sudo·리셋·권한 변경·재부팅을 수행하지 않는다. 측정 데이터 수집이나 전기적 호환 확정은 별도 범위다. 정상 식별 응답도 정품·측정 정확도·전체 센서 정상 보장이 아니다.

## 검증과 재현

스킬 입력 예와 MCP 시나리오는 일반 pytest에 포함된다. 스킬 검증용 PyYAML은 개발 의존성이며 Pi에 설치하지 않는다. [평가 자료](../evals/diagnose-mpu6050/README.md)에 자동 재생과 모델 행동 평가를 구분해 기록한다.

설치된 Codex CLI에서 스킬 검색과 MCP 도구 스키마를 다시 확인하려면 개발 가상환경에서 실행한다.

```bash
.venv/bin/python -I scripts/check_codex_integration.py
```

이 개발 검사기는 임시 Codex 설정·상태 저장소·빈 대상 목록을 사용한다. Codex app-server의 initialize·skills/list·mcpServerStatus/list만 요청하며 thread/turn을 생성하거나 모델을 호출하지 않는다. 종료 후 임시 자료를 지우며 사용자 설정·인증을 복사하지 않는다. Codex app-server 프로토콜은 설치 버전에 따라 달라질 수 있으므로 일반 pytest의 필수 의존성으로 넣지 않았다. [공식 app-server API](https://learn.chatgpt.com/docs/app-server)

이번 호스트 검증은 스킬·의존성·아홉 입력 스키마의 로딩을 확인한다. 자연어 자동 선택률·실제 모델의 절차 준수·보고 품질과 실제 Pi까지의 통합은 별도 평가 대상이다. 훅은 Phase 5에서 구현하며, 없어도 MCP 코어의 제한은 그대로 적용된다.
