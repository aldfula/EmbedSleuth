# Phase 2 코어 사용

현재 제공하는 실행 인터페이스는 Python 라이브러리다. Phase 3에서 이 코어를 로컬 STDIO MCP 서버에 연결한다. 별도 대화형 CLI나 자체 LLM 연결은 없다.

## 신뢰된 로컬 초기화

다음은 운영자가 실제 접속 정보를 검토한 뒤 작성하는 예다. **실행하면 등록한 Pi에 SSH로 접속한다.** 이번 자동 시험에서는 이 예를 실제 장치에 실행하지 않았다. 호스트 키는 독립적으로 확인한 파일을 준비해야 하며 코드가 자동 등록하지 않는다.

```python
from pathlib import Path

from embedsleuth.core import DiagnosticCore
from embedsleuth.storage import RunStore
from embedsleuth.targets import RegisteredTarget, TargetRegistry
from embedsleuth.transport import ParamikoTransport


def main():
    target = RegisteredTarget(
        target_id="lab-pi",
        host="192.0.2.10",  # 예시 주소: 실제 등록 주소로 교체
        username="pi",
        known_hosts_path=Path("/absolute/path/to/known_hosts"),
        key_path=Path("/absolute/path/to/id_ed25519"),
        expected_bus=1,
        expected_addresses=(0x68, 0x69),
        conflicting_use_excluded=False,
    )
    registry = TargetRegistry((target,))
    # 상위 디렉터리는 먼저 준비한다. 최종 state 디렉터리는 0700으로 생성한다.
    with RunStore(Path.home() / ".embedsleuth-state") as store:
        core = DiagnosticCore(registry, ParamikoTransport(registry), store)
        run = core.call("start_diagnosis", {
            "target_id": "lab-pi", "profile_id": "mpu6050", "mode": "passive",
        })
        core.call("inspect_target", {"run_id": run["run_id"]})
        core.call("inspect_i2c_environment", {"run_id": run["run_id"]})
        report = core.call("finalize_report", {"run_id": run["run_id"]})
        print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
```

실행 진입점의 `__main__` 가드는 PC의 SSH 감독 프로세스가 spawn 방식으로 시작하는 데 필요하다. stdin·대화형 Python 대신 저장한 스크립트나 향후 MCP 서버 진입점을 사용한다. Windows·macOS는 아직 지원 시험 대상이 아니며 파일 잠금·권한 구현은 Linux 전용이다.

## 대상과 저장소의 운영 단위

한 물리 장치는 하나의 안정적인 target_id와 공유 상태 저장소로 관리한다. 레지스트리는 정규화한 동일 호스트·포트의 중복 등록을 거부한다. 계정이나 버스만 바꿔 같은 접속 대상을 다시 등록할 수 없다. 별도 DNS/IP 별칭·다른 포트의 터널이 같은 장치인지 자동으로 알아내지는 않는다.

기존 격리나 잠금을 피하려고 target_id를 바꾸거나 별도 저장소를 사용하지 않는다. 여러 호스트·계정에서 같은 물리 장치를 진단해야 한다면 공유 조정 체계가 필요하며 현재 MVP 범위를 넘는다.

## 제한된 식별 읽기

운영자가 다른 I2C 사용자 공간 작업을 배제했다고 확인한 경우에만 신뢰된 등록의 `conflicting_use_excluded=True`를 사용한다. 이는 자동 관측이나 상호 배제를 증명하지 않는다. 모델의 도구 인자로 이 값을 받을 수 없다.

`read_only` 실행에서 대상·환경 검사를 마친 뒤 다음 요청을 사용할 수 있다.

```python
result = core.call("read_device_identity", {
    "run_id": run_id,
    "bus_ref": "bus-1",
    "address": 0x68,
    "operation_id": "identity-address-68",
})
```

버스 참조는 등록한 버스와 일치해야 한다. 레지스터 0x75·길이 1·결합 전송은 코드와 검토 프로필로 고정한다. 동일 operation_id와 인자는 원래 관측·시각을 반환하며 다른 인자로 재사용할 수 없다. 원격 오류 후 새로운 ID로 자동 재시도하지 않는다.

필요한 기존 Python 3·i2ctransfer·장치 접근 권한이 없으면 검사 불가로 보고한다. 도구가 이를 설치하거나 sudo·권한 변경을 수행하지 않는다.

## 저장·취소·복구

저장소는 `runs/<run_id>/`의 상태·감사 JSONL·보고서와 `quarantine/`의 대상 격리를 관리한다. 파일 0600·디렉터리 0700과 소유권을 검사하며 안전하지 않은 기존 권한을 자동 변경하지 않는다. 로컬 기록은 삭제·회전 기능을 아직 제공하지 않으며 운영자가 보존 정책을 정해야 한다.

`cancel_diagnosis`는 독립적인 취소 표시를 저장한다. 진행 중이면 마지막 확정 증거를 포함한 interrupted 스냅샷을 반환하고 작업자가 최종 상태를 기록한다. 원격 완료를 확인하지 못하면 대상 격리를 유지한다. SSH 종료를 I2C 커널 작업 종료로 간주하지 않는다.

재시작 시 미완료 의도는 재실행하지 않는다. 다른 서버가 시작 당시 바쁜 실행을 건너뛰었다면, 이후 대상 잠금을 잡을 때 같은 대상의 모든 실행을 복구해 작업자의 후속 중단을 확인한다. 현재 복구는 저장된 전체 실행 수에 비례하는 순회이므로 장기 운영의 보존·성능 정책이 필요하다. 기존 실행의 완료 캐시와 보고서는 조회할 수 있지만 새 검사는 새 실행이 필요하다. 격리는 새 실행·재시작으로 해제되지 않으며 현재 공개 해제 API는 없다. 실제 원격 종료를 운영자가 별도 확인하고 복구 절차를 정하는 기능은 후속 인수 과제다.

`SessionError`는 잠금 충돌 등 세션 오류의 `code`를 제공한다. 요청 오류는 Pydantic 검증 오류, 안전하지 않은 저장소는 `StorageError`, 파일 시스템 실패는 `OSError`로 전달될 수 있다. Phase 3 MCP 어댑터는 이를 비밀을 포함하지 않는 구조화 오류로 변환해야 한다. 원격 검사 실패는 `ToolResult.error`와 부분 보고서에 남는다.

## 예산과 지원 한계

- 도구 전체 원격 시간 10초, 실행 시작 후 경과 시간 120초, 실행 유효 기간 600초, 원격 호출 최대 12회가 기본값이다. 모델이 다음 검사를 고르는 대기 시간도 120초에 포함하는 보수적 구현이다.
- 환경 관측은 기본 30초만 식별 읽기의 선행 조건으로 사용할 수 있다.
- SSH stdout·stderr 각각 64KiB, Pi I2C 명령 stdout 1KiB·stderr 4KiB, 명령 실행 3초를 제한한다.
- PC의 감독 프로세스가 DNS·연결·인증·실행을 포함해 기한을 감시한다. 종료 정리에 최대 0.2초의 제한된 대기와 OS 스케줄링 지연이 추가될 수 있다. 지연된 성공 응답은 버린다.
- 감독·취소 콜백의 신뢰된 대체 구현은 시험 주입용이다. 운영 경로는 기본 ParamikoTransport를 사용한다.
- Pi 3의 OS·커널·32/64비트 지원, 실제 I2C와 프로세스 종료, 추가 peak RSS 64MiB 목표는 Phase 6 실물 시험 전이다.
