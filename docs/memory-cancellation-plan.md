# 잔여 취소·전체 메모리 인수 계획

기준일: 2026-11-25. 사용자는 0x69 실물 시험을 이번 인수 범위에서 제외했다. 0x69의 소프트웨어 계약·모의 시험은 유지하지만 실물 검증 완료라고 표시하지 않는다.

## 센서 직접 읽기 범위

0x68의 WHO_AM_I 직접 읽기는 이미 실행했다. run-35337e2573194230ada437b6d623057a에서 버스 1·주소 0x68·레지스터 0x75·길이 1바이트를 한 번 읽어 0x68을 얻었고 complete/identity-match pass로 저장됐다. 오류 3종 시험에서의 센서 전송 0회와 구분한다. 가속도·자이로 측정값 연속 수집은 현재 MVP 범위 밖이다.

## 실행 중 취소

개발용 [취소 인수 실행기](../scripts/run_cancel_acceptance.py)는 기존 대상·저장소·생산 MCP를 사용한다. 추가 SSH 세션은 /proc를 읽기만 한다. 새 같은 사용자 Python 프로세스의 argv를 메모리에서만 검토된 remote_probe 소스 해시와 비교하고, 원문은 출력·저장하지 않는다.

- 실제 inspect_target 요청을 시작하고 정확한 원격 검사기를 관측한 뒤 cancel_diagnosis를 한 번 요청한다. 센서 전송·프로세스 신호·설정 변경·고의 지연 주입은 하지 않는다.
- 기존 요청을 끝까지 회수하고 get_report로 영속 보고서를 확인한다. 원격 감사의 before/after 결합과 pending_operation=null을 검사한다.
- interrupted, 완료 불명확, 영속 격리를 기대한다. 실행 중 취소는 현재 전송 계층이 완료를 보증하지 못하므로 격리는 정상적인 안전 결과다. 격리를 지우거나 새 대상·저장소로 우회하지 않는다.
- 원격 관측 뒤 취소가 서버에 도착하기 전 검사가 끝날 수 있다. 따라서 관측 시점의 원격 실행과 미완료 MCP 요청은 확인하지만, 취소 수신 순간의 원격 실행 여부·강제 종료·미관측 자식 전체 종료를 통과했다고 주장하지 않는다.
- 관측한 PID와 시작 tick의 종료는 8초 후 확인한다. 조건이 부족하면 inconclusive로 보존하고 하드웨어 검사를 자동 반복하지 않는다.

일반 격리 해제 MCP API는 없다. 2026-11-26 닫힌 passive 보드 취소 한 건에만 적용하는 [로컬 운영자 복구 절차](passive-recovery-plan.md)를 구현·시험했다. 사용자가 별도 승인한 뒤 적용했으며, 이 인수 실행기 자체가 운영 승인을 대신하지 않는다.

## 전체 메모리 사전 확인 결과

읽기 전용 사전 확인에서 다음을 관측했다. 로컬 원본 요약은 Git에서 제외한다.

- cgroup v2가 마운트돼 있지만 controllers는 cpuset/cpu/io/pids이며 memory가 없다.
- 기존 SSH·사용자 그룹에 memory.current와 memory.peak가 없다. /proc/cgroups의 memory 항목 부재만으로 v2 기능이 없다고 판정하지 않는다(6.12에서 v1 지원을 제외할 수 있음).
- 실제 부팅 옵션에 cgroup_disable=memory가 있다.
- CONFIG_MEMCG=y다. /boot/firmware는 vfat 부트 마운트이며 config.txt에 cmdline/os_prefix/include 재지정이 없다. /boot/firmware/cmdline.txt는 131바이트·한 줄이고 cgroup_disable=memory가 없다. /boot/cmdline.txt는 92바이트·여러 줄인 안내 파일이다. 실제 부팅의 disable 토큰은 cmdline.txt 편집 흔적이라고 단정하지 않는다.

따라서 현 상태의 전체 peak 판정은 unavailable/unknown이다. 기존 검사기+단일 자식 RSS 상한 30,796KiB와 /proc 표본 최대 31,812KiB를 전체 peak 통과로 승격하지 않는다.

## 승인된 변경 절차

2026-11-26 사용자가 Pi 연결 후 진행을 요청해 백업·계측 활성화·재부팅 범위를 승인했다. 아래 실행 도구를 준비했으며, 관리자 작업은 본인 터미널에서 수행한다. 아직 실제 편집·재부팅을 수행한 것은 아니다. 격리 해제나 메모리 기준 변경을 이 승인에 포함하지 않는다.

1. 읽기 전용으로 확인한 CONFIG_MEMCG=y와 부트 마운트·config 선택 정보를 편집 직전에 재확인한다. 환경이 달라지면 편집하지 않는다.
2. 운영자가 본인 Pi 터미널에서 sudo를 사용한다. /boot/firmware/cmdline.txt의 원문·해시·권한·소유자를 보존하는 백업을 만든 뒤, 기존 한 줄 끝에 cgroup_enable=memory를 한 번만 추가한다. Pi 6.12의 기본 DTB disable 설정은 뒤의 enable 설정으로 재정의된다. 다른 옵션·한 줄 구성을 유지하고 편집 직전 해시가 달라지거나 충돌하는 옵션이 발견되면 중단한다. 재부팅 전에 복구 명령을 제공한다.
3. 사용자 승인 후 한 번 재부팅하고 기존 호스트 키로 접속한다. memory controller·memory.peak가 생겼는지 확인한다. 실패하면 측정을 통과시키지 않는다. SSH 복구가 안 되면 로컬 콘솔/부트 매체로 백업을 복원할 수 있어야 한다.
4. memory.peak의 측정 범위는 실제 SSH 인증 프로세스·세션·검사기·자식을 모두 포함하도록 커널 cgroup 경로로 검증한다. 기존 ssh.service와 사용자/session 그룹 간 이동이 있어 세션 한 개의 peak만으로 전체를 주장하지 않는다. 계측을 위해 서비스 재시작·프로세스 이동·추가 권한 설정이 필요하면 구체적 변경을 다시 검토한다.
5. passive와 0x68 식별을 별도 측정한다. 식별 전에는 현재 주소·다른 I2C 프로그램 중지 확인이 필요하다. 취소로 격리돼 있다면 우회하지 않고 감사 가능한 복구가 먼저다.
6. 부팅 옵션 원상복구를 선택하면 백업을 확인·복원하고 두 번째 재부팅이 필요하다. 메모리 계측 기능을 유지하는 선택은 영구 설정 변경에 해당한다.

### 수치 정의

Linux의 memory.peak는 cgroup과 하위 그룹에 과금된 메모리의 최대값이다. 공유 페이지의 과금 귀속·페이지 캐시·커널 메모리가 관련되므로 프로세스 RSS 합계의 정확한 동의어가 아니다. 전체 cgroup 범위가 검증돼도 기존 추가 RSS 64MiB 목표와 별도 지표로 보고하며, 기존 요구사항을 조용히 바꾸지 않는다. 필요하면 운영자가 인수 기준을 전체 과금 메모리로 명시적으로 조정한다.

참고: [Linux cgroup v2 — memory.peak와 메모리 소유권](https://docs.kernel.org/admin-guide/cgroup-v2.html). Pi의 기본 DTB disable 설정과 enable 재정의에 대한 [Raspberry Pi 커널 유지관리자 설명](https://github.com/raspberrypi/linux/issues/6980)도 검토했다. 재부팅 후 실제 카운터 확인을 통과로 대신하지 않는다.

## 준비된 관리자 실행기

[prepare_memory_acceptance.py](../scripts/prepare_memory_acceptance.py)는 본인 PC 터미널에서 실행한다. 기존 등록 대상·키·호스트 신뢰를 그대로 사용하고, SSH의 대화형 sudo 입력을 상속한다. 비밀번호를 입력받아 저장하거나 캡처하지 않는다. 진단 MCP의 읽기 전용 정책과 도구 목록은 바꾸지 않는다.

```bash
/absolute/project/.venv/bin/python -I /absolute/project/scripts/prepare_memory_acceptance.py \
  --config /absolute/private/targets.json --target lab-pi \
  --confirm-boot-change-and-reboot \
  --output /absolute/private/memory-boot-preparation.json
```

- [관리자 가드](../evals/diagnose-mpu6050/memory_boot_guard.py)는 현재 부트·원본 해시·소유권·단일 줄·CONFIG_MEMCG·부트 마운트·config 선택을 검사한다. 기존 cgroup 옵션이 있으면 검토 없이 중복 추가하지 않는다.
- 백업 파일은 cmdline.txt.embedsleuth-원본해시앞16자리.bak다. 기존 백업이 다르면 덮어쓰지 않는다. 원본 백업과 임시 파일을 fsync·검증한 뒤 같은 디렉터리에서 원자 교체한다. 교체 직전 원문이 달라지면 중단한다. vfat의 기존 마운트 소유권·모드 정책을 유지하며 파티션 마운트 옵션은 바꾸지 않는다.
- 관리자 준비의 동시 실행을 막는 /run/lock/embedsleuth-memory-boot.lock은 root 소유 0600이며 잠금 inode를 유지한다. sudoers·패키지·커널·SSH 설정은 바꾸지 않는다.
- 검증을 통과한 뒤 복구 명령을 표시하고 한 번 재부팅한다. PC 결과에는 관리자 실행 전에 계획을 먼저 fsync한다. SSH 단절 후의 연결 대기는 준비 작업의 재실행이 아니다.
- 새 부트 ID·원본 백업 해시·변경된 파일 해시·memory controller를 확인해야 memory_accounting_enabled다. 이는 계측 준비의 성공이며 전체 메모리 예산 통과가 아니다. 실패하면 결과와 백업을 검토하고 명령을 자동 반복하지 않는다.
- 실행 중 취소의 영속 격리는 이 도구에서 유지한다. 재부팅만으로 저장소의 격리를 자동 해제하지 않는다.

2026-11-26 실물의 읽기 전용 사전 확인에서도 계측은 꺼져 있었고 예상 부트 파일은 131바이트였다. 준비된 가드의 비특권 preflight를 실제 Pi에서 실행해 백업 경로와 변경 후 152바이트 계획을 확인했다. 백업·편집·재부팅은 관리자 실행 단계로 남아 있다. 로컬 안전성 시험 23개는 Pi·sudo 없이 통과했다.

### 관리자 실행 완료 — 2026-11-26

사용자가 본인 터미널에서 준비 실행기를 완료했다. 저장된 결과와 실제 Pi의 독립 재확인을 대조해 새 부트 ID, 원본 백업 SHA-256 일치, 변경된 cmdline SHA-256 일치, memory controller와 memory.peak 생성을 확인했다. 부트 파일은 131→152바이트이고 cgroup_enable=memory 한 토큰을 추가했다. 원본 백업은 유지하며 추가 재부팅·설정 복원은 하지 않았다.

격리는 변경하지 않았다. 현재 부팅 설정에 cgroup_disable=memory와 cgroup_enable=memory가 함께 보이는 것은 뒤의 enable이 앞의 기본값을 재정의한 상태이며, 실제 memory controller로 활성화를 확인했다. 터미널의 locale 경고가 계측 실패로 이어진 것은 아니다. 측정 지표의 정의와 격리 복구 승인은 별도로 유지한다.


### 실제 진단 계측 완료

별도 복구 승인 후 새 생산 MCP의 passive/0x68 식별을 완료했다. 전체 계측 범위는 SSH service와 user.slice의 과금 메모리 생성 이후 상한 합이며, 각각 39,522,304바이트(37.69MiB)·39,813,120바이트(37.97MiB)다. 검사기 그룹·sshd 그룹이 집계 범위 안에 있었고 계측기와 다른 세션도 포함했다. 기존 추가 RSS 합계와 혼동하지 않으며 그 엄격한 지표는 unknown으로 유지한다. 구체적인 실행·증거·복구 종료는 [인수 기록](phase6-acceptance.md)에 보존한다.
