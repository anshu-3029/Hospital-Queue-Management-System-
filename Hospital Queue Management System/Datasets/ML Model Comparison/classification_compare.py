"""
Classification model comparison for ER Wait Time Prediction

Models compared:
- Logistic Regression
- SVC
- Random Forest Classifier
- Gradient Boosting Classifier
- XGBoost Classifier

Class labels:
- Low
- Medium
- High

Outputs:
- classification_model_comparison.csv
- classification_report.txt
- final_xgboost_classifier.pkl
- classification_model_columns.pkl
"""

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import joblib

from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    classification_report,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from xgboost import XGBClassifier

# ---------------------------------------------------------------------
# 1. LOAD DATA
# ---------------------------------------------------------------------
print("Loading dataset...")
df = pd.read_csv("ER Wait Time Dataset.csv")
df = df.drop_duplicates()

# ---------------------------------------------------------------------
# 2. CLEANING
# ---------------------------------------------------------------------
df.rename(columns={"Total Wait Time (min)": "wait_time"}, inplace=True)

leakage_cols = [
    "Visit ID",
    "Patient ID",
    "Hospital Name",
    "Patient Satisfaction",
    "Patient Outcome",
    "Time to Registration (min)",
    "Time to Triage (min)",
    "Time to Medical Professional (min)",
]
df.drop(columns=leakage_cols, inplace=True, errors="ignore")

# ---------------------------------------------------------------------
# 3. DATE FEATURES
# ---------------------------------------------------------------------
df["Visit Date"] = pd.to_datetime(df["Visit Date"], errors="coerce")
df["hour"] = df["Visit Date"].dt.hour
df["day"] = df["Visit Date"].dt.dayofweek
df["month"] = df["Visit Date"].dt.month
df.drop(columns=["Visit Date"], inplace=True)

# ---------------------------------------------------------------------
# 4. FEATURE ENGINEERING
# ---------------------------------------------------------------------
urgency_map = {"Low": 1, "Medium": 2, "High": 3}
df["Urgency Score"] = df["Urgency Level"].map(urgency_map)
df.drop(columns=["Urgency Level"], inplace=True)

df["Load_Index"] = df["Facility Size (Beds)"] / (df["Nurse-to-Patient Ratio"] + 1)
df["is_peak"] = df["hour"].apply(lambda x: 1 if 17 <= x <= 22 else 0)

# ---------------------------------------------------------------------
# 5. HANDLE MISSING VALUES
# ---------------------------------------------------------------------
df = df.dropna(subset=["wait_time"])

num_cols = df.select_dtypes(include=["int64", "float64"]).columns
df[num_cols] = df[num_cols].fillna(df[num_cols].median())

cat_cols = df.select_dtypes(include=["object"]).columns
for col in cat_cols:
    df[col] = df[col].fillna(df[col].mode()[0])

# ---------------------------------------------------------------------
# 6. ENCODE CATEGORICAL FEATURES
# ---------------------------------------------------------------------
df = pd.get_dummies(df, drop_first=True)

# ---------------------------------------------------------------------
# 7. CREATE CLASS LABELS
# ---------------------------------------------------------------------
def classify_wait(time):
    if time < 12:
        return "Low"
    elif time < 28:
        return "Medium"
    else:
        return "High"

X = df.drop(columns=["wait_time"])

y_text = df["wait_time"].apply(classify_wait)

label_map = {"Low": 0, "Medium": 1, "High": 2}
reverse_label_map = {0: "Low", 1: "Medium", 2: "High"}

y = y_text.map(label_map)

print("\nDataset summary")
print(f"Samples  : {len(X):,}")
print(f"Features : {X.shape[1]}")

print("\nClass distribution:")
print(y_text.value_counts(normalize=True).mul(100).round(1))

# ---------------------------------------------------------------------
# 8. TRAIN / TEST SPLIT
# ---------------------------------------------------------------------
X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.2,
    random_state=42,
    stratify=y
)

y_train_text = y_train.map(reverse_label_map)
y_test_text = y_test.map(reverse_label_map)

print("\nTraining class distribution:")
print(y_train_text.value_counts())

# ---------------------------------------------------------------------
# 9. SAMPLE WEIGHTS
# ---------------------------------------------------------------------
class_counts = y_train.value_counts().to_dict()
total_samples = len(y_train)

class_weight_map = {
    cls: total_samples / (len(class_counts) * count)
    for cls, count in class_counts.items()
}
sample_weights = y_train.map(class_weight_map)

# ---------------------------------------------------------------------
# 10. MODELS
# ---------------------------------------------------------------------
models = {
    "Logistic Regression": Pipeline([
        ("scaler", StandardScaler()),
        ("model", LogisticRegression(
            max_iter=3000,
            multi_class="multinomial",
            solver="lbfgs",
            class_weight="balanced",
            random_state=42
        ))
    ]),
    "SVC": Pipeline([
        ("scaler", StandardScaler()),
        ("model", SVC(
            kernel="rbf",
            C=10,
            gamma="scale",
            class_weight="balanced"
        ))
    ]),
    "Random Forest": RandomForestClassifier(
        n_estimators=300,
        random_state=42,
        n_jobs=-1,
        class_weight="balanced"
    ),
    "Gradient Boosting": GradientBoostingClassifier(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=3,
        random_state=42
    ),
    "XGBoost": XGBClassifier(
        n_estimators=300,
        learning_rate=0.05,
        max_depth=5,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        eval_metric="mlogloss",
        objective="multi:softprob",
        num_class=3,
        verbosity=0
    ),
}

# ---------------------------------------------------------------------
# 11. TRAIN + EVALUATE
# ---------------------------------------------------------------------
results = []
xgb_preds = None

for name, model in models.items():
    print(f"\nTraining {name}...")

    if name in ["Random Forest", "Gradient Boosting", "XGBoost"]:
        model.fit(X_train, y_train, sample_weight=sample_weights)
    else:
        model.fit(X_train, y_train)

    preds = model.predict(X_test)

    acc = accuracy_score(y_test, preds)
    precision = precision_score(y_test, preds, average="macro", zero_division=0)
    recall = recall_score(y_test, preds, average="macro", zero_division=0)
    f1 = f1_score(y_test, preds, average="macro", zero_division=0)

    results.append({
        "Model": name,
        "Accuracy": round(acc, 4),
        "Precision (Macro)": round(precision, 4),
        "Recall (Macro)": round(recall, 4),
        "F1 (Macro)": round(f1, 4),
    })

    if name == "XGBoost":
        xgb_preds = preds

results_df = pd.DataFrame(results).sort_values(
    by=["F1 (Macro)", "Accuracy"],
    ascending=[False, False]
)

print("\n================ CLASSIFICATION MODEL COMPARISON ================\n")
print(results_df.to_string(index=False))

results_df.to_csv("classification_model_comparison.csv", index=False)

# ---------------------------------------------------------------------
# 12. DETAILED REPORT FOR XGBOOST
# ---------------------------------------------------------------------
print("\n================ XGBOOST CLASSIFICATION REPORT ================\n")
report = classification_report(
    y_test,
    xgb_preds,
    labels=[0, 1, 2],
    target_names=["Low", "Medium", "High"],
    zero_division=0
)
print(report)

with open("classification_report.txt", "w", encoding="utf-8") as f:
    f.write(report)

# ---------------------------------------------------------------------
# 13. SAVE MODEL
# ---------------------------------------------------------------------
joblib.dump(models["XGBoost"], "final_xgboost_classifier.pkl")
joblib.dump(X.columns.tolist(), "classification_model_columns.pkl")

print("\nSaved files:")
print(" - classification_model_comparison.csv")
print(" - classification_report.txt")
print(" - final_xgboost_classifier.pkl")
print(" - classification_model_columns.pkl")