# EmbedSleuth v0.2.0 설치

이 문서는 개발 저장소를 복제하지 않고 PyPI 패키지로 설치하는 절차다. **Linux 개발 PC**에서 실행한다. Raspberry Pi에는 설치하지 않는다. 검증한 PC는 Linux x86_64 / Python 3.12.14이고, Pi는 Pi 3 Model B / Debian 12 / aarch64 / Python 3.11.2다. 패키지의 최소 Python은 3.11이나 PC의 3.11 실행 시험은 아직 하지 않았다. Windows·macOS는 이번 배포의 지원 범위 밖이다.

배포 위치: [PyPI v0.2.0](https://pypi.org/project/embedsleuth/0.2.0/)과 동일 파일의 [GitHub Release](https://github.com/aldfula/EmbedSleuth/releases/tag/v0.2.0). 인터넷으로 패키지와 의존성을 내려받는다. AI 호스트의 설치·로그인·사용 한도는 별도이며 EmbedSleuth는 API 키나 LLM 클라이언트를 만들지 않는다.

v0.2.0의 실제 Pi·설치 인수 범위는 [검증 기록](v0.2.0-verification.md)을 따른다. 기존 pipx 설치는 `pipx upgrade embedsleuth`로 갱신한 뒤 작업할 프로젝트에서 `embedsleuth-setup --in-place --config /ABSOLUTE/PATH/targets.json`을 실행한다. 기존 MCP 연결을 종료·재연결해야 새 코드를 사용한다.

## 1. PC에 설치

pipx와 직접 가상환경을 만드는 방법 중 하나를 선택한다.

### pipx로 설치

pipx와 Python 3.12가 준비되어 있다면 다음과 같이 설치한다. pipx 자체의 설치는 [공식 설치 안내](https://pipx.pypa.io/stable/how-to/install-pipx.html)를 따른다.

```bash
pipx install --python python3.12 embedsleuth==0.2.0
embedsleuth-setup --help
```

pipx의 기본 Python이 3.11 이상이면 `pipx install embedsleuth`도 사용할 수 있다. pipx가 앱별 가상환경을 관리하므로 사용 시 직접 활성화하지 않는다. 설치가 끝나면 `embedsleuth-setup`, `embedsleuth-mcp`, `embedsleuth-hook`, `embedsleuth-approve-sampling` 명령을 제공한다. 명령을 찾지 못하면 `pipx ensurepath` 후 새 터미널에서 다시 확인한다. 이 명령은 셸의 PATH 설정을 갱신한다.

v0.1.0의 과거 검증: 2026-11-27에 Linux/Python 3.12.14/pipx 1.19.1의 pip 백엔드로 공개 PyPI의 `embedsleuth`를 설치했다. 배포본 SHA-256 일치, 세 명령어의 PATH 실행, 실제 setup 명령의 새 작업 폴더 생성, 생성 설정이 pipx Python을 사용하는 것, 의존성, 생산 MCP 9개 도구, 스킬, 훅 3개를 확인했다. 시험은 임시 PIPX_HOME·명령 경로에서 가상환경 활성화 없이 수행했다. 사용자 셸 설정·운영 설정·실제 Pi·모델은 사용하지 않았다. 다른 pipx 버전이나 uv 백엔드 실행 검증으로 확대 해석하지 않는다.

pipx 설치 후 **새 PC의 새 등록**을 준비하는 예다. 아래 경로가 아직 없어야 한다.

```bash
embedsleuth-setup \
  --in-place \
  --config "$HOME/.config/embedsleuth/targets.json" \
  --init-config \
  --state-directory "$HOME/.embedsleuth-state"
```

v0.2.0 공개 설치 검증: 2026-11-27에 별도 임시 pipx 환경에서 공개 PyPI의 버전 미지정 embedsleuth를 캐시 없이 설치해 0.2.0·최종 wheel SHA-256 일치를 확인했다. 명령어 4개·의존성·생산 MCP 11개·스킬·훅 3개·새/기존 프로젝트 적용이 통과했다. 실제 Pi·모델·사용자 전역 설치는 사용하지 않았다. 상세 결과는 [검증 기록](v0.2.0-verification.md)을 따른다.

기존 등록·증거가 있다면 `--init-config`와 `--state-directory`를 생략하고 기존 `--config`를 연결한다. 자세한 조건은 2절을 따른다. 이미 작업 폴더를 생성했다면 3절로 진행한다.

pipx 제거 시험에서 앱과 전용 가상환경은 제거되고 새 작업 폴더·대상 등록·증거 저장소의 파일 해시는 유지됐다. 제거하면 생성된 MCP·훅의 Python 경로는 사용할 수 없으므로, 호스트 연결을 먼저 해제하고 재설치·업데이트 시에는 기존 등록과 증거를 보존해 연결을 다시 검토한다.

### 직접 가상환경을 만들어 설치

Python 3.12와 venv를 준비한다. 아래 전용 가상환경 경로가 아직 없는 새 설치 기준이다. 시스템 Python에 sudo로 설치하지 않는다.

```bash
python3.12 -I -m venv "$HOME/.local/share/embedsleuth/venv"
source "$HOME/.local/share/embedsleuth/venv/bin/activate"
pip install embedsleuth==0.2.0
pip check
```

전용 가상환경이 활성화되어 있으면 `pip install embedsleuth`로 설치할 수 있다. 위 예는 검증한 버전을 명시한다. wheel에 MCP 서버·훅·스킬·참조·프로필·등록 예제가 포함된다. 일반 설치에는 pytest·PyYAML·빌드 도구가 필요 없다. 다른 장치나 기존 설치와 가상환경 경로를 공유하지 않는다.

v0.1.0의 과거 검증: 2026-11-27에 공개 PyPI에서 새 Linux/Python 3.12 가상환경으로 설치하고, 버전 제약 파일 없이 의존성·새 작업 폴더·생산 STDIO MCP 9개 도구·훅 3개를 검증했다. 설치된 wheel의 SHA-256은 기존 GitHub Release와 같다. 이 설치 확인에는 실제 Pi·모델·운영 설정을 사용하지 않았다.

### GitHub Release 파일로 설치하는 경우

Release에서 아래 세 파일을 같은 새 폴더에 내려받는다.

- `embedsleuth-0.2.0-py3-none-any.whl`
- `constraints-runtime.txt`
- `SHA256SUMS`

그 폴더에서 확인한다. 이 해시는 게시 파일의 손상·혼동을 확인하며 독립적인 서명은 아니다.

```bash
sha256sum --ignore-missing -c SHA256SUMS
```

wheel과 constraints-runtime.txt 두 줄 모두 OK인지 확인한다. 출처·해시가 다르면 설치하지 않는다.

직접 만든 전용 가상환경에 내려받은 wheel을 설치할 수도 있다. 런타임 버전을 고정해 기존 Release의 의존성을 재현하려면 내려받은 폴더에서 실행한다.

```bash
"$HOME/.local/share/embedsleuth/venv/bin/python" -I -m pip install \
  -c constraints-runtime.txt ./embedsleuth-0.2.0-py3-none-any.whl
"$HOME/.local/share/embedsleuth/venv/bin/python" -I -m pip check
```

## 2. 새 작업 폴더 만들기

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

## 3. 운영 대상 등록과 호스트 연결

작업 폴더의 targets.example.json에서 대상 객체를 참고해 비공개 등록 파일의 targets 배열을 편집한다. **원래 state_directory는 유지**한다. host·username·known_hosts_path·key_path·expected_bus·expected_addresses를 운영 환경에 맞춘다. 비밀번호나 개인키 내용을 넣지 않는다. `conflicting_use_excluded`는 처음에 false로 유지한다.

PC에서 운영자가 SSH 연결·호스트 키 지문·키 인증·Pi의 기존 I2C 도구와 권한을 준비한다. 설치 준비 명령이 키 등록·호스트 신뢰·권한 변경을 수행하지 않는다. 자세한 조건은 [운영 문서](https://github.com/aldfula/EmbedSleuth/blob/v0.1.0/docs/deployment.md)를 따른다.

Codex에서 새 작업 폴더를 연다. 프로젝트 설정과 스킬을 확인하고, 훅 세 개의 명령·경로를 검토한 뒤 호스트 UI에서 신뢰한다. 신뢰나 도구 승인이 필요하다는 호스트 요청을 검토한다. 설치 명령으로 신뢰를 자동 부여하지 않는다. 다른 프로젝트의 기존 설정에 추가하려면 해당 스킬과 MCP·훅 항목을 검토해 직접 병합한다. 생성기를 기존 프로젝트에 덮어쓰는 용도로 사용하지 않는다.

## 4. 사용 확인

먼저 장치 접근 없는 프로필 요청을 한다.

```text
$diagnose-mpu6050로 MPU6050 프로필의 식별 조건만 알려 줘. 장치 검사는 하지 마.
```

운영 등록·SSH 신뢰가 준비되면 실제 target_id로 환경 조사만 요청한다.

```text
$diagnose-mpu6050로 등록된 lab-pi의 환경만 passive로 조사해 줘. 센서 통신은 하지 마.
```

센서 식별 읽기는 그때의 예상 주소·다른 I2C 작업 중지 확인과 유효한 로컬 운영 선언이 필요하다. 설치가 끝났다는 이유로 자동 검사하지 않는다. v0.2.0의 명시적인 sensor_check는 별도 일회성 로컬 계획 승인 후 짧은 센서 데이터와 조건부 SLEEP 해제·복원을 지원한다. 전기적 호환 확정·일반 설정 변경은 제공하지 않는다. [절차와 한계](v0.2.0-usage.md)를 따른다.

## 업데이트와 제거

업데이트 전 AI 호스트의 기존 MCP를 종료하고 미완료 진단·격리를 확인한다. 새 버전은 별도 가상환경/새 작업 폴더에서 검토한 뒤 기존 등록·증거 저장소를 그대로 연결한다. 이전 코드를 로드한 MCP를 계속 사용하지 않는다. 생성된 훅 정의가 바뀌면 호스트의 신뢰 검토도 다시 거친다. 자동 업데이트·설정 덮어쓰기는 없다.

제거 시 호스트의 MCP·스킬·훅 등록을 먼저 해제하고 전용 가상환경·작업 폴더를 정리한다. 등록·증거·격리·SSH 키는 운영자가 별도로 보존한다. 증거 디렉터리 삭제를 격리 해제나 업그레이드 절차로 사용하지 않는다.

구성 근거: [공식 Codex 스킬 문서](https://learn.chatgpt.com/docs/build-skills), [패키지 데이터 포함](https://setuptools.pypa.io/en/stable/userguide/datafiles.html), [Python 패키징](https://packaging.python.org/en/latest/tutorials/packaging-projects/).
