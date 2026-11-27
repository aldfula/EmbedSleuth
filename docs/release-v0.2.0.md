# EmbedSleuth v0.2.0

기존 프로젝트에서 바로 스킬·MCP·훅을 준비하고, 사용자가 승인한 MPU6050 가속도·자이로·온도를 짧게 점검할 수 있다. Linux 개발 PC에 설치하며 Pi에 상주 패키지·LLM을 설치하지 않는다.

## 변경

- `embedsleuth-setup --in-place`: 현재 작업 폴더의 기존 README·소스·다른 MCP/훅을 보존한다. dry-run·외부 백업·반복 적용·정확한 v0.1.0 원본 스킬 이전을 지원한다. 충돌과 수정된 관리 파일은 강제 덮어쓰지 않는다.
- 명시적인 `sensor_check` 모드와 `prepare_sensor_sampling`·`sample_sensor_data`. 전체 MCP 도구는 11개다. 기본 read_only/passive의 범위는 유지한다.
- `embedsleuth-approve-sampling`: 구체적인 일회성 계획을 운영자가 자신의 로컬 터미널에서 직접 승인한다. MCP는 승인 생성·임의 명령·레지스터 쓰기 기능을 제공하지 않는다.
- 고정 5회 burst, 실제 설정에 따른 단위 변환, 조건부 SLEEP 해제·원래 전원값 복원 확인. 복원/전송 불확실성은 영속 격리하며 자동 재시도하지 않는다.
- 완료 보고서 조회는 같은 증거의 저장된 판정을 보존한다. 새 중단/원격 불확실성 복구는 계속 우선한다.

## 설치·업그레이드

```bash
pipx install --python python3.12 embedsleuth==0.2.0
```

기존 pipx 설치는 `pipx upgrade embedsleuth`를 사용한다. 작업할 기존 프로젝트에서 원래 외부 등록과 증거를 유지하며 실행한다.

```bash
embedsleuth-setup --in-place --config /ABSOLUTE/PATH/targets.json --dry-run
embedsleuth-setup --in-place --config /ABSOLUTE/PATH/targets.json
```

기존 MCP를 종료·재연결하고 바뀐 스킬/훅을 검토한다. 업그레이드 전 서버 프로세스는 새 코드를 자동으로 읽지 않는다. 등록·SSH 신뢰·훅 신뢰를 자동 부여하지 않는다. [설치 안내](https://github.com/aldfula/EmbedSleuth/blob/v0.2.0/docs/installation.md)와 [계획 승인·실패 절차](https://github.com/aldfula/EmbedSleuth/blob/v0.2.0/docs/v0.2.0-usage.md)를 따른다.

## 검증과 한계

실제 Pi 3 Model B / Debian 12 / aarch64 / Python 3.11.2의 0x68에서 운영자 승인 후 절전 해제·5회 데이터·원래 0x40 전원값 복원을 확인했다. 가속도 norm 1.01083–1.02537g, 칩 온도 20.8594–21.0006°C이며 경고는 없었다. 정확도·보정·정품·전기적 호환과 센서 전체 건강 상태를 확정하지 않는다.

오프라인·MCP/훅·패키지 설치와 pipx v0.1.0 이전 결과는 [검증 기록](https://github.com/aldfula/EmbedSleuth/blob/v0.2.0/docs/v0.2.0-verification.md)을 따른다. 새 기능의 실물 취소·오류 주입·전체 SSH 메모리·실제 모델의 승인 대기/재개·이미 활성인 센서 경로는 이번 실물 인수에 포함하지 않는다. 전원 차단·통신 손실·커널 정지 시 복원은 보장하지 않는다. PC 실행 검증은 Linux/Python 3.12이며 다른 OS와 PC Python 3.11은 미검증이다.

v0.1.0 태그·배포 파일·증거는 유지하며 새 버전으로 배포한다. 최종 파일·소스 커밋·SHA-256은 별도 `release-manifests/v0.2.0.json`으로 결합한다.
