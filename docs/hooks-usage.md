# Phase 5: 진단 훅

개발 PC의 Python 명령 훅이다. 사용자는 기존 AI CLI에서 요청하며 별도의 대화형 진단 CLI를 열지 않는다. 모델·SSH·Pi 검사는 훅에서 실행하지 않는다. 기본 MCP 설정은 [MCP 사용 문서](mcp-usage.md), 진단 절차는 [스킬 안내](skill-usage.md)를 따른다.

## 동작과 경계

| 이벤트 | 검사 | 출력 |
|---|---|---|
| PreToolUse | 아홉 도구의 엄격한 인자, 등록 대상·프로필, 기존 실행 결합, 턴·호출 기록 | 정상은 `{}`, 오류는 고정 사유의 deny |
| PostToolUse | MCP 텍스트·구조화 응답 일치, 오류 봉투, 실제 저장된 증거·보고서, 원격 before/after 감사 | 정상은 `{}`, 불일치는 경고·추가 문맥 |
| Stop | 이번 턴의 활성 진단 종료, 보고서 구조·증거 참조·한계, 감사 기록 닫힘 | 미완료 시 한 번 보완 요청; 반복 시 경고만 |

PreToolUse는 승인 허용, 인자 변경, 정책 관측을 만들지 않는다. 실제 접근 판단은 코어가 수행한다. 같은 operation_id의 완료된 식별 결과는 terminal 실행에서도 코어의 캐시로 조회할 수 있다.

PostToolUse는 이미 실행한 작업을 되돌리거나 결과를 숨기지 않는다. MCP 결과가 명시적 어댑터 오류이면 이를 하드웨어 증거로 취급하지 않는다. 동시 취소가 저장 보고서보다 먼저 interrupted 응답을 반환하는 경우 원래 응답을 보존하고 검증 미완료를 알린다. 저장 실패·경합·변경 중인 스냅샷도 재실행 없이 경고한다.

Stop은 보고서의 자연어 진실성을 판정하지 않는다. reported·blocked·expired·cancelled 실행의 유효한 partial/interrupted 보고서를 인정한다. 미완료·감사 누락이면 기존 증거로 finalize_report/get_report 또는 중단 한계 설명을 한 번 요청한다. 호스트가 원격 호출의 PostToolUse를 생략했더라도 관련 실행과 모든 감사 기록이 이미 닫혔다면, 누락 영수증은 검증 미완료로 보존하고 경고만 남긴다. 새 보고서 조회로 누락 이벤트를 복구할 수 없으므로 재요청하지 않는다. 명시적인 결과 검증 실패·미완료 실행·pending 작업은 계속 한 번 보완 대상으로 처리한다. stop_hook_active가 true이거나 해당 턴에서 이미 보완을 요청했으면 다시 block하지 않는다. 과거 보고서 조회만 하거나 EmbedSleuth와 관계없는 턴은 종료 검사를 활성화하지 않는다.

호스트가 훅을 건너뛰거나 시간 초과·실행 오류가 나도 코어의 등록·읽기 정책·예산·격리는 독립적으로 적용된다. 훅을 유일한 승인·안전 경계로 운영하지 않는다.

## 설정과 사용자 검토

검증 호스트는 Linux의 **Codex CLI 0.172.0**이다. PreToolUse/PostToolUse는 MCP 이름에 matcher를 적용하고 Stop은 내부 턴 기록으로 범위를 제한한다. Codex의 프로젝트 훅 위치는 `.codex/hooks.json`이며, 비관리 훅은 사용자 검토·신뢰가 필요하다. 정의를 바꾸면 다시 검토한다. [공식 Codex 훅 문서](https://learn.chatgpt.com/docs/hooks)

이 저장소는 운영 훅이나 신뢰 승인을 자동 등록하지 않는다. [설정 예](../examples/codex-hooks.json)를 검토하거나 다음 생성기로 절대 경로를 넣은 정의를 출력한다.

```bash
.venv/bin/python -I scripts/render_codex_hooks.py \
  --config /ABSOLUTE/PATH/targets.json \
  --hook-state /ABSOLUTE/PATH/embedsleuth-hooks
```

출력을 확인하고 기존 `.codex/hooks.json`의 hooks 항목과 병합한다. 기존 파일을 덮어쓰지 않는다. config는 MCP 서버와 동일한 운영 설정이다. hook-state는 진단 state_directory와 서로 포함되지 않는 별도의 경로여야 하며 부모 디렉터리는 미리 존재해야 한다. Python 경로에 공백이나 셸 특수문자가 있으면 생성기가 인용을 처리한다. MCP 서버 이름이 embedsleuth가 아니면 matcher와 Python의 PREFIX를 함께 검토해야 한다. 현재 구현은 그 이름으로 고정한다.

설정 후 Codex CLI `/hooks` 또는 데스크톱의 Review hooks/Settings → Hooks에서 명령·경로·이벤트를 검토하고 신뢰한다. 신뢰 검토를 우회하는 실행 옵션은 사용하지 않는다. 훅 명령을 바꾸지 않고 Python 구현만 갱신해도 호스트가 코드 변경을 검증해 주는 것은 아니므로, 배포 코드 자체를 별도로 검토한다. [공식 신뢰·설정 안내](https://learn.chatgpt.com/docs/hooks)

호스트는 명령에 stdin JSON을 전달한다. 설치된 `embedsleuth-hook --event EVENT --config ABSOLUTE_PATH --hook-state ABSOLUTE_PATH`도 같은 진입점이다. 출력은 stdout의 JSON 한 개이며 원시 입력·예외·접속 정보를 로그로 출력하지 않는다. 예제 제한 시간은 이벤트당 5초다. 훅 프로세스는 PC에서만 실행한다.

## 저장과 제한

RunStore.open_reader는 기존 진단 저장소를 파일 생성·잠금·복구 없이 연다. 다른 실행이 기록을 갱신하면 일관된 스냅샷을 확인할 수 없다는 경고로 처리한다. 훅은 get_report나 복구 함수를 대신 호출하지 않는다.

별도의 hook-state에는 session_id·turn_id·tool_use_id의 SHA-256과 인자 해시, 도구 이름·run_id·검증 완료·한 번 보완 여부만 저장한다. 응답 본문·관측·SSH 계정·키·대화록은 저장하지 않는다. 인자 해시는 비밀 보관이나 암호화 수단이 아니다. cwd·transcript_path는 입력에 있어도 읽기 경로로 사용하지 않는다. 디렉터리 0700·파일 0600과 소유권·no-follow·하드 링크 검사, 원자 교체·fsync·비차단 잠금을 재사용한다.

입력은 최대 8MiB, 턴당 일반 호출 기록은 64개, 종료·조회·취소용 추가 기록은 16개, 활성 실행과 대기 시작 예약 합계는 최대 8개다. 예산 소진 시 일반 호출은 거부하고 보완 요청을 무한 반복하지 않는다. 기록은 턴별로 분리되며 자동 보존 만료는 구현하지 않았다. 장기 운영 시 호스트를 종료하고 전용 hook-state의 보존·삭제를 관리한다. 진단 state_directory를 지우거나 격리 해제를 대신하지 않는다.

## 검증과 남은 인수

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -I -m pytest -q -W error tests/test_hooks.py
.venv/bin/python -I scripts/check_codex_integration.py
```

자동 시험은 실제 코어·MCP 응답·저장소와 가짜 장치를 사용한다. 20개 스킬 절차를 훅과 함께 재생하고 위조 응답·감사 누락·재사용·부분/중단·취소 경합·저장 경합·스냅샷 변경·입력 거부·훅 부재/실패/시간 초과에서도 코어 정책 유지·종료 반복 방지를 확인한다.

호스트 확인기는 임시 Codex 설정에서 스킬 1개·MCP 도구 9개·command 훅 3개의 검색과 untrusted 상태를 확인한다. 사용자의 설정·신뢰 저장소를 변경하지 않고 모델 턴·장치 진단을 시작하지 않는다. 실제 모델 턴에서 신뢰된 훅의 발화와 보완 메시지에 대한 모델 반응, 데스크톱 UI의 신뢰 절차, 다른 버전·OS·AI 호스트는 미검증이며 Phase 6 인수 범위다. 이번 단계에서는 실제 Pi 검사를 반복하지 않았다.


## Phase 6 실제 정상 흐름 확인

2026-11-26 Codex CLI 0.172.0 / gpt-6.1-sol의 실제 Pi passive 모델 턴에서 프로젝트 훅 PreToolUse 6회·PostToolUse 6회·Stop 1회가 모두 completed로 끝났다. 영속 영수증 5개는 모두 검증됐고, 실행·감사가 닫혀 warned=false·보완 요청 없음이었다. 정상 경로의 실제 발화·코어 연계를 확인했으며, 과거 오류 경로의 Post 누락이나 다른 호스트까지 통과로 확장하지 않는다. [최신 범위·결과](phase6-acceptance.md)
