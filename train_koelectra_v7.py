# -*- coding: utf-8 -*-
"""
train_koelectra_v7.py

목적: 구어체/캐주얼 문체 다양성을 보강한 데이터(train_dataset_v6.csv, 223건)로 재학습
- 기존 train_dataset_v5.csv(183건, 격식체만)의 한계였던 "구어체 미학습" 문제를 보완
- register_augment_v1.csv(구어체 40건, 기밀/비기밀 균형)를 추가로 병합

학습/검증/최종 테스트를 60/20/20 stratified split으로 분리한다.

실행 환경: Google Colab (GPU: T4 권장)
사전 설치: !pip install -q "transformers>=4.41,<5" datasets scikit-learn accelerate torch
"""

import os
import json
import random
import numpy as np
import pandas as pd
import torch
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    confusion_matrix, classification_report, roc_auc_score
)
from datasets import Dataset
from transformers import (
    AutoTokenizer, AutoModelForSequenceClassification,
    TrainingArguments, Trainer, EarlyStoppingCallback
)

# ------------------------------------------------------------------
# 0. 설정값
# ------------------------------------------------------------------
SEED = 42
MODEL_NAME = "monologg/koelectra-base-v3-discriminator"
MODEL_VERSION = "koelectra-dlp-v7"
DATA_PATH = "data/train_dataset_v6.csv"     # 추적된 구어체 보강판 (223건)
OUTPUT_DIR = "./koelectra-dlp-v7"
MAX_LENGTH = 128
TEST_SIZE = 0.2
VALIDATION_SIZE = 0.2
NUM_EPOCHS = 8
BATCH_SIZE = 8
LEARNING_RATE = 2e-5

random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[INFO] Using device: {device}")

# ------------------------------------------------------------------
# 1. 데이터 로드 및 Train/Test 분할 (Stratified)
# ------------------------------------------------------------------
df = pd.read_csv(DATA_PATH)
assert {"text", "label"}.issubset(df.columns), "CSV는 text, label 컬럼이 필요합니다."
df["label"] = df["label"].astype(int)

print(f"[INFO] 전체 데이터: {len(df)}건 | label 분포: {df['label'].value_counts().to_dict()}")

train_validation_df, test_df = train_test_split(
    df, test_size=TEST_SIZE, stratify=df["label"], random_state=SEED
)
relative_validation_size = VALIDATION_SIZE / (1 - TEST_SIZE)
train_df, validation_df = train_test_split(
    train_validation_df,
    test_size=relative_validation_size,
    stratify=train_validation_df["label"],
    random_state=SEED,
)
train_df = train_df.reset_index(drop=True)
validation_df = validation_df.reset_index(drop=True)
test_df = test_df.reset_index(drop=True)

train_df.to_csv("train_split_v7.csv", index=False, encoding="utf-8-sig")
validation_df.to_csv("validation_split_v7.csv", index=False, encoding="utf-8-sig")
test_df.to_csv("test_split_v7.csv", index=False, encoding="utf-8-sig")

print(f"[INFO] Train: {len(train_df)}건 {train_df['label'].value_counts().to_dict()}")
print(f"[INFO] Validation: {len(validation_df)}건 {validation_df['label'].value_counts().to_dict()}")
print(f"[INFO] Test : {len(test_df)}건 {test_df['label'].value_counts().to_dict()}")

# ------------------------------------------------------------------
# 2. Tokenizer / Dataset 준비
# ------------------------------------------------------------------
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

def tokenize_fn(batch):
    return tokenizer(
        batch["text"], padding="max_length", truncation=True, max_length=MAX_LENGTH,
    )

train_ds = Dataset.from_pandas(train_df[["text", "label"]])
validation_ds = Dataset.from_pandas(validation_df[["text", "label"]])
test_ds = Dataset.from_pandas(test_df[["text", "label"]])
train_ds = train_ds.map(tokenize_fn, batched=True)
validation_ds = validation_ds.map(tokenize_fn, batched=True)
test_ds = test_ds.map(tokenize_fn, batched=True)
train_ds = train_ds.rename_column("label", "labels")
validation_ds = validation_ds.rename_column("label", "labels")
test_ds = test_ds.rename_column("label", "labels")
cols = ["input_ids", "attention_mask", "labels"]
train_ds.set_format(type="torch", columns=cols)
validation_ds.set_format(type="torch", columns=cols)
test_ds.set_format(type="torch", columns=cols)

# ------------------------------------------------------------------
# 3. 모델 로드
# ------------------------------------------------------------------
model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME, num_labels=2).to(device)
model.config.sentry_model_version = MODEL_VERSION
model.config.id2label = {0: "non_sensitive", 1: "sensitive"}
model.config.label2id = {"non_sensitive": 0, "sensitive": 1}

# ------------------------------------------------------------------
# 4. 평가 지표 함수
# ------------------------------------------------------------------
def compute_metrics(eval_pred):
    logits, labels = eval_pred
    probs = torch.softmax(torch.tensor(logits), dim=1).numpy()
    preds = np.argmax(logits, axis=1)
    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, preds, average="binary", pos_label=1, zero_division=0
    )
    acc = accuracy_score(labels, preds)
    try:
        auc = roc_auc_score(labels, probs[:, 1])
    except ValueError:
        auc = float("nan")
    return {"accuracy": acc, "precision": precision, "recall": recall, "f1": f1, "roc_auc": auc}

# ------------------------------------------------------------------
# 5. TrainingArguments / Trainer
# ------------------------------------------------------------------
training_args = TrainingArguments(
    output_dir=OUTPUT_DIR,
    num_train_epochs=NUM_EPOCHS,
    per_device_train_batch_size=BATCH_SIZE,
    per_device_eval_batch_size=BATCH_SIZE,
    learning_rate=LEARNING_RATE,
    weight_decay=0.01,
    eval_strategy="epoch",
    save_strategy="epoch",
    load_best_model_at_end=True,
    metric_for_best_model="f1",
    greater_is_better=True,
    logging_steps=10,
    save_total_limit=2,
    seed=SEED,
    report_to="none",
    fp16=torch.cuda.is_available(),
)

trainer = Trainer(
    model=model, args=training_args,
    train_dataset=train_ds, eval_dataset=validation_ds,
    compute_metrics=compute_metrics,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=3)],
)

# ------------------------------------------------------------------
# 6. 학습
# ------------------------------------------------------------------
print("[INFO] 학습을 시작합니다...")
trainer.train()

# ------------------------------------------------------------------
# 7. 최종 Test셋 평가
# ------------------------------------------------------------------
print("\n[INFO] Test셋 최종 평가를 진행합니다...")
pred_output = trainer.predict(test_ds)
logits = pred_output.predictions
labels = pred_output.label_ids
preds = np.argmax(logits, axis=1)
probs = torch.softmax(torch.tensor(logits), dim=1).numpy()[:, 1]

cm = confusion_matrix(labels, preds)
report = classification_report(labels, preds, target_names=["비기밀(0)", "기밀(1)"], digits=4)

print("\n=== Confusion Matrix ===")
print("            예측:비기밀  예측:기밀")
print(f"실제:비기밀   {cm[0][0]:>8}  {cm[0][1]:>8}")
print(f"실제:기밀     {cm[1][0]:>8}  {cm[1][1]:>8}")
print("\n=== Classification Report ===")
print(report)

final_metrics = compute_metrics((logits, labels))
print("\n=== 최종 지표 요약 ===")
for k, v in final_metrics.items():
    print(f"{k}: {v:.4f}")

result_summary = {
    "metrics": final_metrics, "confusion_matrix": cm.tolist(),
    "test_size": len(test_df), "validation_size": len(validation_df),
    "train_size": len(train_df),
    "model_name": MODEL_NAME,
    "hyperparams": {"epochs": NUM_EPOCHS, "batch_size": BATCH_SIZE,
                    "learning_rate": LEARNING_RATE, "max_length": MAX_LENGTH},
}
with open("eval_results_v7.json", "w", encoding="utf-8") as f:
    json.dump(result_summary, f, ensure_ascii=False, indent=2)

test_df_out = test_df.copy()
test_df_out["pred"] = preds
test_df_out["prob_sensitive"] = probs
test_df_out.to_csv("test_predictions_v7.csv", index=False, encoding="utf-8-sig")

validation_output = trainer.predict(validation_ds)
validation_df_out = validation_df.copy()
validation_df_out["pred"] = np.argmax(validation_output.predictions, axis=1)
validation_df_out["prob_sensitive"] = torch.softmax(
    torch.tensor(validation_output.predictions), dim=1
).numpy()[:, 1]
validation_df_out.to_csv(
    "validation_predictions_v7.csv", index=False, encoding="utf-8-sig"
)

# ------------------------------------------------------------------
# 8. 모델 저장
# ------------------------------------------------------------------
trainer.save_model(f"{OUTPUT_DIR}/final")
tokenizer.save_pretrained(f"{OUTPUT_DIR}/final")
print(f"\n[INFO] 모델 저장 완료: {OUTPUT_DIR}/final")
print("[INFO] 다음 단계: eval_hard_cases.py / threshold_analysis.py / batch_test_live_api.py 재실행 필요")
