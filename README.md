# Sentry AI 분석 서버 — API 문서

담당 역할: AI Model 담당
최종 갱신: 2026-08-13
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
| `matched_patterns` | string[] | X | 1차 Rule 필터에서 매칭된 패턴명 (없으면 빈 배열) |
| `snippet` | string | O | 분석할 텍스트 (PII는 사전에 마스킹 권장) |
| `metadata` | object | X | `app`, `dest`, `severity_hint` 포함 가능 |

### Request 예시
```json
{
  "event_id": "evt-20260813-00123",
  "channel": "clipboard",
  "user_id": "device-12345",
  "matched_patterns": ["rrn"],
  "snippet": "이OO 부장 외 4명에게 1인당 5천만 원 상당의 특별 스톡옵션을 배정하기로 결정했습니다.",
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
| 0.20 ~ 0.41 | review |
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
