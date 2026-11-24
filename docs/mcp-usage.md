# 로컬 STDIO MCP 사용

Phase 3는 PC의 진단 코어를 MCP 클라이언트에서 호출하는 연결부다. 서버에 자체 LLM이나 채팅 화면이 없으며 기존 AI 호스트가 실행하고 추론한다. Phase 4에 [진단 스킬과 Codex 검색 검증](skill-usage.md)을 추가했고 훅은 Phase 5 범위다.

## 운영 설정

[설정 예](../examples/targets.example.json)를 저장소 밖의 운영자 관리 디렉터리에 복사한다. 예시 주소와 PC 경로를 실제 값으로 교체한다. JSON 필드는 다음과 같다.

| 필드 | 의미 |
|---|---|
| schema_version | `1.0` |
| state_directory | 기존 공유 증거 저장소의 절대 경로; 상위 디렉터리가 존재해야 함 |
| targets | 등록 대상 배열; 빈 배열 허용, 최대 32개 |
| target_id | 물리 장치당 안정된 ID; 기존 진단에 사용한 ID 유지 |
| host·port·username | 운영자가 검토한 SSH 접속 정보 |
| known_hosts_path | 독립적으로 확인한 SSH 호스트 키 파일의 절대 경로 |
| key_path | 개인키 파일의 절대 경로; 내용·비밀번호를 넣지 않음 |
| profile_id | 현재 `mpu6050` |
| expected_bus·expected_addresses | 예상 연결; JSON 주소는 정수 104 또는 105 |
| conflicting_use_excluded | 다른 I2C 프로그램을 운영자가 배제했다는 선언; 기본 false |

설정 파일은 현재 사용자 소유의 일반 파일이어야 하며 0600 또는 0644 등 다른 사용자가 쓸 수 없는 권한이어야 한다. 심볼릭 링크·하드 링크·중복 JSON 키·미등록 필드를 거부한다. 설정 디렉터리도 신뢰된 운영자가 관리한다. `~`·상대 경로는 확장하지 않는다. 파일 최대 크기는 64KiB다. 등록 변경은 서버 재시작 후 반영한다.

기존 상태 저장소를 다른 경로로 바꾸거나 target_id를 새로 만들어 격리를 우회하지 않는다. 저장소는 디렉터리 0700·파일 0600을 요구하고 기존의 안전하지 않은 권한을 자동 변경하지 않는다. 이번 Phase는 실제 접속 설정이나 AI 호스트 설정을 자동으로 생성·변경하지 않는다.

`conflicting_use_excluded=true`는 그 환경의 다른 I2C 작업을 중지하고 확인한 동안만 사용한다. 서버 재시작이 이 선언을 자동 재검증하지 않는다. 확인할 수 없으면 false를 유지하거나 passive로 진단한다. 호스트 키가 없거나 변경됐으면 검사를 중단하며 자동 수락·원격 설치·권한 변경을 하지 않는다.

## 호스트에서 서버 시작

AI 호스트의 STDIO MCP 등록에 절대 실행 경로와 인자 목록을 지정한다. 아래는 등록에 필요한 값이며 전체 호스트 설정 파일 형식은 호스트마다 다르다.

```json
{
  "command": "/ABSOLUTE/PATH/EmbedSleuth/.venv/bin/python",
  "args": [
    "-I", "-m", "embedsleuth.mcp_server",
    "--config", "/ABSOLUTE/PATH/targets.json"
  ]
}
```

설치된 `embedsleuth-mcp --config /ABSOLUTE/PATH/targets.json` 명령도 동일하다. 별도 대화형 CLI를 띄우는 방식이 아니다. 서버는 MCP JSON-RPC를 stdin으로 받고 stdout으로 응답한다. 로그는 stderr이며 HTTP 포트를 열지 않는다. 시작·도구 목록·프로필 조회만으로 SSH에 접속하지 않는다. 키 기반 인증 또는 로컬 SSH 에이전트를 사용하며 서버에서 비밀번호 입력 창을 제공하지 않는다.

공식 SDK 클라이언트의 자동 협상과 legacy 초기화 모두 오프라인 검증했다. 특정 AI 제품의 MCP 설정·도구 승인·스킬 지원 여부까지 검증한 것은 아니다. [공식 SDK와 전송 API](https://py.sdk.modelcontextprotocol.io/client/transports/)

## 도구 순서와 결과

1. `get_component_profile({"profile_id":"mpu6050"})`로 검토 근거와 검사 범위를 확인한다.
2. `start_diagnosis({"target_id":"lab-pi","profile_id":"mpu6050","mode":"passive"})`로 실행을 생성한다. mode를 생략하면 read_only다.
3. 반환받은 run_id로 `inspect_target`, `inspect_i2c_environment`를 순서대로 호출한다.
4. read_only이고 코어의 모든 조건이 충족됐을 때만 `read_device_identity`를 호출한다. 아래 예의 버스·주소는 등록과 관측에 일치해야 한다.
5. `evaluate_findings`, `finalize_report`로 종료한다. partial·interrupted도 유효한 보고서다. 조회는 `get_report`, 명시적 중단은 `cancel_diagnosis`를 사용한다.

```json
{
  "run_id": "run-FROM-START",
  "bus_ref": "bus-1",
  "address": 104,
  "operation_id": "identity-address-68"
}
```

식별 요청에는 레지스터·길이·명령·승인·선행 조건 플래그를 받지 않는다. 같은 의도 재전송은 같은 operation_id와 인자를 사용하며 원래 시각·증거를 반환한다. 오류 후 새 ID로 자동 재시도하지 않는다. 시작한 실행의 120초 예산과 환경 증거 30초 유효 기간에는 AI가 다음 검사를 고르는 시간도 포함된다.

모든 도구는 다음 봉투를 텍스트 content와 structuredContent에 동일하게 반환한다. result의 내부 필드는 기존 코어 계약을 유지한다. 발견 사항은 배열이다.

```json
{
  "schema_version": "1.0",
  "result": null,
  "error": {
    "code": "invalid_request",
    "message": "Arguments do not match the diagnostic tool schema"
  }
}
```

정상은 error=null이다. 검사 실행 오류·차단·미지원·시간 초과는 isError=true이며 result에 원래 ToolResult와 증거 참조가 남는다. 입력·세션·저장 오류는 result=null과 안전한 오류 메시지를 반환한다. 정상 실행 후 식별값이 불일치한 assessment=fail은 isError=false다. 보고서 outcome=complete가 센서 전체 정상이나 정품을 보장하지 않는다. 알 수 없는 JSON-RPC 메서드나 프로토콜 형식 오류는 SDK의 프로토콜 오류 응답 대상이다.

## 취소·종료·제한

MCP 원격 검사 요청의 취소·클라이언트 시간 초과는 해당 run_id에 영속 취소 표시를 남긴다. `cancel_diagnosis`는 바쁜 실행에서도 마지막 확정 증거의 interrupted 보고서를 제공할 수 있다. 작업자가 후속 종료 결과를 저장하므로 즉시 반환된 보고서가 최종 증거를 모두 포함한다고 가정하지 않는다.

EOF·출력 연결 단절·SIGINT·SIGTERM은 새 호출을 중단하고 원격 검사에 취소를 전달한 뒤 작업자의 저장이 끝날 때까지 저장소를 유지한다. 열린 stdin이나 가득 찬 stdout 대기도 종료할 수 있다. 원격 종료가 불명확하면 대상 격리를 유지하며 재시작으로 지우지 않는다. SIGKILL·프로세스 충돌은 다음 시작에 미완료 의도를 복구한다. 로컬 디스크 자체가 멈춘 경우 종료 시간 상한은 보장하지 않는다.

일반 작업자 4개와 취소 전용 1개를 사용하며 초과 호출은 대기열 없이 resource_limit로 거부한다. 대상별 원격 작업 1개, 기존 코어의 시간·호출·출력 예산도 유지한다. STDIO 한 줄 512KiB 초과·유효하지 않은 UTF-8은 안전한 로그와 연결 종료로 처리한다. 도구 인자 JSON은 256KiB, 결과 JSON 봉투는 1MiB가 상한이다. 큰 결과는 성공으로 잘라 반환하지 않으며 원본 로컬 증거를 확인해야 한다.

현재 지원 검증 환경은 Linux x86_64·Python 3.12다. Pi에 MCP 패키지를 설치하지 않는다. 실제 AI 호스트와 장치의 전체 연결, Pi 메모리 목표·실물 오류 인수는 후속 단계다.
