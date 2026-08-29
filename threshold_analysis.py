# -*- coding: utf-8 -*-
"""
threshold_analysis.py

목적: v7 block/review 컷라인의 수학적 근거를 재검증하기 위한 ROC / PR 곡선 분석
- 입력 1: validation_predictions_v7.csv (재학습 시 생성된 검증셋 예측 결과)
- 입력 2: hard_test_results_v7.csv       (경계 하드케이스 보정 결과)
  -> 둘을 합쳐 임계값 후보를 선정합니다. 검증셋만 쓰면 확률이
     0.99+/0.01- 로 양극단에 몰려 있어 곡선이 사실상 무의미합니다.
     하드케이스가 섞여야 "애매한 경계 구간"이 곡선에 반영됩니다.
- 입력 3: test_predictions_v7.csv (임계값 선정에 사용하지 않는 최종 확인용)

출력:
- threshold_analysis.png (ROC curve + PR curve, 후보 threshold 표시)
- threshold_candidates.csv (여러 기준별 추천 threshold와 성능 비교표)

실행 환경: Colab (train_koelectra_v7.py, eval_hard_cases.py와 같은 폴더)
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.metrics import (
    roc_curve, auc, precision_recall_curve, fbeta_score,
    precision_score, recall_score, f1_score
)

VALIDATION_PRED_PATH = "validation_predictions_v7.csv"
HARD_PRED_PATH = "hard_test_results_v7.csv"
HELD_OUT_TEST_PRED_PATH = "test_predictions_v7.csv"
CURRENT_THRESHOLD = 0.41
REVIEW_THRESHOLD = 0.20
OUTPUT_PNG = "threshold_analysis.png"
OUTPUT_CSV = "threshold_candidates.csv"

# ------------------------------------------------------------------
# 1. 데이터 로드 및 병합
# ------------------------------------------------------------------
validation_df = pd.read_csv(VALIDATION_PRED_PATH)[["text", "label", "prob_sensitive"]]
hard_df = pd.read_csv(HARD_PRED_PATH)[["text", "label", "prob_sensitive"]]
test_df = pd.read_csv(HELD_OUT_TEST_PRED_PATH)[["text", "label", "prob_sensitive"]]

combined = pd.concat([validation_df, hard_df], ignore_index=True)
combined = combined.drop_duplicates(subset=["text"]).reset_index(drop=True)

y_true = combined["label"].values
y_prob = combined["prob_sensitive"].values

print(f"[INFO] Validation: {len(validation_df)}건 / 하드케이스: {len(hard_df)}건")
print(f"[INFO] 임계값 보정셋(중복 제거): {len(combined)}건 "
      f"(label 분포: {combined['label'].value_counts().to_dict()})")
print(f"[INFO] 임계값 선정에서 제외한 최종 Test셋: {len(test_df)}건")

# ------------------------------------------------------------------
# 2. ROC Curve + Youden's J statistic
# ------------------------------------------------------------------
fpr, tpr, roc_thresholds = roc_curve(y_true, y_prob)
roc_auc = auc(fpr, tpr)

youden_j = tpr - fpr
best_j_idx = np.argmax(youden_j)
best_j_threshold = roc_thresholds[best_j_idx]

print(f"\n[ROC] AUC = {roc_auc:.4f}")
print(f"[ROC] Youden's J 최적 threshold = {best_j_threshold:.4f} "
      f"(TPR={tpr[best_j_idx]:.3f}, FPR={fpr[best_j_idx]:.3f})")

# ------------------------------------------------------------------
# 3. Precision-Recall Curve + F1 / F2 최적점
# ------------------------------------------------------------------
precisions, recalls, pr_thresholds = precision_recall_curve(y_true, y_prob)

# precision_recall_curve는 thresholds가 점 개수보다 1개 적으므로 마지막 점 제외하고 매칭
f1_scores = 2 * (precisions[:-1] * recalls[:-1]) / (precisions[:-1] + recalls[:-1] + 1e-9)
best_f1_idx = np.argmax(f1_scores)
best_f1_threshold = pr_thresholds[best_f1_idx]

# F2: Recall에 2배 가중치 (DLP는 기밀 유출을 놓치는 게 훨씬 치명적이므로 Recall 우선)
beta = 2
f2_scores = (1 + beta**2) * (precisions[:-1] * recalls[:-1]) / (
    (beta**2 * precisions[:-1]) + recalls[:-1] + 1e-9
)
best_f2_idx = np.argmax(f2_scores)
best_f2_threshold = pr_thresholds[best_f2_idx]

print(f"\n[PR] F1 최적 threshold = {best_f1_threshold:.4f} "
      f"(Precision={precisions[best_f1_idx]:.3f}, Recall={recalls[best_f1_idx]:.3f}, F1={f1_scores[best_f1_idx]:.3f})")
print(f"[PR] F2 최적 threshold = {best_f2_threshold:.4f} "
      f"(Precision={precisions[best_f2_idx]:.3f}, Recall={recalls[best_f2_idx]:.3f}, F2={f2_scores[best_f2_idx]:.3f})")

# ------------------------------------------------------------------
# 4. 후보 threshold 비교표
# ------------------------------------------------------------------
def eval_at(th, labels=y_true, probabilities=y_prob):
    pred = (probabilities >= th).astype(int)
    return {
        "threshold": round(th, 4),
        "precision": round(precision_score(labels, pred, zero_division=0), 4),
        "recall": round(recall_score(labels, pred, zero_division=0), 4),
        "f1": round(f1_score(labels, pred, zero_division=0), 4),
        "f2": round(fbeta_score(labels, pred, beta=2, zero_division=0), 4),
    }

candidates = pd.DataFrame([
    {"criterion": "현재 block 적용값 (0.41)", **eval_at(CURRENT_THRESHOLD)},
    {"criterion": "현재 review 경계값 (0.20)", **eval_at(REVIEW_THRESHOLD)},
    {"criterion": "Youden's J (ROC 최적)", **eval_at(best_j_threshold)},
    {"criterion": "F1 최적 (PR curve)", **eval_at(best_f1_threshold)},
    {"criterion": "F2 최적 (Recall 가중, DLP 권장)", **eval_at(best_f2_threshold)},
])
print("\n=== Threshold 후보 비교표 ===")
print(candidates.to_string(index=False))
candidates.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
print(f"\n[INFO] 비교표 저장: {OUTPUT_CSV}")

test_metrics = eval_at(
    CURRENT_THRESHOLD,
    test_df["label"].values,
    test_df["prob_sensitive"].values,
)
print("\n=== 고정된 현재 block threshold의 최종 Test 지표 ===")
print(test_metrics)
print("[주의] 이 결과를 본 뒤 threshold를 다시 고르면 Test셋은 더 이상 held-out이 아닙니다.")

# ------------------------------------------------------------------
# 5. 시각화 (ROC + PR curve, 후보 threshold 표시)
# ------------------------------------------------------------------
fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))

# --- ROC ---
ax = axes[0]
ax.plot(fpr, tpr, label=f"ROC curve (AUC={roc_auc:.3f})", linewidth=2)
ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="Random")
ax.scatter(fpr[best_j_idx], tpr[best_j_idx], color="red", zorder=5,
           label=f"Youden's J opt (th={best_j_threshold:.2f})")
ax.set_xlabel("False Positive Rate")
ax.set_ylabel("True Positive Rate")
ax.set_title("ROC Curve")
ax.legend(loc="lower right", fontsize=9)
ax.grid(alpha=0.3)

# --- PR ---
ax = axes[1]
ax.plot(recalls, precisions, label="PR curve", linewidth=2, color="tab:orange")
ax.scatter(recalls[best_f1_idx], precisions[best_f1_idx], color="red", zorder=5,
           label=f"F1 opt (th={best_f1_threshold:.2f})")
ax.scatter(recalls[best_f2_idx], precisions[best_f2_idx], color="green", zorder=5,
           label=f"F2 opt (th={best_f2_threshold:.2f})")
ax.set_xlabel("Recall")
ax.set_ylabel("Precision")
ax.set_title("Precision-Recall Curve")
ax.legend(loc="lower left", fontsize=9)
ax.grid(alpha=0.3)

plt.tight_layout()
plt.savefig(OUTPUT_PNG, dpi=150)
print(f"[INFO] 그래프 저장: {OUTPUT_PNG}")
plt.show()
