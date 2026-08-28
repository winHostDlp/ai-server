# Sentry AI 서버 테스트 시나리오

작성 역할: AI Model 담당
목적: Host Agent 담당 통합 테스트 시, AI 서버(`/api/v1/analyze`) 단독 검증용 입력값 및 기대 결과 제공

**현재 기준 (2026-08-29)**

- AI 모델: `koelectra-dlp-v7`
- 판정 기준: `block >= 0.41`, `0.20 <= review < 0.41`, `allow < 0.20`
- 통합 검토안: `review`는 경고 후 허용하고 `WARNED`로 기록하며, AI 장애나 의미적으로 잘못된 응답은 Fail-Open으로 허용하고 실패 사유를 남긴다. 팀 승인 전까지 확정 정책으로 간주하지 않는다.

착수보고서 1.3절의 반출 시나리오를 기준으로, 각 채널별 block / review / allow
후보 입력을 준비했습니다. 아래 판정 표는 **AI 서버 단독 검증용**입니다. Host
Agent의 실제 채널은 정규식 게이트를 먼저 통과해야 하므로, 표의 문맥 전용 문장을
그대로 사용해서는 Windows E2E가 시작되지 않을 수 있습니다.

> 대시보드(`/api/v1/logs`) 로그 연동 및 표시 검증은 아래 **웹 대시보드 연동 체크리스트 (Web 담당)** 절을 참고합니다.

---

## 실제 시연 토폴로지와 정규식 게이트

현재 발표용 기본 경로는 다음과 같습니다.

```text
Windows Host Agent
  ├─ POST {AI_BASE_URL}/api/v1/analyze ──> 실제 koelectra-dlp-v7 서버
  └─ POST {DASHBOARD_URL}/api/v1/logs ──> Web 서버·SQLite·대시보드
```

Host Agent가 AI 서버와 Web 서버에 각각 직접 요청합니다. Web의
`POST /api/v1/analyze` 중계 기능은 별도 테스트 경로이며, 이 Windows E2E의
기본 토폴로지로 보지 않습니다. AI 요청과 대시보드 저장 요청은 Host Agent가
생성한 동일한 `event_id`로 연결합니다.

### Host 정규식 게이트

실제 Clipboard, USB, Outlook, 웹메일, 클라우드 드라이브 채널은 다음 공통 흐름을
사용합니다.

```text
실제 사용자 동작
  → Host 정규식 1차 검사
  → hit가 있을 때만 PayloadBuilder가 snippet 생성
  → 실제 AI v7 판정
  → 운영체제·애플리케이션 조치
  → 같은 event_id로 대시보드 기록
```

따라서 라이브 입력은 AI 단독 예문에 테스트용 가상 패턴을 억지로 붙이는 방식이
아니라, 실제 발표 파일·클립보드 원문을 Host의 `RuleFilter -> PayloadBuilder`
순서로 먼저 실행해 생성된 정확한 `snippet`으로 사전 보정해야 합니다. 정규식
hit가 없으면 AI 미호출이 정상 동작입니다.

- block 후보: 가상 주민등록번호·계좌번호 등 구성된 테스트 패턴과 기밀 문맥을
  함께 사용합니다.
- allow 후보: 테스트 전용 `>> 확인완료` 패턴을 사용할 수 있지만, 운영 배포에는
  포함하지 않습니다.
- 실제 개인정보, 실제 계정 비밀, 운영 API 키는 사용하지 않습니다.
- 라이브 v7 결과가 예상과 다르면 임계값이나 모델 코드를 즉석에서 바꾸지 않고
  입력·점수·모델 버전을 기록한 뒤 AI Model 담당과 다시 보정합니다.

## Windows 다중 시나리오 범위

| 시연 | Host AI 요청 채널 | Web `leak_channel` | 실제 실행 요구사항 | 상태 표현 |
|---|---|---|---|---|
| USB 파일 복사 | `file_guard` | `USB_COPY` | Windows 이동식 드라이브·파일 감시 | 실제 장비 검증 전 미완료 |
| 외부 앱에 붙여넣기 | `clipboard` | `CLIPBOARD` | Windows 클립보드/키보드 훅·실제 `Ctrl+V` | 실제 장비 검증 전 미완료 |
| Outlook 외부 메일 | `outlook` | `EMAIL_ATTACHMENT` | Outlook 로그인·COM `ItemSend` | 실제 장비 검증 전 미완료 |
| 웹메일 본문·첨부 | `web_mail` | `WEB_UPLOAD` | HTTPS 프록시·테스트 브라우저/계정 | 실제 장비 검증 전 미완료 |
| 클라우드 업로드 | `drive_upload` | `CLOUD_DRIVE` | HTTPS 프록시·테스트 브라우저/계정 | 실제 장비 검증 전 미완료 |

`MESSENGER` 전용 전송 훅은 현재 없습니다. Teams·Slack 등에서의 실제 시연은
붙여넣기 훅을 거치는 `clipboard -> CLIPBOARD`로만 기록하며 `MESSENGER` 구현
완료로 표현하지 않습니다. `PRINT`도 Web 로그 스키마와 샘플 데이터만 있고
Host 인쇄 훅이 없으므로 실제 시연 지원 대상이 아닙니다.

---

## 시나리오 1: 웹메일/클라우드 첨부를 통한 문서 유출 시도

**AI 단독 채널**: `web_mail` / `drive_upload` (착수보고서 시나리오 1 대응)

| # | 입력 텍스트 | 채널 | 기대 decision | 비고 |
|---|---|---|---|---|
| 1-A | "A전자向 반도체 부품 납품 단가는 개당 12,400원으로 최종 협상되었습니다." | web_mail | **block** | 미공개 단가 정보, 정규식 hit 없는 AI 단독 예문 |
| 1-B | "정보보호 인증(ISMS) 심사 관련 부서별 자료 제출을 요청드립니다." | drive_upload | **review** (또는 block, 경계값) | 임계값 근처 애매 케이스 |
| 1-C | "이번 신제품 카탈로그를 첨부하니 확인 부탁드립니다." | web_mail | **allow** | 일반 공개 자료 |

## 시나리오 2: 변형된 민감정보의 클립보드 복사

**채널**: `clipboard` (착수보고서 시나리오 2 대응)

| # | 입력 텍스트 | 채널 | 기대 decision | 비고 |
|---|---|---|---|---|
| 2-A | "이OO 부장 외 4명에게 1인당 5천만 원 상당의 특별 스톡옵션을 배정하기로 결정했습니다." | clipboard | **block** | 인사 기밀, 문맥 기반 판별 |
| 2-B | "3분기 수주 잔고는 총 2조 1천억 원이며 B건설 프로젝트가 40%를 차지합니다." | clipboard | **block** | 영업 기밀, 수치 기반 |
| 2-C | "이번 주 금요일 오후 2시에 신규 입사자 환영회가 예정되어 있습니다." | clipboard | **allow** | 완전 무해한 공지문 |

## 시나리오 3: USB 등 이동식 매체로의 파일 복사

**AI 단독 채널**: `file_guard` (착수보고서 시나리오 3 대응)

| # | 입력 텍스트 | 채널 | 기대 decision | 비고 |
|---|---|---|---|---|
| 3-A | "차세대 배터리 소재 배합비는 리튬 62%, 실리콘 23%, 첨가제 15%입니다." | file_guard | **block** | R&D 핵심 기술, 정규식 hit 없는 AI 단독 예문 |
| 3-B | "정보보안팀 주관으로 전 직원 대상 보안 교육이 진행됩니다." | file_guard | **allow** | '보안' 단어 포함되지만 비기밀 (키워드 함정 케이스) |

## 시나리오 4: Outlook 외부 메일 전송

**AI 단독 채널**: `outlook`

| # | 입력 텍스트 | 채널 | 기대 decision | 비고 |
|---|---|---|---|---|
| 4-A | "인수 검토 대상 회사의 비공개 제안 가격과 임직원 보상안을 첨부합니다." | outlook | **block** | 실제 E2E에서는 가상 정규식 패턴이 포함된 첨부 사용 |
| 4-B | "공개된 제품 소개서와 행사 일정을 전달드립니다." | outlook | **allow** | AI 단독 예문; Host에서는 정규식 hit 없으면 AI 미호출 |

---

## AI 서버 단독 체크리스트

- [ ] AI 서버에 위 후보 텍스트를 직접 `POST /api/v1/analyze`로 전송한다
- [ ] AI 서버가 위 표의 기대 `decision`과 일치하는 응답을 반환한다
- [ ] `confidence_score`, `model_version`, `latency_ms` 필드가 정상적으로 채워져 있다

> 이 체크리스트는 모델 API 단독 검증입니다. Host 정규식 게이트, 실제 Windows
> 훅, 대시보드 전송은 아래 라이브 E2E 체크리스트에서 별도로 확인합니다.

## 라이브 v7 E2E 증거 기준

다음 조건이 모두 같은 실행에서 확인되어야 실제 AI 기반 시연으로 인정합니다.
Mock 결과나 샘플 로그 전송만으로는 완료 처리하지 않습니다.

Host의 `demo_preflight.py`와 R009 안전·준비 회귀 테스트는 설정·전제 조건과
플랫폼 중립 조치 계약을 점검하는 검증입니다. 사전 점검이 통과해도 라이브 v7
호출과 E2E가 증명되지 않으며, 아래처럼 실제 채널 이벤트의 동일한 `event_id`와
운영체제·애플리케이션 조치를 확인해야 완료됩니다.

- [ ] Host 사전 점검에서 `SentryUserAgent State=Running`, WebProxy TCP 리스너,
  Outbound UDP/443 QUIC 차단 규칙과 5개 시나리오가 모두 `PASS`이다
- [ ] AI 서버 루트 헬스 응답에서 `status=running`을 확인한다
- [ ] 시연 직전 Host의 실제 `RuleFilter -> PayloadBuilder` 결과로 v7 판정을
  사전 확인한다
- [ ] AI 응답의 `model_version=koelectra-dlp-v7`을 확인한다
- [ ] Host 로컬 이벤트와 대시보드 상세의 `detection_type=HYBRID`를 확인한다
- [ ] AI 요청, Host 로컬 이벤트, Web 저장 결과의 `event_id`가 정확히 일치한다
- [ ] Web 상세의 `ai_score`, `action_taken`, `matched_keywords`, `latency_ms`,
  `model_version`이 같은 이벤트의 Host/AI 값과 일치한다
- [ ] 운영체제·애플리케이션 조치와 대시보드 상태를 함께 기록한다

## 저장소 구현 기준

2026-08-29에 확인한 각 저장소 `origin/main`의 출발점은 다음과 같습니다.

| 저장소 | `origin/main` | 원격 코드 기준 상태 |
|---|---|---|
| `ai-server` | `5d536b4` | v7 API, `0.41` / `0.20` 임계값, 요청 로그 구현. 실제 v7 모델 파일과 실행 평가 결과는 원격에 없어 라이브 재검증 필요 |
| `host-agent` | `e1c8ca1` | 최신 AI 요청·응답 스키마, `/api/v1/logs`, `X-Agent-Token` 지원. 신규 채널·`model_version`·정책 통일은 이번 통합 작업 대상 |
| `web` | `fffe8b4` | 신규 채널·`model_version` 저장/표시와 준실시간 고위험 알림 구현. 실제 Host·v7 E2E는 별도 검증 필요 |

> 아래 **코드 계약 구현 및 자동 검증**은 위 원격 출발점에 현재 통합 작업본과 자동 테스트 결과를 더한 상태입니다. 실제 Windows·AI·대시보드 연동 검증과는 구분합니다.

## 웹 대시보드 연동 체크리스트 (Web 담당)

### 판정 및 채널 변환 기준

통합 완료 시 Host Agent는 AI 서버의 요청·응답 값을 다음과 같이 대시보드 로그 필드로
변환해 `POST /api/v1/logs`에 전송해야 합니다.

| 연동 원본 값 | 대시보드 로그 값 |
|---|---|
| AI 요청 `event_id` | `event_id` |
| `decision=block` + Host 실제 차단 성공 | `action_taken=BLOCKED` |
| USB 삭제 직전·도중 파일 없음, 파일 지문 변경, 삭제 실패·매체 이탈 | `action_taken=WARNED` + 실패 사유 |
| WebProxy HTTP 451 응답 생성 실패 | `action_taken=WARNED` + 실패 사유 |
| `decision=review` | `action_taken=WARNED` |
| `decision=allow` + 실제 허용 조치 성공 | `action_taken=ALLOWED` |
| Clipboard 대상 변경·재발행 실패 | `action_taken=WARNED` + 실패 사유 |
| `confidence_score` | `ai_score` |
| AI 요청 `matched_patterns`의 패턴 ID | `matched_keywords` |
| AI 응답 `reason` | `evidence_summary` |
| Host Agent의 최종 조치 사유 | `decision_reason` |
| AI 응답 `latency_ms` | `latency_ms` |
| AI 응답 `model_version` | `model_version` (저장 및 상세 화면 표시) |

현재 검토안에 구현된 대시보드 API 채널 변환은 다음과 같습니다.

| AI 요청 `channel` | 대시보드 `leak_channel` |
|---|---|
| `http` | `WEB_UPLOAD` |
| `web_mail` | `WEB_UPLOAD` |
| `outlook` | `EMAIL_ATTACHMENT` |
| `smtp` | `EMAIL_ATTACHMENT` |
| `usb` | `USB_COPY` |
| `file_guard` | `USB_COPY` |
| `clipboard` | `CLIPBOARD` |
| `drive_upload` | `CLOUD_DRIVE` |

### 현재 검토안에 구현된 처리 정책

| 상황 | Host Agent 최종 조치 | 대시보드 기록 |
|---|---|---|
| `decision=block` + 실제 조치 성공 | 차단 | `BLOCKED` |
| USB 삭제 직전·도중 파일 없음, 파일 지문 변경, 삭제 실패·매체 이탈 | 경고, 실제 차단 미확인 | `WARNED` + 실패 사유 |
| WebProxy HTTP 451 응답 생성 실패 | 경고, 실제 차단 미확인 | `WARNED` + 실패 사유 |
| `decision=review` | 사용자에게 경고하고 전송/붙여넣기 허용 | `WARNED` |
| `decision=allow` + 실제 허용 조치 성공 | 허용 | `ALLOWED` |
| Clipboard 대상 변경·재발행 실패 | 붙여넣기 미수행 | `WARNED` + 실패 사유 |
| AI 타임아웃·통신 오류·5xx·잘못된 응답 + 실제 허용 조치 성공 | Fail-Open으로 허용 | `ALLOWED` + `decision_reason`에 실패 사유 |
| 위 Fail-Open 뒤 Clipboard 대상 변경·재발행 실패 | 붙여넣기 미수행 | `WARNED` + AI 실패·재발행 실패 사유 |

> 아래의 **구현 확인**은 코드에 반영된 계약만 표시합니다. **실행 검증**은 세 저장소를 동시에 실행해야 하므로 성공할 때까지 체크하지 않습니다.

### 코드 계약 구현 및 자동 검증

- [x] Host Agent의 AI 요청 필드를 `event_id`, `channel`, `user_id`, `matched_patterns`, `snippet`, `metadata` 형식에 맞춘다
- [x] Host Agent의 AI 응답 파서를 `decision`, `confidence_score`, `model_version`, `latency_ms`, `reason` 형식에 맞춘다
- [x] 대시보드 전송 경로를 `/api/v1/events`가 아닌 `POST /api/v1/logs`로 맞춘다
- [x] Host Agent가 `clipboard` / `drive_upload`를 `CLIPBOARD` / `CLOUD_DRIVE`로 변환해 전송한다
- [x] Host Agent가 `model_version`을 대시보드 로그 페이로드에 포함한다
- [x] 대시보드가 `CLIPBOARD`, `CLOUD_DRIVE`를 허용하고 `model_version`을 저장·조회·표시한다 (Web R009 테스트 45건 통과)
- [x] Web R009가 로그를 생성하지 않는 보호 API에서 `X-Agent-Token` 일치를 확인하고, Host 사전 점검이 이 결과를 5개 시나리오 준비 조건으로 사용한다
- [x] Web 분석 중계가 Host Agent의 7개 채널(`clipboard`, `outlook`, `smtp`, `http`, `web_mail`, `drive_upload`, `file_guard`)을 수락하고 `reason`, `evidence_summary`를 반환한다
- [x] Web 분석 중계가 upstream `event_id` 불일치와 `model_version` 누락을 HTTP 502로 거절한다
- [x] Host Agent가 정상 `allow`의 실제 허용 조치가 성공하면 `ALLOWED`로 기록하고, Clipboard 재발행 실패는 `WARNED`와 실패 사유로 기록한다
- [x] Host Agent가 모든 `review` 채널에서 사용자에게 경고한 후 허용하고 `WARNED`로 기록한다
- [x] Host Agent가 AI 타임아웃·통신 오류·5xx와 `decision` / `confidence_score` 누락·형식·범위 오류를 Fail-Open으로 처리하고 `ALLOWED`와 실패 사유를 기록한다 (Host Agent 기존 통합 계약 테스트 81건 통과)
- [x] Clipboard의 플랫폼 중립 worker 계약에서 `CF_HDROP` 파일 페이로드에 필요한 형식 분기, 복원·재붙여넣기 호출 순서, 예외 시 release 호출을 검증한다 (실제 Win32 훅 호출 검증 아님)
- [x] Outlook `ItemSend` 콜백이 판정별 `Cancel` 값을 반환하는 단위 계약을 검증한다
- [x] 대시보드 flush가 HTTP 성공 응답을 받은 건수만 성공으로 집계한다
- [x] `EventLogger`·`Notifier` 실패를 조치 로직과 격리해 `block` / `review` / `allow`를 바꾸지 않고, Web 차단 HTTP 451·Clipboard 차단·USB·SMTP·HTTP·Outlook의 판정별 조치를 보존한다
- [x] 교차 저장소 코드 계약 검증으로 7개 채널의 analyze 페이로드 7건과 로그 페이로드 2건을 Host→Web 코드에 직접 적용해 통과시킨다
- [ ] 두 컴퓨터 통합 테스트 시 Host의 대시보드 주소를
  `http://<WEB_LAN_IP>:8000` 또는 팀에서 정한 서버 주소로 설정한다
- [x] Host Agent가 로그 전송 시 `X-Agent-Token`을 포함할 수 있고 대시보드 로그 요청 스키마를 사용한다

> 교차 저장소 9건은 과거 통합 검토에서 페이로드를 함수·API 코드에 직접 적용한 **수동 계약 검증**입니다. 현재 저장소에서 다시 실행할 수 있는 자동 테스트 파일은 아니며, 실제 서비스 프로세스·네트워크·Windows 채널을 연결한 E2E 테스트도 아닙니다.

> Host 전체 자동 테스트 118건은 **기존 통합 계약 81건 + R009 안전·준비 회귀 37건**입니다. 추가된 37건은 machine-local 드라이브 baseline, 실행 중인 예약 작업·프록시·QUIC 사전 점검, Outlook 원문·Exchange 주소 전달, Clipboard 목적지·조치 결과, WebProxy 파일명·HTTP 차단 결과 분기, USB 안정화·파일 지문·삭제 결과, 대시보드 전송 재시도·수동 재전송 같은 플랫폼 중립 계약을 검증하며 실제 Windows 채널·라이브 v7·Web E2E를 의미하지 않습니다.

### 로그 수집 API 검증

- [ ] AI 분석 요청과 로그 저장 요청에 동일한 `event_id`가 사용된다
- [ ] Host Agent가 `X-Agent-Token` 헤더를 포함해 `POST /api/v1/logs`를 호출한다
- [ ] 최초 전송 시 HTTP 201과 `duplicate=false`가 반환된다
- [ ] `ai_score`, `action_taken`, `leak_channel`, `matched_keywords`, `evidence_summary`, `latency_ms`, `model_version`이 Agent 전송값과 동일하게 저장된다
- [ ] 동일한 `event_id`를 재전송하면 HTTP 200과 `duplicate=true`가 반환되고 로그가 중복 생성되지 않는다

### 로그 및 대시보드 화면 검증

- [ ] `GET /api/v1/logs` 목록에서 신규 이벤트를 `event_id`로 확인할 수 있다
- [ ] `GET /api/v1/logs/{log_id}` 상세 응답에 사용자·Host·파일·프로세스·정책·탐지 근거·`model_version`이 포함된다
- [ ] `/logs` 화면에서 `BLOCKED` / `WARNED` / `ALLOWED` 상태, AI 점수, 모델 버전, 매칭 키워드와 판단 사유가 정상 표시된다
- [ ] 부서, 사용자, Agent, 조치 결과, 반출 채널, 날짜 및 검색어 필터가 신규 이벤트에 적용된다
- [ ] `GET /api/v1/dashboard/summary?days=7`의 KPI와 타임라인에 신규 이벤트가 1건 반영된다
- [ ] 차단 이벤트는 채널 분포·부서 통계·고위험 이벤트 목록에 반영된다

### 예외 상황 검증

- [ ] `X-Agent-Token`이 없거나 올바르지 않으면 로그 저장 요청이 HTTP 401로 거절된다
- [ ] `ai_score` 범위 또는 `action_taken`, `leak_channel` 값이 유효하지 않으면 HTTP 422가 반환된다
- [ ] 존재하지 않는 `log_id` 상세 조회는 HTTP 404를 반환한다
- [ ] AI 서버 장애로 Host Agent가 Fail-Open 처리한 이벤트도 `ALLOWED`와 장애 사유를 포함해 로그로 남는지 확인한다

### Windows 실환경 채널 검증

> 아래 항목은 단위 계약 테스트와 달리 Windows API·권한·Office COM이 필요한 실행 검증입니다.

- [ ] Clipboard 테스트 원문이 정규식 게이트를 통과하고, 실제 `Ctrl+V`에서
  `clipboard -> CLIPBOARD` 이벤트가 생성되는지 확인한다
- [ ] Windows Explorer에서 복수 파일 `CF_HDROP` 붙여넣기를 `allow` / `review`했을 때 원본 파일 목록과 형식이 보존되는지 확인한다
- [ ] 일반 프로세스와 관리자 권한 프로세스에서 `SendInput` 재붙여넣기를 확인하고 UIPI 제약과 실패 시 동작을 기록한다
- [ ] 실제 Outlook COM `ItemSend`에서 `block`은 `Cancel=True`, `review` / `allow`는 `Cancel=False`로 적용되는지 확인한다
- [ ] 실제 USB 복사에서 `file_guard -> USB_COPY` 이벤트가 생성되고, `block`일
  때 원본이 아닌 USB 대상 파일만 삭제되는지 확인한다
- [ ] 테스트 브라우저의 웹메일 전송에서 `web_mail -> WEB_UPLOAD`와 차단 응답을
  확인한다
- [ ] 테스트 브라우저의 클라우드 업로드에서
  `drive_upload -> CLOUD_DRIVE`와 실제 업로드 차단을 확인한다
- [ ] Teams·Slack 붙여넣기 시연을 `CLIPBOARD`로 기록하고 `MESSENGER` 전용
  훅으로 설명하지 않는지 확인한다
- [ ] `PRINT`를 실제 Host 차단 지원 채널로 시연하지 않는지 확인한다
- [ ] AI 서버·Windows Host Agent·대시보드를 동시 실행해 전체 이벤트 흐름을 E2E로 확인한다

## 장애 상황 테스트 (Fail-Open 정책 확인용)

> 타임아웃·통신 오류·AI 5xx·의미적으로 잘못된 응답을 각각 재현해, 사용자 작업은 허용되고 동일한 `event_id`의 장애 로그가 남는지 확인합니다.

- [ ] AI 서버가 응답하지 않을 때(타임아웃) Host Agent가 기본 허용(Fail-Open)으로 동작하는지 확인
- [ ] AI 서버가 500 에러를 반환할 때도 Host Agent가 차단 없이 통과시키는지 확인
- [ ] `decision` / `confidence_score`가 누락되거나 알 수 없는 값·비수치·`NaN`·범위 밖일 때 차단 없이 통과시키는지 확인
- [ ] 위 장애 이벤트가 `action_taken=ALLOWED`와 구체적인 `decision_reason`을 포함해 대시보드에 저장되는지 확인

---

## TODO 및 담당

- [x] 대시보드(`/api/v1/logs`) 로그 기록 및 상세 화면 표시 체크리스트 작성 — Web 담당
- [x] `clipboard -> CLIPBOARD`, `drive_upload -> CLOUD_DRIVE` 채널 계약을 검토안에 반영 — Host Agent 담당·Web 담당
- [x] `decision=review`를 경고 후 허용하고 `WARNED`로 기록하는 정책을 검토안에 반영 — 전체 담당
- [x] AI 장애 시 Fail-Open으로 허용하고 실패 사유를 기록하는 정책을 검토안에 반영 — 전체 담당
- [x] AI `model_version`을 대시보드에 저장·표시하는 계약을 검토안에 반영 — AI Model 담당·Web 담당
- [x] Host Agent에 채널 변환·`model_version` 전송·판정과 실제 조치 결과 로깅·경고·엄격한 Fail-Open·worker/Outlook/USB/WebProxy 단위 계약·실시간 전송 재시도·수동 flush를 반영하고, 기존 통합 계약 81건과 R009 안전·준비 회귀 37건으로 Host 전체 118건을 통과시킨다 — Host Agent 담당
- [x] 대시보드에 신규 채널·`model_version` 저장/조회/표시·7채널 분석 중계·보호된 Agent 인증 점검·upstream 응답 검증·다중 시나리오 자동 갱신을 반영하고 Web R009 테스트 45건을 통과시킨다 — Web 담당
- [ ] 실제 `koelectra-dlp-v7` 서버 URL과 모델을 준비하고 예상 판정을 확인한다 — AI Model 담당
- [ ] AI 서버·Host Agent·대시보드를 동시 실행하여 위 실행 검증 체크리스트를 완료한다 — 전체

> 참고: v7은 `data/train_dataset_v6.csv`(223건, 구어체 40건 보강)로 재학습했습니다. 위 텍스트는
> `data/train_dataset_v5.csv`와 `data/hard_test_set_v1.csv`의 대표 케이스입니다. 실제 `confidence_score`는
> v7 실행 환경에서 재확인하고, 예상 `decision`과 다르면 입력·임계값·모델 버전을 함께 기록합니다.
