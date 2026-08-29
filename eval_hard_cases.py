# -*- coding: utf-8 -*-
"""
eval_hard_cases.py

train_koelectra_v7.py로 재학습한 모델(./koelectra-dlp-v7/final)을
하드케이스 보정셋(hard_test_set_v1.csv)에 돌려서 경계 문맥의 실패 양상을
점검합니다. threshold_analysis.py가 이 결과를 임계값 후보 선정에 사용하므로
최종 held-out 성능 증거로 사용하지 않습니다.

- hard_positive: 트리거 키워드("보안","대외비","극비" 등) 없이
  문맥/수치만으로 기밀이라고 판단해야 하는 문장 (label=1)
- hard_negative: 트리거 키워드가 있지만 실제로는 비기밀인 문장
  (안내/교육/행정 공지 등) (label=0)

이 문장들은 학습 split과 별도로 작성되었지만 임계값 보정에 사용됩니다.
정확도가 크게 떨어지면 모델이 표면적 패턴에 의존하고 있다는 진단 신호입니다.

실행 환경: train_koelectra_v7.py와 같은 폴더 (모델 저장 경로 동일)
"""

import pandas as pd
import numpy as np
import torch
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support, confusion_matrix
)
from transformers import AutoTokenizer, AutoModelForSequenceClassification

MODEL_DIR = "./koelectra-dlp-v7/final"
HARD_SET_PATH = "data/hard_test_set_v1.csv"
MAX_LENGTH = 128
THRESHOLD = 0.41  # server.py의 현재 block 컷라인

device = "cuda" if torch.cuda.is_available() else "cpu"

# ------------------------------------------------------------------
# 1. 모델 / 토크나이저 로드
# ------------------------------------------------------------------
tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR).to(device)
model.eval()

# ------------------------------------------------------------------
# 2. 하드케이스 로드
# ------------------------------------------------------------------
df = pd.read_csv(HARD_SET_PATH)
print(f"[INFO] 하드케이스 {len(df)}건 로드 완료 "
      f"(hard_positive={sum(df.case_type=='hard_positive')}, "
      f"hard_negative={sum(df.case_type=='hard_negative')})")

# ------------------------------------------------------------------
# 3. 추론
# ------------------------------------------------------------------
def predict(texts):
    enc = tokenizer(
        texts, padding=True, truncation=True,
        max_length=MAX_LENGTH, return_tensors="pt"
    ).to(device)
    with torch.no_grad():
        logits = model(**enc).logits
    probs = torch.softmax(logits, dim=1)[:, 1].cpu().numpy()  # P(기밀)
    return probs

probs = predict(df["text"].tolist())
preds = (probs >= THRESHOLD).astype(int)

df["prob_sensitive"] = probs
df["pred"] = preds
df["correct"] = (df["pred"] == df["label"])

# ------------------------------------------------------------------
# 4. 전체 성능
# ------------------------------------------------------------------
precision, recall, f1, _ = precision_recall_fscore_support(
    df["label"], df["pred"], average="binary", pos_label=1, zero_division=0
)
acc = accuracy_score(df["label"], df["pred"])
cm = confusion_matrix(df["label"], df["pred"])

print("\n=== 전체 하드케이스 성능 ===")
print(f"Accuracy : {acc:.4f}")
print(f"Precision: {precision:.4f}")
print(f"Recall   : {recall:.4f}")
print(f"F1       : {f1:.4f}")
print("\nConfusion Matrix (행:실제, 열:예측) [0=비기밀, 1=기밀]")
print(cm)

# ------------------------------------------------------------------
# 5. 케이스 타입별 성능 (진짜 중요한 부분)
# ------------------------------------------------------------------
print("\n=== 케이스 타입별 정답률 ===")
for ct in ["hard_positive", "hard_negative"]:
    sub = df[df.case_type == ct]
    ct_acc = (sub["correct"]).mean()
    print(f"{ct:15s}: {ct_acc:.1%} ({sub['correct'].sum()}/{len(sub)})")

print("\n=== 도메인별 정답률 ===")
for dm in df["domain"].unique():
    sub = df[df.domain == dm]
    dm_acc = (sub["correct"]).mean()
    print(f"{dm:10s}: {dm_acc:.1%} ({sub['correct'].sum()}/{len(sub)})")

# ------------------------------------------------------------------
# 6. 오답 케이스 상세 출력 (가장 중요 - 실패 패턴 분석용)
# ------------------------------------------------------------------
wrong = df[~df["correct"]].sort_values("case_type")
print(f"\n=== 오답 케이스 상세 ({len(wrong)}건) ===")
if len(wrong) == 0:
    print("오답 없음 - 하드케이스도 모두 통과했습니다.")
else:
    for _, row in wrong.iterrows():
        print(f"\n[{row['case_type']} / {row['domain']}] 실제={row['label']} 예측={row['pred']} "
              f"(확률={row['prob_sensitive']:.3f})")
        print(f"  문장: {row['text']}")
        print(f"  비고: {row['note']}")

# ------------------------------------------------------------------
# 7. 저장
# ------------------------------------------------------------------
df.to_csv("hard_test_results_v7.csv", index=False, encoding="utf-8-sig")
print(f"\n[INFO] 상세 결과를 hard_test_results_v7.csv 로 저장했습니다.")
