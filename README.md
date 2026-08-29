# Sentry AI 분석 서버 — API 문서

담당 역할: AI Model 담당
최종 갱신: 2026-08-29
모델 버전: `koelectra-dlp-v7`

## 1. 엔드포인트

```
POST {BASE_URL}/api/v1/analyze
Content-Type: application/json
Authorization: Bearer {AI_API_TOKEN}
```

주의: `{BASE_URL}`은 Colab 세션마다 바뀝니다. (Cloudflare Tunnel 무료 플랜은 고정 도메인 미지원)

Cloudflare 주소는 공개 인터넷에 열리므로 서버 실행 전 최소 32자의 ASCII 무작위 토큰을 환경변수로 설정합니다. 짧거나 비ASCII인 토큰이면 패키지 설치·모델 로드·터널 실행 전에 중단합니다.

```python
import os
import secrets

os.environ["AI_API_TOKEN"] = secrets.token_urlsafe(32)
# 필요한 경우만 기본 Colab Drive 경로를 덮어씁니다.
# os.environ["SENTRY_MODEL_PATH"] = "/content/drive/MyDrive/.../koelectra-dlp-v7/final"
```

같은 `AI_API_TOKEN`을 Host의 환경변수 또는 Git에서 제외된 `config/settings.local.yaml`의 `server.ai_token`, Web의 `AI_SERVER_TOKEN`에 설정하되 추적 설정·Git·채팅·발표 화면에 값을 남기지 않습니다. Host 환경변수가 local YAML보다 우선합니다.

`server.py`는 **Google Colab의 Linux amd64 런타임 전용**이며, Colab 셀 붙여넣기와 그 런타임 안의 `python server.py` 실행을 지원합니다. macOS·Windows·ARM·일반 로컬 Python 실행 지원을 뜻하지 않습니다. 독립 실행은 `Ctrl+C`까지 유지되고, Colab에서 셀을 다시 실행할 때는 그 셀이 앞서 만든 서버·터널만 종료합니다. 터널이 예기치 않게 종료되면 연결이 끊긴 로컬 API도 중단하며, 다른 `cloudflared` 프로세스는 광역 종료하지 않습니다. 내려받는 `cloudflared`는 `2026.8.2` Linux amd64와 공식 SHA-256으로 고정·검증하며, 다운로드가 60초 동안 응답하지 않으면 중단합니다. FastAPI·Uvicorn·Pydantic·PyTorch·Transformers는 코드가 전제한 호환 메이저 범위로 설치해 기존 Pydantic v1 환경을 그대로 사용하지 않습니다.

서버는 체크포인트의 `sentry_model_version`, 2-label 구성, `label 1 = sensitive` 매핑과 준비 추론을 모두 확인한 뒤에만 `status=running`을 반환합니다. 기존 Drive 체크포인트에 이 메타데이터가 없다면 이름만 보고 v7로 간주하지 않고 분석 준비를 거절하므로, 현재 `train_koelectra_v7.py`로 재학습한 `final` 체크포인트를 사용해야 합니다.

## 2. Request

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `event_id` | string | O | 1~120자. Host Agent가 발급하는 이벤트 고유 ID |
| `channel` | string | O | Host/Web 계약의 `clipboard`, `outlook`, `smtp`, `web_mail`, `drive_upload`, `file_guard`, `http`, `usb`, `web_upload`, `email_attachment`, `print`, `messenger` 중 하나 |
| `user_id` | string | O | 1~100자. 사용자/디바이스 식별자 |
| `matched_patterns` | string[] | X | 최대 100개, 각 1~100자. 1차 Rule 필터 패턴 ID |
| `snippet` | string | O | 1~4000자. 분석할 텍스트 |
| `metadata` | object | X | 최대 50개 항목. 키 1~100자, 문자열 값 최대 500자, 수치는 유한값만 허용 |

모델 문맥을 위해 Host가 `snippet` 원문을 전송하므로 실제 개인정보·운영 비밀은 시연에 사용하지 않습니다. AI 서버 JSONL에는 snippet 원문/미리보기 대신 길이만, `user_id` 대신 SHA-256 기반 가명값만 기록합니다.

`/api/v1/analyze`는 JSON을 읽기 전에 Bearer 토큰을 확인합니다. `Content-Length`가 있으면 크기를 먼저 검사하고, chunked 요청도 수신 중 합계를 검사해 본문을 256 KiB로 제한합니다. 성공 감사 로그는 응답 후 백그라운드에서 기록됩니다.

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
| `reason` | string | 판정 사유 요약 |

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

`server.py`에 적용된 현재 값입니다. 저장소에는 v7 모델 가중치·실행 산출물이 없으므로 `train_koelectra_v7.py` → `eval_hard_cases.py` → `threshold_analysis.py`를 같은 checkpoint로 재실행해야 합니다. 학습 스크립트는 Train/Validation/Test를 60/20/20으로 분리하고, early stopping은 Validation만 사용합니다. 임계값 후보는 `validation_predictions_v7.csv`와 `hard_test_results_v7.csv`로 고른 뒤 `test_predictions_v7.csv`는 최종 확인에 한 번만 사용합니다. 결과를 본 뒤 임계값을 다시 고르면 Test는 더 이상 held-out 증거가 아닙니다. `threshold_candidates.csv`까지 검토하기 전에는 0.41/0.20을 재현 검증 완료로 표현하지 않습니다.

## 5. 에러 응답

| status code | 상황 |
|---|---|
| 401 | Bearer 토큰 누락 또는 불일치 |
| 400 | `snippet`이 비어있거나 `Content-Length` 형식이 잘못됨 |
| 413 | 요청 본문이 256 KiB 초과 |
| 422 | 요청 스키마 검증 실패 |
| 503 | AI 모델이 로드되지 않은 상태 (서버 기동 직후 등) |
| 500 | 추론 중 내부 오류. 응답과 JSONL에는 내부 예외 문자열을 노출하지 않음 |

Host Agent는 착수보고서 3.5절 Fail-Open 정책(AI 서버 무응답/에러 시 기본 허용)을 따르므로, 위 에러 상황에서도 서비스가 멈추지 않도록 타임아웃/재시도 로직 권장.

## 6. curl 테스트 예시

```bash
curl -X POST {BASE_URL}/api/v1/analyze \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer {AI_API_TOKEN}" \
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

토큰만 확인할 때는 추론·이벤트 로그를 만들지 않는 `GET {BASE_URL}/api/v1/agent-check`에 같은 Bearer 헤더를 사용합니다. 서버 셀에 Cloudflare 주소가 표시된 뒤 새 셀에서 아래처럼 배치 검증을 실행합니다.

```python
import os

os.environ["AI_BASE_URL"] = "https://<현재-주소>.trycloudflare.com"
%run batch_test_live_api.py
```

`batch_test_live_api.py`는 추적 파일을 수정하지 않고 `AI_BASE_URL` 환경변수의 현재 HTTPS 도메인을 사용하며, 주소가 없거나 경로까지 포함되면 패키지 설치 전에 중단합니다. 이 스크립트는 v6 학습 데이터와 하드케이스를 대상으로 응답의 `event_id`, 점수-판정 경계, `koelectra-dlp-v7`, 지연시간 계약까지 확인한 뒤 정상 건만 성공 집계합니다. 401·422와 응답 계약 위반은 재호출하지 않으며, 재시도는 일시적인 통신 오류와 408/425/429/5xx에만 적용합니다. 이 배치에는 학습 데이터가 포함되므로 **held-out 모델 품질 증거로 사용하지 않습니다.**

### Blind OOD 최종 평가 게이트

`eval_ood_final.py`는 threshold 선택이 끝난 뒤 독립 보관한 OOD를 라이브
`/api/v1/analyze`에 단 한 번 보내 최종 일반화 성능을 고정하는 용도입니다.
순서는 반드시 다음과 같습니다.

1. 학습·Validation·hard-case 분석을 마치고 사용할 v7 checkpoint와
   `block_threshold` / `review_threshold`를 먼저 동결합니다.
2. 서버 실행 전에 `SENTRY_MODEL_PATH`를 명시합니다. 평가 담당자가 저장소 밖에
   보관한 OOD CSV와 그 경로의 실제 weights 파일 SHA-256을 계산해 외부 lock
   manifest에 기록합니다. OOD 실제 행 수를 `expected_rows`에 고정하고
   품질 합격선 `minimum_f1` / `minimum_recall`도 결과를 보기 전에 고정한 뒤
   `status`를 `locked`로 바꿉니다. F1 90% 이상·Recall 우선 목표가 무력화되지
   않도록 두 합격선 모두 0.90 이상 1 이하만 허용합니다.
3. 같은 Colab 런타임에서 라이브 서버를 연 뒤 아래 명령을 한 번 실행합니다.
4. 생성된 aggregate JSON의 SHA-256과 precision/recall/F1/confusion을
   공개합니다. OOD 원문·행별 예측은 공개하지 않습니다.
5. 결과를 본 뒤 checkpoint, threshold, 데이터 또는 전처리를 다시 고르지
   않습니다. 바꾸면 기존 OOD는 튜닝 데이터가 된 것이므로 새 독립 OOD로
   처음부터 다시 평가해야 합니다.

추적된 `ood_eval_manifest.example.json`은 의도적으로 hash와 threshold가 비어
있고 `locked`가 아니므로 그대로는 항상 실패합니다. 완성 manifest와 OOD CSV는
Git에 추가하지 않습니다. OOD CSV 헤더는 정확히
`case_id,text,label,domain,style`이어야 하며 label 0/1 수가 같아야 합니다.
최소 16행·4개 domain이 필요하고, 각 domain에는 최소 2개 style, 각
domain×style 조합에는 label 0/1이 모두 있어야 합니다. 실제 총 행 수는
manifest의 `expected_rows`와 정확히 일치해야 합니다.
스크립트는 내부 중복과 추적된 `train_dataset_v6.csv` /
`hard_test_set_v1.csv`의 정규화 exact-overlap도 네트워크 호출 전에 거절합니다.

```bash
export AI_BASE_URL="https://<현재-주소>.trycloudflare.com"
# AI_API_TOKEN은 server.py를 연 값과 동일하게 이미 설정되어 있어야 합니다.
python eval_ood_final.py \
  --manifest /content/drive/MyDrive/private/ood_eval_lock_v1.json \
  --ood-csv /content/drive/MyDrive/private/ood_final_v1.csv \
  --checkpoint-file "${SENTRY_MODEL_PATH:?server.py 실행 전에 설정}/model.safetensors" \
  --output /content/drive/MyDrive/private/ood_eval_result_v7.json
```

`--checkpoint-file`은 실행 중인 서버의 `SENTRY_MODEL_PATH`가 실제로 읽은 weights
파일을 가리켜야 합니다. evaluator는 그 파일과 OOD CSV의 SHA-256, 루트 health의
checkpoint SHA-256·v7·두 threshold, 모든 분석 응답 계약을 함께 확인합니다.
서버는 모호한 checkpoint 선택을 피하기 위해 해당 경로에 단일
`model.safetensors` 또는 `pytorch_model.bin`이 있을 때만 준비 상태가 됩니다.
`.bin` checkpoint이면 명령의 파일명도 `pytorch_model.bin`으로 바꿉니다.
양성 예측은 고정한 `block_threshold` 이상으로 정의합니다. 통신 오류나 응답
계약 오류가 한 건이라도 있으면 metrics를 `null`, 전체 `status`를 `invalid`로
기록합니다. 전체 응답이 유효하면 F1과 Recall이 두 합격선을 모두 충족할 때만
`status=passed`, `quality_passed=true`, 종료 코드 0입니다. 하나라도 미달이면
metrics와 confusion을 보존한 채 `status=quality_failed`,
`quality_passed=false`, 종료 코드 2입니다. 결과 파일은 새 파일로만 생성하므로 같은
출력에 덮어써서 1회 평가 기록을 바꿀 수 없습니다. 최종 원자적 교체가 실패하면
선점한 `invalid` 결과와 복구 가능한 임시 JSON을 삭제하지 않습니다.

## 7. 현재 발표 통합 토폴로지

실제 Windows 시연에서는 Host Agent가 AI 판정과 대시보드 기록을 서로 다른
서버로 직접 전송합니다.

```text
Windows Host Agent
  ├─ POST {AI_BASE_URL}/api/v1/analyze ──> Colab의 koelectra-dlp-v7
  └─ POST {DASHBOARD_URL}/api/v1/logs ──> Web 서버·SQLite·대시보드
```

- `AI_BASE_URL`은 AI Model 담당이 실행한 Colab/Cloudflare 주소이며 Host는 같은 `AI_API_TOKEN`을 Bearer 헤더로 보냅니다.
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
| USB로 테스트 파일 복사 | `file_guard` | `USB_COPY` | Windows가 `DRIVE_REMOVABLE`로 보고하는 플래시 드라이브 필요; FIXED 외장 SSD는 제외 |
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
Web의 보호 API로 대시보드 토큰 일치까지 확인하지만, 실행 중인 Windows
훅과 실제 조치 성공 여부를 대신 증명하지는 않습니다. 현재 사전 점검은
`SentryUserAgent` 실행 상태, WebProxy TCP 리스너와 QUIC 차단 규칙도 확인하지만,
실제 Outlook·USB·Clipboard·브라우저 트래픽 발생 증거는 아닙니다.

- [ ] AI 서버 루트 헬스 응답의 `status`가 `running`이고 보호된 `/api/v1/agent-check`가 토큰을 승인한다.
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
