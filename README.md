# Sentry AI 분석 서버 — API 문서

담당 역할: AI Model 담당
최종 갱신: 2026-08-29
모델 버전: `koelectra-dlp-v7`

## 1. 엔드포인트

```
POST {BASE_URL}/api/v1/analyze
Content-Type: application/json
```

주의: `{BASE_URL}`은 Colab 세션마다 바뀝니다. (Cloudflare Tunnel 무료 플랜은 고정 도메인 미지원)

## 2. Request

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `event_id` | string | O | Host Agent가 발급하는 이벤트 고유 ID |
| `channel` | string | O | `clipboard` / `outlook` / `http` / `usb` 등 반출 채널 |
| `user_id` | string | O | 사용자/디바이스 식별자 |
| `matched_patterns` | string[] | X | 1차 Rule 필터에서 매칭된 패턴 ID (없으면 빈 배열) |
| `snippet` | string | O | 분석할 텍스트 (PII는 사전에 마스킹 권장) |
| `metadata` | object | X | `app`, `dest`, `severity_hint` 포함 가능 |

### Request 예시
```json
{
  "event_id": "evt-20260813-00123",
  "channel": "clipboard",
  "user_id": "device-12345",
  "matched_patterns": ["rrn"],
  "snippet": "[가상 테스트] 900101-1234567 대상 임직원에게 비공개 특별 스톡옵션을 배정합니다.",
  "metadata": {
    "app": "chrome.exe",
    "dest": "external",
    "severity_hint": "high"
  }
}
```

## 3. Response

| 필드 | 타입 | 설명 |
|---|---|---|
| `event_id` | string | Request에서 받은 값 그대로 반환 |
| `decision` | string | `allow` / `review` / `block` |
| `confidence_score` | float | 0.0 ~ 1.0, 기밀일 확률 |
| `model_version` | string | 현재: `koelectra-dlp-v7` |
| `latency_ms` | float | AI 서버 내부 추론 소요 시간(ms) |
| `reason` | string | 확장 필드 (공식 스키마엔 없음), 판정 사유 요약 |

### Response 예시
```json
{
  "event_id": "evt-20260813-00123",
  "decision": "block",
  "confidence_score": 0.5584,
  "model_version": "koelectra-dlp-v7",
  "latency_ms": 49.72,
  "reason": "AI 문맥 분석 결과 기밀 가능성이 높아 차단 조치"
}
```

## 4. decision 판정 기준 (threshold)

| confidence_score | decision |
|---|---|
| >= 0.41 | block |
| 0.20 이상 0.41 미만 | review |
| < 0.20 | allow |

v7(구어체 보강 재학습) 기준 ROC/PR curve 재검증 결과. 격식체 하드케이스 50건 + 신규 구어체 하드케이스 34건 + 정식 Test셋으로 검증. 모델 재학습 시 threshold도 재산정 필요.

## 5. 에러 응답

| status code | 상황 |
|---|---|
| 400 | `snippet`이 비어있음 |
| 503 | AI 모델이 로드되지 않은 상태 (서버 기동 직후 등) |
| 500 | 추론 중 내부 에러 (`detail` 필드 참고) |

Host Agent는 착수보고서 3.5절 Fail-Open 정책(AI 서버 무응답/에러 시 기본 허용)을 따르므로, 위 에러 상황에서도 서비스가 멈추지 않도록 타임아웃/재시도 로직 권장.

## 6. curl 테스트 예시

```bash
curl -X POST {BASE_URL}/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "event_id": "test-001",
    "channel": "clipboard",
    "user_id": "test-device",
    "matched_patterns": [],
    "snippet": "이번 주 금요일 오후 2시에 신규 입사자 환영회가 예정되어 있습니다.",
    "metadata": {"app": "test", "dest": "external", "severity_hint": "low"}
  }'
```

Swagger UI: `{BASE_URL}/docs`

## 7. 현재 발표 통합 토폴로지

실제 Windows 시연에서는 Host Agent가 AI 판정과 대시보드 기록을 서로 다른
서버로 직접 전송합니다.

```text
Windows Host Agent
  ├─ POST {AI_BASE_URL}/api/v1/analyze ──> Colab의 koelectra-dlp-v7
  └─ POST {DASHBOARD_URL}/api/v1/logs ──> Web 서버·SQLite·대시보드
```

- `AI_BASE_URL`은 AI Model 담당이 실행한 Colab/Cloudflare 주소입니다.
- `DASHBOARD_URL`은 Web 담당 컴퓨터에서 LAN으로 연 Web 서버 주소입니다.
- Web의 `/api/v1/analyze` 중계 기능은 별도 테스트 경로이며, 위 실제 Host 시연의
  기본 경로로 간주하지 않습니다.
- AI 서버는 판정만 반환합니다. 대시보드 로그 생성과 전송은 Host Agent가 같은
  `event_id`를 유지해 수행합니다.

### Host 정규식 게이트

Host Agent의 실제 채널은 원문에서 설정된 정규식 패턴이 한 건 이상 탐지된
경우에만 AI 서버를 호출합니다. 따라서 `matched_patterns: []`로 직접 호출하는
`curl` 테스트가 성공하더라도 실제 Windows 채널 E2E가 검증된 것은 아닙니다.

발표용 입력은 반드시 다음 순서로 미리 확인합니다.

1. Host 정규식 필터가 테스트용 가상 개인정보·키 패턴을 탐지한다.
2. Host의 `PayloadBuilder`가 만든 실제 `snippet`으로 라이브 v7을 호출한다.
3. 기대한 `decision`이 재현될 때만 해당 입력을 실제 채널 시연에 사용한다.

실제 개인정보나 운영 비밀은 사용하지 않고, 명백히 가상임을 표시한 테스트
데이터만 사용합니다.

## 8. Windows 다중 시나리오 시연 범위

| 발표 동작 | Host AI 채널 | 대시보드 채널 | 실제 검증 조건 |
|---|---|---|---|
| USB로 테스트 파일 복사 | `file_guard` | `USB_COPY` | Windows 이동식 드라이브와 파일 생성 감시 필요 |
| 브라우저·메신저 입력창에 붙여넣기 | `clipboard` | `CLIPBOARD` | Windows 키보드/클립보드 훅과 실제 `Ctrl+V` 필요 |
| Outlook 외부 메일 전송 | `outlook` | `EMAIL_ATTACHMENT` | Outlook 설치·로그인과 COM `ItemSend` 필요 |
| 웹메일 본문·첨부 전송 | `web_mail` | `WEB_UPLOAD` | HTTPS 프록시, 테스트 브라우저와 웹메일 계정 필요 |
| 클라우드 드라이브 업로드 | `drive_upload` | `CLOUD_DRIVE` | HTTPS 프록시, 테스트 브라우저와 드라이브 계정 필요 |

`MESSENGER` 전용 전송 훅은 현재 없습니다. Teams·Slack 등에서 민감정보를
붙여넣는 시연은 `clipboard -> CLIPBOARD`로만 기록해야 하며 `MESSENGER` 구현
증거로 표현하지 않습니다. `PRINT` 채널도 Web 로그 스키마와 샘플 데이터만
있고 Host 인쇄 훅은 구현되지 않았으므로 실제 차단 시연 범위에서 제외합니다.

### 라이브 v7 E2E 완료 증거

아래 항목은 실제 장비에서 모두 확인되기 전까지 완료로 표시하지 않습니다.
Host의 `demo_preflight.py` 통과는 설정과 실행 전제 조건을 점검한 발표 준비도
증거일 뿐, 라이브 v7 호출이나 E2E 완료 증거가 아닙니다. 실제 채널 이벤트로
동일한 `event_id`의 전 경로와 운영체제·애플리케이션 조치까지 따로 확인해야 합니다.
Web R009의 보호 API로 대시보드 토큰 일치까지 확인하지만, 실행 중인 Windows
훅과 실제 조치 성공 여부를 대신 증명하지는 않습니다. R009 사전 점검은
`SentryUserAgent` 실행 상태, WebProxy TCP 리스너와 QUIC 차단 규칙도 확인하지만,
실제 Outlook·USB·Clipboard·브라우저 트래픽 발생 증거는 아닙니다.

- [ ] AI 서버 루트 헬스 응답의 `status`가 `running`이다.
- [ ] Host 사전 점검에서 `SentryUserAgent`, WebProxy 리스너, QUIC 차단 규칙과
  5개 시나리오가 모두 `PASS`이다.
- [ ] 각 AI 응답과 대시보드 상세의 `model_version`이
  `koelectra-dlp-v7`이다.
- [ ] 각 대시보드 이벤트의 `detection_type`이 `HYBRID`이며 Mock 또는
  `RULE_BASED` 결과가 아니다.
- [ ] Host의 AI 요청, 로컬 이벤트, `POST /api/v1/logs` 저장 결과가 동일한
  `event_id`를 사용한다.
- [ ] 실제 채널별 조치와 대시보드의 `BLOCKED` / `WARNED` / `ALLOWED`가
  일치한다.
- [ ] USB 삭제, Clipboard 붙여넣기 차단·복원, Outlook `Cancel`, 웹 응답
  차단처럼 운영체제·애플리케이션 수준 결과를 별도로 확인한다.
