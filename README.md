# MCP & Skill Inspector

Claude Code의 MCP 서버와 스킬 구성을 조사하고, 외부 통신 가능성과 실제 연결 상태를 보고서로 정리하는 도구입니다. Python 3.10 이상과 표준 라이브러리만으로 실행합니다.

기본 실행은 파일만 읽습니다. `--probe`를 지정하면 설정된 서버 프로세스를 실행하거나 HTTP 엔드포인트에 연결해 상태를 검사합니다.

## 주요 기능

- **구성 목록:** 사용자·프로젝트·로컬 MCP 설정, 스킬, 플러그인 캐시를 조사합니다.
- **설정 진단:** 잘못된 JSON, 누락된 환경 변수, 잘못된 서버 정의를 보고합니다.
- **우선순위 반영:** 로컬 > 프로젝트 > 사용자 설정을 구분하고, 비활성·가려진 서버는 연결 검사에서 제외합니다.
- **통신 분류:** `local`, `external`, `unknown`을 분류하고 판단 근거를 기록합니다. 스킬은 근거의 파일·행 번호도 제공합니다.
- **프로토콜 검사:** stdio와 Streamable HTTP의 초기화·도구 목록을 검증하고, 도구 페이지네이션과 HTTP JSON/SSE 응답을 처리합니다.
- **병렬 점검:** 동시 실행 수와 서버별 검사 시간을 제한하고, stderr를 함께 읽어 로그로 인한 교착을 방지합니다.
- **보고서 비교:** 이전 스냅샷과 비교해 서버·스킬의 추가, 삭제, 변경을 표시합니다.
- **자동화:** JSON/Markdown 보고서, 실패 종료 코드, GitHub Actions 테스트 구성을 제공합니다.

## 시작하기

```bash
git clone https://github.com/kos2001/skill-mcp-inspector.git
cd skill-mcp-inspector
python3 scripts/audit.py --out ./mcp-inspector-out
```

별도 패키지 설치는 필요하지 않습니다. 결과는 다음 두 파일에 저장됩니다.

| 파일 | 용도 |
|------|------|
| `mcp-inspector-out/report.md` | 사람이 읽는 요약, 서버 상태, 분류 근거, 진단 |
| `mcp-inspector-out/report.json` | 자동화에 사용할 구성 목록, 상태, 근거, 비교 결과 |

다른 프로젝트를 점검하려면 해당 프로젝트의 경로를 지정합니다.

```bash
python3 scripts/audit.py --scope project --project-root /path/to/project
```

### Claude Code 스킬로 설치

저장소를 내려받은 디렉터리에서 스킬 파일과 실행에 필요한 리소스를 복사합니다. 기존 설치가 있으면 같은 이름의 파일이 갱신됩니다.

```bash
mkdir -p ~/.claude/skills/mcp-skill-inspector
cp SKILL.md ~/.claude/skills/mcp-skill-inspector/
cp -R scripts references ~/.claude/skills/mcp-skill-inspector/
```

에이전트가 사용할 절차는 [SKILL.md](SKILL.md)에 있습니다. 테스트와 개발은 `tests/`가 포함된 저장소에서 실행합니다.

## 사용 예제

### 프로젝트 서버의 연결 상태 검사

```bash
python3 scripts/audit.py \
  --scope project \
  --project-root /path/to/project \
  --probe --workers 4 --probe-timeout 8
```

`--probe`는 설정된 명령과 URL을 실제로 사용합니다. 활성 설치로 확인되지 않은 플러그인 캐시와 가려진 정의는 실행하지 않습니다. 검사는 메타데이터와 도구 목록을 읽으며, 도구를 호출하지 않습니다.

### 특정 서버 또는 별도 설정 파일 검사

```bash
python3 scripts/audit.py --probe --server my-server
python3 scripts/audit.py --scope project --config /path/to/mcp.json --probe
```

`--server`와 `--config`는 여러 번 지정할 수 있습니다. 서버 이름이 없으면 인자 오류로 종료합니다.

### 최신 프로토콜의 서버 검사

기본 `legacy` 모드는 `initialize`와 `notifications/initialized`를 사용하며, 2024-11-05부터 2025-11-25까지의 지원 버전을 협상합니다. 2026-07-28 프로토콜의 서버는 `modern` 모드를 지정합니다.

```bash
python3 scripts/audit.py --probe --server my-server --protocol-era modern
```

`modern` 모드는 `server/discover`와 요청별 메타데이터를 사용합니다. 두 모드 간 자동 전환은 하지 않습니다.

### 이전 보고서와 비교

```bash
python3 scripts/audit.py --scope project --out ./mcp-inspector-out
cp ./mcp-inspector-out/report.json /tmp/mcp-inspector-baseline.json
python3 scripts/audit.py --scope project \
  --baseline /tmp/mcp-inspector-baseline.json --out ./mcp-inspector-out
```

같은 프로젝트 경로, 범위, 서버 필터로 생성한 보고서를 비교하세요. 타임스탬프와 지연 시간 변화는 제외하고, 양쪽 보고서에 상태 검사 결과가 있을 때만 상태 변화를 비교합니다. 자격증명 값은 보고서에서 생략하므로 값의 교체는 감지하지 않습니다.

### CI에서 실패 감지

```bash
python3 scripts/audit.py --scope project --probe --strict --fail-on-unhealthy
```

| 종료 코드 | 의미 |
|-----------|------|
| `0` | 요청한 실행과 검사가 성공 |
| `1` | `--strict`에서 진단 발견, 또는 `--fail-on-unhealthy`에서 실패·미검증 상태나 진단 발견 |
| `2` | 잘못된 인자 또는 비교 기준 보고서 |

`--fail-on-unhealthy`는 `--probe`가 필요합니다. 활성·유효 서버의 상태가 `ok`가 아니면 실패하며, 실행 파일 누락과 인증 필요 상태도 포함합니다. 코드 `1`로 종료하는 경우에도 보고서는 먼저 저장합니다.

## 조사 범위와 보고서 해석

| 범위 | 조사 대상 |
|------|-----------|
| `all` | 사용자와 현재 프로젝트 구성; 기본값 |
| `user` | 사용자 MCP 설정, Claude 스킬, 플러그인 캐시·마켓플레이스 |
| `project` | 해당 프로젝트의 로컬 MCP 항목, `.mcp.json`, `.claude/settings*.json`, `.claude/skills` |

사용자·로컬 MCP 항목은 `~/.claude.json`에서 읽으며, 기존 `~/.claude/settings*.json`의 서버 정의도 지원합니다. 사용자 설정의 비활성화 정책은 프로젝트 서버에도 적용될 수 있습니다. `enabled`는 구성상 활성 여부, `effective`는 사용자·프로젝트·로컬 정의 사이에서 우선 적용되는 항목을 나타냅니다. 런타임 승인이나 연결 성공을 뜻하지 않습니다.

### 통신 분류

| 분류 | 의미 |
|------|------|
| `external` | 루프백 외부 URL, 알려진 외부 서비스, 스킬의 HTTP·웹 도구 사용 등 정적 근거 발견 |
| `local` | 로컬 기능·루프백 엔드포인트 근거, 또는 완성된 스킬 스캔에서 외부 통신 표시를 발견하지 못함 |
| `unknown` | 신뢰할 만한 근거 부족, 잘못된 설정, 불완전한 스캔 |

분류는 소스와 설정을 통한 추론이며 실제 트래픽 측정이 아닙니다. `local`도 외부 통신이 불가능하다는 보장은 아닙니다. 자세한 규칙은 [분류·상태 규칙](references/classification-rules.md)을 참고하세요.

### 연결 검사 상태

| 상태 | 의미 |
|------|------|
| `ok` | 유효한 초기화·발견 응답과, 서버가 제공한다고 선언한 도구의 전체 목록 검증 |
| `partial` | 초기화·발견 성공 후 도구 목록 검사 실패 |
| `auth_required` / `forbidden` | HTTP 401 / 403; 연결 검증 완료로 처리하지 않음 |
| `reachable` | 구형 HTTP+SSE 엔드포인트에 도달했지만 MCP 초기화는 미검증 |
| `config_error` | 잘못된 정의 또는 필수 환경 변수 누락; 실행하지 않음 |
| `timeout` / `exited` / `unreachable` / `error` | 시간 초과, 프로세스 종료, 연결 실패, 프로토콜·실행 오류 |
| `disabled` / `skipped` | 비활성, 캐시, 가려진 정의, 실행 파일 누락, 지원하지 않는 검사 대상 |

HTTP 응답이 있다는 이유만으로 정상 판정하지 않습니다. 리소스·프롬프트만 제공하는 서버도 유효한 초기화·발견에 성공하면 정상입니다. 세부 오류와 마스킹된 stderr는 JSON의 `health`에 기록됩니다.

## CLI 옵션

| 옵션 | 기본값 또는 역할 |
|------|------------------|
| `--out DIR` | `./mcp-inspector-out` |
| `--scope all/user/project` | `all` |
| `--project-root PATH` | 현재 작업 디렉터리 |
| `--claude-home PATH` | `CLAUDE_HOME` 또는 `~/.claude` |
| `--claude-config PATH` | Claude 홈과 같은 상위 디렉터리의 `.claude.json` |
| `--config PATH` | 추가 MCP JSON 파일; 반복 가능 |
| `--server NAME` | 정확한 서버 이름 필터; 반복 가능 |
| `--probe` | 실제 상태 검사 활성화 |
| `--protocol-era legacy/modern` | `legacy` |
| `--probe-timeout SECONDS` | 서버별 프로토콜 검사 예산; 기본 8초, 유한한 양수 |
| `--workers N` | 병렬 검사 수; 기본 4, 범위 1–32; 1은 순차 실행 |
| `--baseline PATH` | 비교할 이전 `report.json` |
| `--strict` | 스캔·설정 진단이 있으면 실패 |
| `--fail-on-unhealthy` | 활성·유효 서버의 상태가 `ok`가 아니거나 진단이 있으면 실패 |

전체 도움말: `python3 scripts/audit.py --help`

## 테스트와 성능

```bash
python3 -m unittest discover -s tests -v
python3 scripts/benchmark.py
```

테스트는 임시 설정과 로컬 모의 stdio/HTTP 서버를 사용합니다. 로컬 Python 3.10.20·3.14.7에서 각각 **26개 테스트가 통과**했습니다. [GitHub Actions](.github/workflows/test.yml)는 Linux·macOS와 Python 3.10·3.14 조합을 검사하도록 구성했습니다. 이번 반영 시점에는 저장소에서 Actions가 비활성화되어 있어 원격 CI 결과는 없습니다.

모의 stdio 서버 8개, 서버별 초기화 지연 150ms, 3회 반복한 로컬 측정 결과입니다.

| 실행 방식 | 실행 시간 중앙값 |
|-----------|------------------|
| 순차, 1개 워커 | 1,573.7ms |
| 병렬, 4개 워커 | 392.6ms |

측정에서는 **4.01배 속도 향상**, 약 **75% 시간 감소**를 확인했습니다. 실제 서버의 지연과 부하에 따라 결과는 달라집니다. 측정 조건과 공식 GitHub·MCP 자료는 [개선 근거와 검증 기록](references/improvements.md)에 정리했습니다.

## 지원 한계

- stdio 검사는 macOS·Linux에서 지원합니다. 구형 HTTP+SSE는 도달 가능 여부만 확인하며, WebSocket·SDK 서버는 목록에 포함하되 검사를 건너뜁니다.
- 구성·캐시 목록은 모든 런타임 연결을 재현하지 않습니다. 관리형 정책, 인라인 플러그인 매니페스트의 MCP 정의, 동적 인증 헤더, OAuth 로그인, 승인 상태와 플러그인 엔드포인트 중복 제거는 처리하지 않습니다.
- 검사는 일회성입니다. DNS 조회·프로세스 생성과 짧은 정리 시간은 지정된 프로토콜 예산을 초과할 수 있고, 끊어진 SSE 스트림을 재개하지 않습니다.
- 의존성·빌드 디렉터리와 중첩 디렉터리 심볼릭 링크는 스캔하지 않습니다. 스킬·스크립트 파일은 2MiB, 프로토콜 응답은 1MiB, 도구 목록은 100페이지까지 처리합니다.
- 보고서는 실행 환경·헤더 값을 생략하고 인식 가능한 자격증명을 마스킹합니다. 임의의 서버 메시지에 포함된 모든 비밀을 찾아내는 보장은 없으므로 공유 전에 내용을 확인하세요.

## 파일 구성

```text
SKILL.md                         에이전트용 실행 지침
scripts/audit.py                 설정·스킬 조사, 분류, 보고서와 CLI
scripts/probes.py                stdio·HTTP 프로토콜 검사
scripts/benchmark.py             로컬 성능 측정
tests/                          회귀·통합 테스트와 모의 서버
references/classification-rules.md  분류·상태 규칙
references/improvements.md       조사 출처와 검증 결과
.github/workflows/test.yml       CI 테스트 구성
```

## 라이선스

[MIT License](LICENSE)
