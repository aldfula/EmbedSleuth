# PyPI 게시 절차

현재 v0.1.0은 GitHub Release에 게시되어 있으며, PyPI 게시를 준비 중이다. PyPI 업로드와 공개 인덱스 설치 검증 전에는 `pip install embedsleuth`가 가능하다고 간주하지 않는다.

## 게시 파일과 경계

기존 [v0.1.0 Release](https://github.com/aldfula/EmbedSleuth/releases/tag/v0.1.0)의 wheel과 sdist를 그대로 게시한다. 빌드 소스는 `1347849831e81b2a07a4a5fe5c27cd79c716f832`이며, [검토한 매니페스트](../release-manifests/v0.1.0.json)가 파일 이름·SHA-256·패키지 이름·버전을 고정한다. 새 빌드나 태그 변경을 수행하지 않는다. 런타임 제약 파일은 설치 검증에만 사용하고 PyPI에는 올리지 않는다.

워크플로는 공개 배포 파일 세 개를 내려받아 검증한 뒤 배포본 wheel을 설치하고, 의존성 일관성·새 작업 폴더 생성·생산 STDIO MCP·훅을 검사한다. 이 검증에는 Pi 접속·센서 통신·LLM·운영 설정을 사용하지 않는다. 업로드 전용 작업은 검증 작업과 분리하며 코드 checkout 없이 검증 산출물만 전달받는다.

## 소유 계정과 Trusted Publisher

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

## 검증과 게시 실행

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

## 검증 기록

- 게시 파일 검증 회귀 12개 통과. 변조·누락·링크·다른 메타데이터·허용되지 않은 파일 목록을 거부한다.
- 전체 자동 회귀 **781 passed in 64.62s**, 경고 오류 처리. 실제 Pi·모델 호출 없음.
- 기존 wheel과 sdist의 `twine check --strict` 통과.
- GitHub [검증 실행 45914544501](https://github.com/aldfula/EmbedSleuth/actions/runs/45914544501) 성공(`e04c680`, validate 13초). 기존 공개 Release 다운로드·해시·배포 wheel 설치·의존성·MCP/훅 검사와 산출물 전달이 통과했으며 `publish` 작업은 실행하지 않았다.
- GitHub `pypi` environment의 허용 배포 브랜치는 `main` 한 개임을 확인했다. PyPI 게시와 공개 인덱스 설치 검증은 계정 인증·Publisher 등록 후 진행한다.

근거: [PyPI Pending Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/), [Trusted Publisher 설정](https://docs.pypi.org/trusted-publishers/adding-a-publisher/), [공식 PyPI 게시 Action](https://github.com/pypa/gh-action-pypi-publish).
