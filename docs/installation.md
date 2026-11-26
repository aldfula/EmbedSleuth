# EmbedSleuth v0.1.0 설치

이 문서는 개발 저장소를 복제하지 않고 GitHub Release의 패키지로 설치하는 절차다. **Linux 개발 PC**에서 실행한다. Raspberry Pi에는 설치하지 않는다. 검증한 PC는 Linux x86_64 / Python 3.12.14이고, Pi는 Pi 3 Model B / Debian 12 / aarch64 / Python 3.11.2다. 패키지의 최소 Python은 3.11이나 PC의 3.11 실행 시험은 아직 하지 않았다. Windows·macOS는 이번 배포의 지원 범위 밖이다.

배포 위치: [v0.1.0 GitHub Release](https://github.com/aldfula/EmbedSleuth/releases/tag/v0.1.0). PyPI에 게시하지 않았으므로 `pip install embedsleuth` 대신 아래 wheel을 설치한다. 인터넷으로 의존성을 내려받는다. AI 호스트의 설치·로그인·사용 한도는 별도이며 EmbedSleuth는 API 키나 LLM 클라이언트를 만들지 않는다.

## 1. 다운로드와 확인

Release에서 아래 세 파일을 같은 새 폴더에 내려받는다.

- `embedsleuth-0.1.0-py3-none-any.whl`
- `constraints-runtime.txt`
- `SHA256SUMS`

그 폴더에서 확인한다. 이 해시는 게시 파일의 손상·혼동을 확인하며 독립적인 서명은 아니다.

```bash
sha256sum --ignore-missing -c SHA256SUMS
```

wheel과 constraints-runtime.txt 두 줄 모두 OK인지 확인한다. 출처·해시가 다르면 설치하지 않는다.

## 2. PC 전용 가상환경에 설치

Python 3.12와 venv를 준비하고, 내려받은 폴더에서 실행한다. 시스템 Python에 sudo로 설치하지 않는다.

```bash
python3.12 -I -m venv "$HOME/.local/share/embedsleuth/venv"
"$HOME/.local/share/embedsleuth/venv/bin/python" -I -m pip install \
  -c constraints-runtime.txt ./embedsleuth-0.1.0-py3-none-any.whl
"$HOME/.local/share/embedsleuth/venv/bin/python" -I -m pip check
```

wheel에 MCP 서버·훅·스킬·참조·프로필·등록 예제가 포함된다. 일반 설치에는 pytest·PyYAML·빌드 도구가 필요 없다. 다른 장치나 기존 설치와 가상환경 경로를 공유하지 않는다.

## 3. 새 작업 폴더 만들기

**처음 설치하는 PC에서만** 빈 대상 등록과 새 증거 저장소를 만든다. 아래 세 경로가 아직 없어야 한다. 작업 폴더의 부모는 미리 존재해야 하며 상대 경로·`..`·심볼릭 링크가 들어간 경로는 받지 않는다.

```bash
"$HOME/.local/share/embedsleuth/venv/bin/python" -I -m embedsleuth.workspace \
  --directory "$HOME/embedsleuth-workspace" \
  --config "$HOME/.config/embedsleuth/targets.json" \
  --init-config \
  --state-directory "$HOME/.embedsleuth-state"
```

설치 후 제공되는 `embedsleuth-setup` 명령도 같은 기능이다. 생성 결과는 다음과 같다.

```text
~/embedsleuth-workspace/
  .agents/skills/diagnose-mpu6050/   스킬·참조·호스트 메타데이터
  .codex/config.toml               이 가상환경의 생산 MCP 명령
  .codex/hooks.json                Pre/Post/Stop 정의, 신뢰는 부여하지 않음
  targets.example.json            공개 예제
  README.md                       다음 절차
  INSTALLATION.json               로컬 설치 정보, Git 제외
```

대상 등록은 작업 폴더 밖의 0600 파일이고 증거 저장소는 0700이다. 등록 대상은 0개로 시작한다. 설치 준비는 Pi 접속·센서 검사·LLM 호출·훅 신뢰·사용자 전역 설정 변경을 수행하지 않는다. 기존 폴더/등록을 덮어쓰거나 기존 증거 저장소에 새 빈 등록을 만들지 않는다.

기존 EmbedSleuth 등록·증거가 있다면 `--init-config`와 `--state-directory`를 생략하고 **기존 등록 파일**을 재사용한다. 기존 target_id·state_directory·격리를 유지한다.

```bash
"$HOME/.local/share/embedsleuth/venv/bin/python" -I -m embedsleuth.workspace \
  --directory "$HOME/embedsleuth-workspace-new" \
  --config /ABSOLUTE/PATH/EXISTING/targets.json
```

## 4. 운영 대상 등록과 호스트 연결

작업 폴더의 targets.example.json에서 대상 객체를 참고해 비공개 등록 파일의 targets 배열을 편집한다. **원래 state_directory는 유지**한다. host·username·known_hosts_path·key_path·expected_bus·expected_addresses를 운영 환경에 맞춘다. 비밀번호나 개인키 내용을 넣지 않는다. `conflicting_use_excluded`는 처음에 false로 유지한다.

PC에서 운영자가 SSH 연결·호스트 키 지문·키 인증·Pi의 기존 I2C 도구와 권한을 준비한다. 설치 준비 명령이 키 등록·호스트 신뢰·권한 변경을 수행하지 않는다. 자세한 조건은 [운영 문서](https://github.com/aldfula/EmbedSleuth/blob/v0.1.0/docs/deployment.md)를 따른다.

Codex에서 새 작업 폴더를 연다. 프로젝트 설정과 스킬을 확인하고, 훅 세 개의 명령·경로를 검토한 뒤 호스트 UI에서 신뢰한다. 신뢰나 도구 승인이 필요하다는 호스트 요청을 검토한다. 설치 명령으로 신뢰를 자동 부여하지 않는다. 다른 프로젝트의 기존 설정에 추가하려면 해당 스킬과 MCP·훅 항목을 검토해 직접 병합한다. 생성기를 기존 프로젝트에 덮어쓰는 용도로 사용하지 않는다.

## 5. 사용 확인

먼저 장치 접근 없는 프로필 요청을 한다.

```text
$diagnose-mpu6050로 MPU6050 프로필의 식별 조건만 알려 줘. 장치 검사는 하지 마.
```

운영 등록·SSH 신뢰가 준비되면 실제 target_id로 환경 조사만 요청한다.

```text
$diagnose-mpu6050로 등록된 lab-pi의 환경만 passive로 조사해 줘. 센서 통신은 하지 마.
```

센서 식별 읽기는 그때의 예상 주소·다른 I2C 작업 중지 확인과 유효한 로컬 운영 선언이 필요하다. 설치가 끝났다는 이유로 자동 검사하지 않는다. 센서 측정 데이터 수집·전기적 호환 확정·설정 변경은 이 MVP 기능이 아니다.

## 업데이트와 제거

업데이트 전 AI 호스트의 기존 MCP를 종료하고 미완료 진단·격리를 확인한다. 새 버전은 별도 가상환경/새 작업 폴더에서 검토한 뒤 기존 등록·증거 저장소를 그대로 연결한다. 이전 코드를 로드한 MCP를 계속 사용하지 않는다. 생성된 훅 정의가 바뀌면 호스트의 신뢰 검토도 다시 거친다. 자동 업데이트·설정 덮어쓰기는 없다.

제거 시 호스트의 MCP·스킬·훅 등록을 먼저 해제하고 전용 가상환경·작업 폴더를 정리한다. 등록·증거·격리·SSH 키는 운영자가 별도로 보존한다. 증거 디렉터리 삭제를 격리 해제나 업그레이드 절차로 사용하지 않는다.

구성 근거: [공식 Codex 스킬 문서](https://learn.chatgpt.com/docs/build-skills), [패키지 데이터 포함](https://setuptools.pypa.io/en/stable/userguide/datafiles.html), [Python 패키징](https://packaging.python.org/en/latest/tutorials/packaging-projects/).
