# PyPI 게시 절차

## v0.2.0

소스 `2d769c7348661b56b96c36291325809ce9041061`의 최종 파일과 별도 [매니페스트](../release-manifests/v0.2.0.json)를 사용한다. v0.1.0 태그·파일·매니페스트를 교체하지 않는다. [새 Release](https://github.com/aldfula/EmbedSleuth/releases/tag/v0.2.0)에 최종 wheel·sdist·constraints·체크섬·설치 안내를 게시했다.

워크플로의 저장소·main 검사, `publish=false` 기본값, 고정된 공식 Action SHA, 검증 작업과 OIDC 업로드 작업의 권한 분리를 유지한다. 기존 Trusted Publisher의 `aldfula/EmbedSleuth`·`pypi-publish.yml`·`pypi` environment를 재사용하며 새 권한을 부여하지 않는다. environment는 main만 허용한다. 코드 checkout과 설치 검사는 게시 자격 증명이 없는 검증 작업에서 수행하며 업로드 작업은 검증된 두 배포 파일만 받는다.

```bash
gh workflow run pypi-publish.yml --repo aldfula/EmbedSleuth --ref main -f publish=false
# 검증 성공 후, 사용자가 승인한 게시를 실행한다.
gh workflow run pypi-publish.yml --repo aldfula/EmbedSleuth --ref main -f publish=true
```

[검증 전용 실행 45932392614](https://github.com/aldfula/EmbedSleuth/actions/runs/45932392614)은 `c5d6b2f`에서 성공했고 publish는 skipped였다. GitHub Release의 최종 바이트·패키지 이름/버전·고정 런타임 설치·pip check·생산 MCP 11개·스킬·훅·기존 프로젝트 적용을 확인했다. 실제 Pi·LLM·운영 설정을 사용하지 않았다. 이어 같은 커밋의 [게시 실행 45932476251](https://github.com/aldfula/EmbedSleuth/actions/runs/45932476251)도 validate/publish 모두 성공했다. [PyPI v0.2.0](https://pypi.org/project/embedsleuth/0.2.0/)에 같은 두 배포 파일을 게시했다. 공개 PyPI 파일 해시 일치·버전 미지정 새 pipx 설치·명령어 4개/MCP 11개/스킬/훅/기존 프로젝트 적용 통과 결과는 [v0.2.0 검증 기록](v0.2.0-verification.md)에 기록한다.

게시 후 공개 PyPI JSON의 두 파일 해시를 매니페스트와 비교한다. 새 설치 환경에서 이름으로 설치해 버전·실제 다운로드 wheel 해시·의존성·설치본 setup/MCP/스킬/훅을 검사한다. 같은 버전 파일을 삭제해 교체하거나 자동 skip-existing으로 차이를 숨기지 않는다. 부분 업로드나 오류는 공개 파일 목록부터 확인한다.

## v0.1.0 게시 기록 (보존)

2026-11-27에 기존 GitHub Release의 v0.1.0 wheel·sdist를 [PyPI](https://pypi.org/project/embedsleuth/0.1.0/)에 게시했다. 공개 인덱스에서 패키지 이름으로 설치하고 MCP·스킬·훅 검증까지 통과했다. Linux 개발 PC의 전용 가상환경에서 `pip install embedsleuth`로 설치할 수 있다.

### 게시 파일과 경계

기존 [v0.1.0 Release](https://github.com/aldfula/EmbedSleuth/releases/tag/v0.1.0)의 wheel과 sdist를 그대로 게시한다. 빌드 소스는 `1347849831e81b2a07a4a5fe5c27cd79c716f832`이며, [검토한 매니페스트](../release-manifests/v0.1.0.json)가 파일 이름·SHA-256·패키지 이름·버전을 고정한다. 새 빌드나 태그 변경을 수행하지 않는다. 런타임 제약 파일은 설치 검증에만 사용하고 PyPI에는 올리지 않는다.

워크플로는 공개 배포 파일 세 개를 내려받아 검증한 뒤 배포본 wheel을 설치하고, 의존성 일관성·새 작업 폴더 생성·생산 STDIO MCP·훅을 검사한다. 이 검증에는 Pi 접속·센서 통신·LLM·운영 설정을 사용하지 않는다. 업로드 전용 작업은 검증 작업과 분리하며 코드 checkout 없이 검증 산출물만 전달받는다.

### 소유 계정과 Trusted Publisher

PyPI 소유 계정의 이메일 인증과 사이트에서 요구하는 2단계 인증을 운영자가 직접 완료한다. 비밀번호·인증 코드·복구 코드·API 토큰을 저장소나 채팅에 넣지 않는다.

프로젝트가 아직 없다면 PyPI 계정의 **Publishing → Add a new pending publisher → GitHub**에서 아래 값을 사용한다.

| 항목 | 값 |
| --- | --- |
| PyPI Project Name | `embedsleuth` |
| Owner | `aldfula` |
| Repository name | `EmbedSleuth` |
| Workflow name | `pypi-publish.yml` |
| Environment name | `pypi` |

등록은 해당 GitHub 워크플로에 PyPI 프로젝트 게시 권한을 부여하는 작업이다. 운영자가 정확한 값을 검토하고 승인한다. Pending Publisher는 프로젝트 이름을 예약하지 않으며 첫 업로드에서 이름 사용 가능 여부가 최종 결정된다. 이미 프로젝트가 생겼다면 그 프로젝트의 Publishing 설정에서 같은 값으로 등록해야 한다.

GitHub의 `pypi` environment는 `main` 브랜치만 허용한다. 워크플로 자체도 원본 저장소와 `main`을 검사한다. PyPI 게시 작업에만 `id-token: write`를 주고 단기 OIDC 인증을 사용한다. 저장된 PyPI API 토큰은 사용하지 않는다. 유지보수자는 저장소·워크플로·environment의 접근을 보호해야 한다. 이 제한은 `main` 변경 권한이 있는 유지보수자를 방어하는 장치는 아니다.

### 검증과 게시 실행

[게시 워크플로](../.github/workflows/pypi-publish.yml)는 수동 실행만 허용하며 `publish=false`가 기본값이다. 먼저 검증만 실행한다.

```bash
gh workflow run pypi-publish.yml --repo aldfula/EmbedSleuth --ref main -f publish=false
```

계정 인증·Publisher 등록·검증 성공을 확인한 뒤 게시를 실행한다.

```bash
gh workflow run pypi-publish.yml --repo aldfula/EmbedSleuth --ref main -f publish=true
```

게시 작업은 GitHub OIDC와 배포 provenance를 사용한다. PyPI의 동일 버전·동일 파일명 재업로드는 기대하지 않는다. 이미 게시된 파일을 삭제해 교체하지 말고, 실패 시 PyPI의 파일 목록과 해시부터 조회한다. 부분 업로드라면 원인을 확인하고 업로드되지 않은 파일만 별도 검토한다. 자동 `skip-existing`으로 해시 차이를 숨기지 않는다.

게시 후 공개 PyPI JSON의 버전·두 파일의 SHA-256이 매니페스트와 같은지 확인한다. 새 가상환경에서 PyPI 인덱스로 `embedsleuth==0.1.0`을 설치하고 `pip check`와 `scripts/check_release_install.py`를 실행한다. 성공 후 설치 문서에 패키지 이름으로 설치하는 명령을 안내한다. 기존 태그와 Release 첨부 파일은 수정하지 않는다.

### 검증 기록

- 게시 파일 검증 회귀 12개 통과. 변조·누락·링크·다른 메타데이터·허용되지 않은 파일 목록을 거부한다.
- 전체 자동 회귀 **781 passed in 64.62s**, 경고 오류 처리. 실제 Pi·모델 호출 없음.
- 기존 wheel과 sdist의 `twine check --strict` 통과.
- GitHub [검증 실행 45914544501](https://github.com/aldfula/EmbedSleuth/actions/runs/45914544501) 성공(`e04c680`, validate 13초). 기존 공개 Release 다운로드·해시·배포 wheel 설치·의존성·MCP/훅 검사와 산출물 전달이 통과했으며 `publish` 작업은 실행하지 않았다.
- GitHub `pypi` environment의 허용 배포 브랜치는 `main` 한 개임을 확인했다. 운영자의 계정 인증 완료와 구체적인 게시 권한 승인 후 Pending Publisher를 등록했다.
- 실제 [게시 실행 45915581385](https://github.com/aldfula/EmbedSleuth/actions/runs/45915581385) 성공(`7b2fc2f`, validate 15초, publish 20초). OIDC 인증과 디지털 attestation을 사용했다. GitHub 작업 커밋과 배포 파일의 소스 커밋은 서로 다르며 후자는 매니페스트에 고정되어 있다.
- 공개 PyPI JSON의 이름·버전·파일 두 개·SHA-256이 매니페스트와 일치하고 yanked 상태가 아님을 확인했다. GitHub Release와 같은 바이트다.
- 저장소 밖의 새 Linux/Python 3.12.14 가상환경에서 `pip --isolated install --index-url https://pypi.org/simple embedsleuth==0.1.0` 성공. 런타임 제약 파일을 사용하지 않았다. 설치 보고서의 wheel 출처는 `files.pythonhosted.org`이고 SHA-256도 일치했다. 선택된 주요 의존성은 Pydantic 2.14.4, Paramiko 4.0.0, MCP 2.3.0, AnyIO 4.16.0이다.
- `pip check`와 실제 `scripts/check_release_install.py` 성공: 스킬 자료·새 작업 폴더·생산 MCP 도구 9개·Pre/Post/Stop 훅 3개. 등록 대상 0개·장치 검사 0회·모델 0턴·훅 신뢰 부여 없음.
- pipx 추가 인수도 통과했다(Linux/Python 3.12.14, pipx 1.19.1, pip 백엔드). 공개 PyPI에서 버전 미지정 `embedsleuth` 설치로 0.1.0과 동일 wheel 해시를 확인했다. 임시 PIPX_HOME/BIN 경로를 사용하고 가상환경을 활성화하지 않은 상태에서 앱 3개 실행·실제 setup·pipx Python의 MCP 설정·의존성·기존 설치 검사 전체를 검증했다. `pipx uninstall embedsleuth` 후 앱/가상환경 제거와 새 작업 폴더/등록/증거 파일의 해시 보존을 확인했다. 운영 설정·전역 pipx 설치·사용자 셸 설정을 변경하지 않았다. [pipx 설치 안내](installation.md#pipx로-설치)
- 기존 배포 파일을 보존했으므로 PyPI v0.1.0 설명은 GitHub 설치를 안내하던 README다. 설명 본문의 상대 문서 링크는 PyPI에서 열리지 않으며 Metadata의 Documentation 링크 또는 [최신 설치 문서](https://github.com/aldfula/EmbedSleuth/blob/main/docs/installation.md)를 사용한다. 이 문서 한계 때문에 이미 게시된 파일을 바꾸거나 재업로드하지 않는다.

근거: [PyPI Pending Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/), [Trusted Publisher 설정](https://docs.pypi.org/trusted-publishers/adding-a-publisher/), [공식 PyPI 게시 Action](https://github.com/pypa/gh-action-pypi-publish).
