# =====================================================================
# 배치 테스트 (Colab 셀 붙여넣기용)
# ※ fastapi 서버를 띄운 셀이 계속 돌아가는 상태에서, 새 셀에 이 코드를 붙여넣어 실행하세요.
# =====================================================================
import os
import math
import subprocess
import sys
import time
import uuid

AI_API_TOKEN = os.environ.get("AI_API_TOKEN", "").strip()
try:
    AI_API_TOKEN_BYTES = AI_API_TOKEN.encode("ascii")
except UnicodeEncodeError as exc:
    raise RuntimeError("AI_API_TOKEN은 ASCII 무작위 문자열이어야 합니다.") from exc
if len(AI_API_TOKEN_BYTES) < 32:
    raise RuntimeError("32자 이상의 ASCII AI_API_TOKEN 환경변수가 필요합니다.")

subprocess.run(
    [sys.executable, "-m", "pip", "install", "-q", "requests", "pandas"],
    check=True,
)

import pandas as pd
import requests

# =====================================================================
# 0. 설정값
# =====================================================================
# 위 서버 실행 셀에서 나온 출력의 "에이전트 연동 엔드포인트" 앞부분(도메인)을 그대로 쓰면 됩니다.
BASE_URL = "https://여기에-현재-cloudflare-URL을-붙여넣으세요.trycloudflare.com"
ANALYZE_ENDPOINT = f"{BASE_URL}/api/v1/analyze"

# 드라이브 경로에 맞게 필요시 수정하세요
TRAIN_DATA_PATH = "/content/drive/MyDrive/2026_졸업과제/도메인편향개선2/train_dataset_v6.csv"
HARD_DATA_PATH = "/content/drive/MyDrive/2026_졸업과제/도메인편향개선2/hard_test_set_v1.csv"

REQUEST_TIMEOUT_SEC = 10
RETRY_COUNT = 2
SLEEP_BETWEEN_REQUESTS_SEC = 0.02
OUTPUT_CSV = "/content/drive/MyDrive/2026_졸업과제/도메인편향개선2/batch_live_test_results.csv"

# =====================================================================
# 1. 데이터 로드 및 병합
# =====================================================================
def load_datasets():
    frames = []
    train_df = pd.read_csv(TRAIN_DATA_PATH)[["text", "label"]].copy()
    train_df["source"] = "train_dataset_v6"
    train_df["case_type"] = "normal"
    frames.append(train_df)

    try:
        hard_df = pd.read_csv(HARD_DATA_PATH)[["text", "label", "case_type"]].copy()
        hard_df["source"] = "hard_test_set_v1"
        frames.append(hard_df)
    except FileNotFoundError:
        print(f"[안내] {HARD_DATA_PATH} 를 찾지 못해 train 데이터만 테스트합니다.")

    combined = pd.concat(frames, ignore_index=True)
    return combined.drop_duplicates(subset=["text"]).reset_index(drop=True)

# =====================================================================
# 2. API 호출
# =====================================================================
_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {AI_API_TOKEN}"})


def validate_api_response(raw: object, expected_event_id: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("response must be a JSON object")
    if raw.get("event_id") != expected_event_id:
        raise ValueError("response event_id mismatch")

    decision = raw.get("decision")
    if not isinstance(decision, str) or decision not in {"allow", "review", "block"}:
        raise ValueError("invalid decision")

    score = raw.get("confidence_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("invalid confidence_score type")
    score = float(score)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError("confidence_score must be finite and between 0 and 1")
    expected_decision = "block" if score >= 0.41 else "review" if score >= 0.20 else "allow"
    if decision != expected_decision:
        raise ValueError("decision does not match the returned score")

    if raw.get("model_version") != "koelectra-dlp-v7":
        raise ValueError("unexpected model_version")

    latency = raw.get("latency_ms")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        raise ValueError("invalid latency_ms type")
    latency = float(latency)
    if not math.isfinite(latency) or not 0.0 <= latency <= 600000.0:
        raise ValueError("latency_ms must be finite and between 0 and 600000")

    reason = raw.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise ValueError("invalid reason")
    return raw


def call_api(text: str) -> dict:
    event_id = f"batch-{uuid.uuid4().hex[:8]}"
    payload = {
        "event_id": event_id,
        "channel": "clipboard",
        "user_id": "batch-test-runner",
        "matched_patterns": [],
        "snippet": str(text)[:4000],
        "metadata": {"app": "batch_test", "dest": "internal", "severity_hint": "n/a"},
    }
    last_error = None
    for attempt in range(RETRY_COUNT + 1):
        try:
            resp = _session.post(ANALYZE_ENDPOINT, json=payload, timeout=REQUEST_TIMEOUT_SEC)
            if resp.status_code == 200:
                try:
                    return validate_api_response(resp.json(), event_id)
                except (TypeError, ValueError) as exc:
                    return {
                        "decision": None,
                        "confidence_score": None,
                        "error": f"응답 계약 위반: {exc}",
                    }
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                break
        except requests.exceptions.RequestException as e:
            last_error = str(e)
        if attempt < RETRY_COUNT:
            time.sleep(0.5)
    return {"decision": None, "confidence_score": None, "error": last_error}

# =====================================================================
# 3. 실행
# =====================================================================
df = load_datasets()
print(f"[INFO] 총 {len(df)}건 테스트 시작 -> {ANALYZE_ENDPOINT}")
print(f"[INFO] label 분포: {df['label'].value_counts().to_dict()}")
print("[주의] 학습 데이터가 포함된 연동 점검이며 held-out 모델 품질 증거가 아닙니다.")

results = []
for i, row in df.iterrows():
    res = call_api(row["text"])
    results.append({
        "text": row["text"], "label": row["label"], "source": row["source"],
        "case_type": row["case_type"], "decision": res.get("decision"),
        "confidence_score": res.get("confidence_score"),
        "latency_ms": res.get("latency_ms"), "event_id": res.get("event_id"),
        "model_version": res.get("model_version"), "reason": res.get("reason"),
        "error": res.get("error"),
    })
    if (i + 1) % 20 == 0 or (i + 1) == len(df):
        print(f"  ... {i + 1}/{len(df)} 처리 완료")
    time.sleep(SLEEP_BETWEEN_REQUESTS_SEC)

result_df = pd.DataFrame(results)
result_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
print(f"\n[INFO] 결과 저장: {OUTPUT_CSV}")

# =====================================================================
# 4. 리포트 출력
# =====================================================================
ok = result_df[result_df["error"].isna()]
failed = result_df[result_df["error"].notna()]

print("\n" + "=" * 60)
print(f"성공: {len(ok)}건 / 실패: {len(failed)}건")
if len(failed) > 0:
    print("\n[실패 케이스]")
    print(failed[["text", "error"]].head(10).to_string(index=False))

if len(ok) > 0:
    print("\n[decision 분포 (label별)]")
    print(pd.crosstab(ok["label"], ok["decision"]))

    print("\n[confidence_score 통계 (label별)]")
    print(ok.groupby("label")["confidence_score"].describe()[["count", "mean", "std", "min", "25%", "50%", "75%", "max"]])

    label1 = ok[ok["label"] == 1]
    label0 = ok[ok["label"] == 0]
    fn = (label1["decision"] == "allow").sum()
    caught = (label1["decision"] != "allow").sum()
    hard_fp = (label0["decision"] == "block").sum()
    soft_fp = (label0["decision"] == "review").sum()
    clean_allow = (label0["decision"] == "allow").sum()

    print("\n[판정 품질 요약]")
    print(f"  기밀(label=1) Recall(block/review로 걸러짐): {caught}/{len(label1)} ({caught/len(label1)*100:.1f}%)")
    print(f"  기밀(label=1) 완전히 놓침(allow):              {fn}/{len(label1)} ({fn/len(label1)*100:.1f}%)  <- 가장 치명적")
    print(f"  비기밀(label=0) 정확히 허용(allow):             {clean_allow}/{len(label0)} ({clean_allow/len(label0)*100:.1f}%)")
    print(f"  비기밀(label=0) review 분류(알람 피로 요인):    {soft_fp}/{len(label0)} ({soft_fp/len(label0)*100:.1f}%)")
    print(f"  비기밀(label=0) 완전 오탐(block, 업무 방해):   {hard_fp}/{len(label0)} ({hard_fp/len(label0)*100:.1f}%)")

    if "hard_test_set_v1" in ok["source"].values:
        print("\n[source별 판정 품질]")
        for src in ok["source"].unique():
            sub = ok[ok["source"] == src]
            sub1 = sub[sub["label"] == 1]
            sub0 = sub[sub["label"] == 0]
            r = (sub1["decision"] != "allow").mean() * 100 if len(sub1) else float("nan")
            fp = (sub0["decision"] == "block").mean() * 100 if len(sub0) else float("nan")
            print(f"  {src}: 기밀 recall(block/review)={r:.1f}%  비기밀 오탐(block)={fp:.1f}%")
else:
    print("성공한 요청이 없습니다. BASE_URL / 서버 상태를 확인하세요.")
print("=" * 60)
