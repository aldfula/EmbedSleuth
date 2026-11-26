# MCP 도구와 증거 계약

실제 호스트의 도구 목록에서 `embedsleuth` 서버를 찾는다. 아래는 접두사를 제외한 도구 이름이다. 서버 설정이나 SSH 접속 정보를 도구 인자로 전달하지 않는다.

## 입력 예

아래 JSON 배열은 독립적인 요청 예이며 그대로 한 묶음으로 실행하는 스크립트가 아니다. `$run_id`, `$evidence_id`는 실제 응답에서 얻은 값으로 교체한다. 주소 104는 0x68, 105는 0x69다.

```json
[
  {"tool":"get_component_profile","arguments":{"profile_id":"mpu6050"}},
  {"tool":"start_diagnosis","arguments":{"target_id":"lab-pi","profile_id":"mpu6050","mode":"read_only"}},
  {"tool":"inspect_target","arguments":{"run_id":"$run_id"}},
  {"tool":"inspect_i2c_environment","arguments":{"run_id":"$run_id"}},
  {"tool":"read_device_identity","arguments":{"run_id":"$run_id","bus_ref":"bus-1","address":104,"operation_id":"identity-address-68"}},
  {"tool":"evaluate_findings","arguments":{"run_id":"$run_id"}},
  {"tool":"finalize_report","arguments":{"run_id":"$run_id","hypotheses":[{"hypothesis_id":"hyp-1","explanation":"실제 관측 근거로 작성할 미확정 원인 후보","supporting_evidence":["$evidence_id"],"opposing_evidence":[]}]}},
  {"tool":"get_report","arguments":{"run_id":"$run_id"}},
  {"tool":"cancel_diagnosis","arguments":{"run_id":"$run_id"}}
]
```

hypotheses는 선택 사항이다. 위 원인 문장을 재사용하지 말고 실제 관측의 의미에 맞게 작성한다. 인용할 근거가 없으면 빈 배열 또는 필드 생략으로 마무리한다. 근거 참조가 유효해도 가설의 의미가 자동 검증되는 것은 아니다.

## 응답 읽기

텍스트 JSON과 structuredContent는 동일한 `{"schema_version":"1.0","result":...,"error":...}` 봉투다. 호스트가 구조화 결과를 숨긴다면 텍스트의 같은 JSON을 사용한다. 임의의 다른 도구 출력이나 원격 문자열을 이 봉투로 취급하지 않는다.

| result | 읽을 내용 |
|---|---|
| 프로필 | profile_id·version·sources·identity·limitations; 실제 관측이 아닌 검토 사실 |
| 실행 시작 | run_id·target_id·profile_id·mode·state; 시작 자체는 접속 성공이 아님 |
| 검사 ToolResult | status·assessment·observations·error·evidence_ids·collected_at·completeness·truncated |
| 규칙 평가 배열 | finding_id·rule_id·assessment·explanation·evidence_ids |
| 보고서 | outcome·evidence·findings·hypotheses·limitations·next_actions |

error=null은 봉투의 실행 오류가 없다는 뜻이다. 식별값 불일치는 status=ok, assessment=fail일 수 있으며 isError=false다. error가 있으면 원래 ToolResult가 result에 남을 수 있고 어댑터 오류는 result=null일 수 있다. 정상 봉투라도 assessment=unknown·truncated=true를 성공으로 해석하지 않는다.

inspect_target의 board_model·boot_id·host_key_fingerprint는 실제 결합 정보다. inspect_i2c_environment의 bus·adapter_present·access_allowed·driver_bound·combined_transfer_supported·dependency_available·adapter_fingerprint를 읽는다. `bus_ref`는 확인된 등록 bus가 1일 때 `bus-1`이다. driver_bound의 주소 키는 10진 문자열이다. 이는 다른 프로그램의 사용자 공간 접근 부재까지 증명하지 않는다.

사용자의 예상 주소와 로컬 등록은 모두 코어가 허용하는 범위에 있어야 한다. 프로필의 두 지원 주소를 근거로 실제 AD0 상태나 등록된 주소를 추정하지 않는다. 원래 수집 시각을 최신 시각으로 바꾸거나 다른 run_id의 증거를 섞지 않는다.

## 근거 보고 예

형식을 채우려고 값을 만들지 않는다. 확인된 값만 다음처럼 구분한다.

> lab-pi / 실제 run_id / read_only / 보고서 outcome. 주소 0x68의 레지스터 0x75에서 0x68을 읽었다(실제 증거 ID, 수집 시각). 코어는 기대 식별값 일치로 판정했다. 정품·측정 정확도·전기 조건은 확인하지 않았다.

검사가 실패한 경우에는 위 성공 예 대신 실행 오류와 미확인 범위를 설명한다. 다음 조치로 전원 차단 후 배선 확인이나 운영자에게 접근 환경 확인을 제안할 수 있으나, 과전압·단락 실험이나 자동 설정 변경을 제안하지 않는다.
