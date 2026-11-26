# 개발 PC 배포와 운영 대상 등록

MVP 배포 대상은 Linux 개발 PC다. Python 패키지·MCP 서버·스킬·훅·AI 호스트는 PC에서 실행한다. Pi에는 기존 Python 3와 I2C 도구를 사용하며 이 문서의 PC 설치 명령을 Pi에서 실행하지 않는다. 검증 환경과 남은 인수는 [Phase 6 기록](phase6-acceptance.md)을 따른다.

## PC 준비

```bash
python3 -m venv .venv
.venv/bin/python -I -m pip install -c constraints-dev.txt -e '.[dev]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -I -m pytest -q -W error
```

제품에는 자체 LLM 로그인·API 클라이언트가 없다. 사용 중인 Codex 등 AI 호스트의 모델과 인증을 사용한다. 스킬은 저장소의 .agents/skills 상대 링크로 검색한다. 다른 PC에 wheel만 설치하면 저장소 스킬·예제가 함께 등록되지는 않는다. [스킬 사용 안내](skill-usage.md)

## SSH와 대상 등록

운영자는 비특권 계정·호스트 키·개인키 파일·예상 버스·주소를 관리한다. 처음 연결하거나 호스트 키가 달라졌다면 Pi 콘솔 등 신뢰할 수 있는 경로에서 지문을 확인하고 PC의 known_hosts를 관리한다. 진단기는 새 키를 자동 수락하지 않는다. 개인키·비밀번호를 JSON이나 채팅에 넣지 않는다. 키 등록은 승인된 준비 작업이며 진단 도구가 authorized_keys를 수정하지 않는다.

[대상 예제](../examples/targets.example.json)를 비공개 운영 설정으로 복사하고 절대 경로를 넣는다. 파일은 소유자 관리 일반 파일, 권한 0600 또는 다른 사용자 쓰기 불가 상태여야 한다. 증거 저장소는 물리 장치마다 기존 target_id와 기존 루트를 유지한다. 디렉터리 0700·파일 0600을 사용하며 심볼릭 링크·하드 링크는 거부한다.

conflicting_use_excluded의 기본값은 false다. 사용자가 지금 다른 I2C 프로그램을 중지했다는 확인과 운영 선언이 모두 있어야 식별 읽기를 진행할 수 있다. 과거 답변이나 드라이버 미바인딩만으로 현재 사용자 공간 점유를 배제하지 않는다. 주소·점유가 불명확하면 passive로 조사한다. 운영 설정을 갱신한 뒤 서버를 재시작하며 진단 도중 변경하지 않는다.

Pi의 기존 i2ctransfer가 없거나 권한·어댑터·결합 전송이 확인되지 않으면 검사는 중단/부분 보고로 끝난다. 설치·sudo·권한 변경·드라이버 언바인딩·I2C 활성화·재부팅은 별도 구체적 변경 계획과 사용자 승인이 필요한 준비 작업이다. 장치 오류를 재현하려고 권한·부트 설정을 자동 변경하지 않는다.

## Codex 연결

[MCP 예제](../examples/codex-mcp.toml)의 절대 경로를 교체해 신뢰된 프로젝트 .codex/config.toml에 서버 항목을 병합한다. 서버 이름은 embedsleuth를 사용한다. [훅 예제](../examples/codex-hooks.json) 또는 생성기로 만든 정의를 .codex/hooks.json에 병합하고 Codex의 사용자 검토·신뢰를 거친다. [훅 사용 안내](hooks-usage.md)

이 개발 저장소에서 준비한 실제 로컬 설정은 .embedsleuth-local와 .codex/config.toml·hooks.json이며 Git에서 제외한다. 접속 정보·운영 상태·개인별 경로를 커밋하지 않는다. 인증키나 증거를 다른 프로젝트로 복사해 격리를 우회하지 않는다.

```bash
.venv/bin/python -I scripts/check_codex_integration.py
.venv/bin/python -I scripts/check_codex_integration.py --project-settings
```

첫 명령은 빈 대상·임시 설정의 격리 검증이다. 실제 프로젝트 설정이 있어도 명령 인자 우선순위로 운영 MCP를 덮어쓰지 않고 별도의 임시 등록을 선택한다. 둘째는 실제 프로젝트 스킬·MCP·훅 신뢰 상태를 읽는다. 이때 저장하지 않는 ephemeral 문맥을 만들지만 모델 턴·장치 도구는 실행하지 않는다. 사용자 설정이나 훅 신뢰를 바꾸지 않는다.

## 실물 인수 명령

기존 증거 저장소와 운영 등록을 준비한 뒤 명시적으로 실행한다. 기본은 센서 통신 없는 passive다.

```bash
.venv/bin/python -I scripts/run_hardware_acceptance.py \
  --config /ABSOLUTE/PATH/targets.json --target lab-pi
```

현재 주소·다른 I2C 작업 중지를 확인하고 등록의 충돌 배제 선언도 유효할 때만 다음 능동 식별 인수를 사용한다.

```bash
.venv/bin/python -I scripts/run_hardware_acceptance.py \
  --config /ABSOLUTE/PATH/targets.json --target lab-pi \
  --identity-address 0x68 --confirm-exclusive-i2c
```

이 확인 인자는 false인 운영 선언을 덮어쓰지 않는다. 검사기는 실제 STDIO MCP 서버를 통해 도구를 호출하고 원격 오류 뒤 하드웨어를 재시도하지 않는다. JSON 결과와 기존 RunStore의 증거를 함께 보존한다. 종료 코드 0은 명시한 결정적 검사 범위의 통과, 1은 검사 실패/차단, 3은 자원 측정 등 판정 자료 부족이다. passive의 identity=unknown과 report_outcome=partial은 센서 고장 판정이 아니다. 이 명령만으로 전체 MVP 릴리스나 모델 행동 평가가 통과하지 않는다.

## 복구와 운영 한계

미완료 원격 의도는 재시작 시 재실행하지 않고 영속 격리한다. remote_state_unknown을 성공이나 종료 확인으로 바꾸지 않는다. 격리는 새 target_id·빈 저장소·캐시 삭제로 우회하지 않는다. 현재 공개 격리 해제 API는 없으며 원격 작업 종료를 운영자가 별도 확인한 후 복구 방안을 정해야 한다. 보고서 조회·원본 증거를 먼저 보존한다.

증거·훅·모델 평가 기록에는 자동 보존 만료가 없다. 장기 운영의 백업·정리·접근 권한은 운영자가 관리한다. 동작 중인 서버의 상태·감사·잠금 파일을 임의 삭제하지 않는다.

## 개발용 자원·오류 인수

[프로세스 관측기](../scripts/run_process_acceptance.py)는 기존 운영 설정으로 /proc의 PID·시작 시각·RSS만 15초간 관측하면서 실제 MCP passive 검사를 수행한다. 센서·명령줄·환경·개인키 내용을 원격 관측하지 않는다. 요청 간격은 20ms이나 실제 최대 간격도 기록하며, 표본의 범위를 전체 순간 peak나 전체 자식 종료 보장으로 바꾸지 않는다. 원격 검사기와 별도의 SSH 관측 세션은 계측 오버헤드다. 외부 모델은 사용하지 않는다.

실물 오류는 [승인된 변경·복구 계획](hardware-fault-plan.md)과 대화형 실행기를 따른다. sudo 준비를 MCP의 일반 진단이나 모델 승인으로 바꾸지 않는다. 사용자 확인 없이 파일 권한·커널 장치를 바꾸는 일반 시험은 없다.


## 실행 중 취소 인수와 계측 한계

[취소 인수 실행기](../scripts/run_cancel_acceptance.py)는 --confirm-quarantine을 명시해야 한다. 실제 passive 생산 MCP 요청 한 건을 취소하며 그 결과로 생긴 영속 격리를 해제하지 않는다. 같은 사용자·새 PID의 argv를 검토된 검사기 소스 해시와 메모리 내에서만 비교하고 원문을 보존하지 않는다. 원격 프로세스를 멈추거나 장치·설정을 변경하지 않는다.

```bash
.venv/bin/python -I scripts/run_cancel_acceptance.py \
  --config /absolute/path/targets.json --target lab-pi \
  --confirm-quarantine --output /absolute/private/path/cancellation.json
```

관측한 원격 검사기와 미완료 MCP 요청이 있어야 취소 경로를 시험한 것으로 판정한다. captured PID+start tick 종료, interrupted, pending=null, 감사 닫힘, 영속 격리를 확인해도 취소 수신 시점의 원격 실행·강제 종료·미관측 자식 전체 종료를 증명하지 않는다. 실패 후 검사를 자동 반복하지 않는다. 실제 실행에서는 이 경로가 통과했다.

2026-11-26 사용자가 승인된 준비를 완료해 현재 실물 Pi의 memory controller가 활성화됐다. 이후 별도로 승인된 취소 건의 로컬 복구·새 생산 MCP 메모리 측정까지 완료했다. 원래 취소 보고서는 interrupted/unknown으로 보존하며 현재 대상 격리는 없다. [잔여 인수 계획](memory-cancellation-plan.md)은 현재 관측·제안 변경·메모리 지표의 차이와 격리 복구 선행 조건을 설명한다.


## 닫힌 passive 취소의 로컬 복구

일반 격리 해제 MCP API는 추가하지 않는다. [한정된 운영자 계획](passive-recovery-plan.md)은 독립 확인된 새 부트, 기존 SSH 신뢰·등록 결합, 닫힌 passive inspect_target 한 번만 허용한다. 승인 전에는 recover_passive_cancel.py를 확인 플래그 없이 실행해 조건만 점검한다. 실제 적용은 사용자 승인 후 --confirm-local-recovery를 명시하며, root는 쓰지 않는다.

복구 기록은 원래 중단 상태·보고서·감사·증거를 유지한 채 별도 owner-only 파일에 기록한다. 다른 미완료 작업이 있거나 저장·검증이 실패하면 해제하지 않는다. 이후 새 생산 MCP 연결을 사용한다. 이전 코드를 로드한 서버는 이 확인 기록을 모르므로 재연결해야 한다. 저장소 경로를 바꾸거나 격리 파일을 직접 지우지 않는다.

run_cgroup_acceptance.py는 기존 생산 MCP 검사와 읽기 전용 memory.peak 관측을 병행한다. 계측기와 다른 세션을 포함하는 SSH 서비스·user.slice의 생성 이후 상한을 합하며, peak 차감·초기화·프로세스 이동은 하지 않는다. 엄격한 추가 RSS 목표와 과금 메모리 지표는 구분한다.


## 메모리 판정과 실제 에이전트 통합 검증

[MVP 기준 결정](memory-budget-decision.md)에 따라 과금 메모리 상한 64MiB와 미측정인 전체 추가 RSS를 구분한다. cgroup 실행기는 결정적 검사와 메모리 기준이 모두 pass면 종료 코드 0, 결정적 검사 fail이면 1, 자료 부족·범위 불명확·넓은 상한 예산 초과면 3을 반환한다. 취소 실행기도 inconclusive를 3으로 반환한다. 측정 JSON과 원래 증거는 어느 경우든 보존한다.

[실제 에이전트 검증 실행기](../scripts/run_agent_acceptance.py)는 제품의 별도 에이전트 루프가 아니라 개발용 호스트 평가다. 공개 스킬·실제 프로젝트의 신뢰된 훅·기존 생산 MCP로 최대 한 모델 턴을 실행하며, [구체적인 전송·읽기 범위](agent-evaluation-scope.md)의 사전 승인이 필요하다. --confirm-real-model-and-passive-reads는 그 승인을 받은 운영자의 명시 입력이다. 승인·훅 신뢰·대상 등록을 영구 변경하지 않는다. 결과는 사람의 보고 의미 검토를 거쳐야 하며 자동 성공 판정으로 전체 인수를 대신하지 않는다.
