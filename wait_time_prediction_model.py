"""
wait_time_prediction_model.py
=============================
Trains the XGBoost wait-time regression model on the
"ER Wait Time Dataset.csv" and saves it as
"final_xgboost_balanced.pkl".

Run:
    python wait_time_prediction_model.py
"""

import pandas as pd
import numpy as np
import warnings
import joblib

from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    r2_score,
    confusion_matrix,
    ConfusionMatrixDisplay,
    classification_report,
)
from xgboost import XGBRegressor
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

# -----------------------------------------------------------------------
# 1. LOAD DATA
# -----------------------------------------------------------------------
print("📂 Loading dataset...")
df = pd.read_csv("ER Wait Time Dataset.csv")
print(f"   Rows: {len(df):,}  Columns: {len(df.columns)}")

# -----------------------------------------------------------------------
# 2. CLEANING
# -----------------------------------------------------------------------
df = df.drop_duplicates()
df.rename(columns={"Total Wait Time (min)": "wait_time"}, inplace=True)

# -----------------------------------------------------------------------
# 3. REMOVE DATA LEAKAGE
# -----------------------------------------------------------------------
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
print(f"   After leakage drop: {len(df.columns)} columns remain")

# -----------------------------------------------------------------------
# 4. DATE FEATURES
# -----------------------------------------------------------------------
df["Visit Date"] = pd.to_datetime(df["Visit Date"], errors="coerce")
df["hour"]  = df["Visit Date"].dt.hour
df["day"]   = df["Visit Date"].dt.dayofweek
df["month"] = df["Visit Date"].dt.month
df.drop(columns=["Visit Date"], inplace=True)

# -----------------------------------------------------------------------
# 5. FEATURE ENGINEERING
# -----------------------------------------------------------------------
urgency_map = {"Low": 1, "Medium": 2, "High": 3}
df["Urgency Score"] = df["Urgency Level"].map(urgency_map)
df.drop(columns=["Urgency Level"], inplace=True)

df["Load_Index"] = df["Facility Size (Beds)"] / (df["Nurse-to-Patient Ratio"] + 1)
df["is_peak"]    = df["hour"].apply(lambda x: 1 if 17 <= x <= 22 else 0)

# -----------------------------------------------------------------------
# 6. HANDLE MISSING VALUES
# -----------------------------------------------------------------------
df = df.dropna(subset=["wait_time"])

num_cols = df.select_dtypes(include=["int64", "float64"]).columns
df[num_cols] = df[num_cols].fillna(df[num_cols].median())

cat_cols = df.select_dtypes(include=["object"]).columns
for col in cat_cols:
    df[col] = df[col].fillna(df[col].mode()[0])

# -----------------------------------------------------------------------
# 7. ENCODE CATEGORICAL
# -----------------------------------------------------------------------
df = pd.get_dummies(df, drop_first=True)

# -----------------------------------------------------------------------
# 8. FEATURES & TARGET
# -----------------------------------------------------------------------
X = df.drop(columns=["wait_time"])
y = df["wait_time"]

print(f"\n📊 Dataset summary")
print(f"   Samples : {len(X):,}")
print(f"   Features: {X.shape[1]}")
print(f"   Target  : wait_time  mean={y.mean():.1f}  std={y.std():.1f}  "
      f"min={y.min():.0f}  max={y.max():.0f}")

# -----------------------------------------------------------------------
# 9. TRAIN / TEST SPLIT
# -----------------------------------------------------------------------
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)
print(f"\n🔀 Train: {len(X_train):,}  |  Test: {len(X_test):,}")

# -----------------------------------------------------------------------
# 10. CLASSIFICATION LABELS (for sample weighting)
# -----------------------------------------------------------------------
def classify_wait_train(time):
    if time < 15:
        return "Low"
    elif time < 30:
        return "Medium"
    else:
        return "High"

y_train_class = y_train.apply(classify_wait_train)
dist = y_train_class.value_counts(normalize=True) * 100
print(f"\n📈 Class distribution in train set:")
for cls, pct in dist.items():
    print(f"   {cls:8s}: {pct:.1f}%")

# Boost Low & Medium to reduce bias toward High
weight_map     = {"Low": 2.0, "Medium": 1.5, "High": 1.0}
sample_weights = y_train_class.map(weight_map)

# -----------------------------------------------------------------------
# 11. TRAIN MODEL
# -----------------------------------------------------------------------
print("\n🚀 Training XGBoost model...")
model = XGBRegressor(
    n_estimators    = 300,
    learning_rate   = 0.05,
    max_depth       = 5,
    subsample       = 0.8,
    colsample_bytree= 0.8,
    random_state    = 42,
    eval_metric     = "mae",
    verbosity       = 0,
)
model.fit(
    X_train, y_train,
    sample_weight   = sample_weights,
    eval_set        = [(X_test, y_test)],
    verbose         = False,
)

# -----------------------------------------------------------------------
# 12. PREDICTIONS
# -----------------------------------------------------------------------
y_pred = model.predict(X_test)

# -----------------------------------------------------------------------
# 13. REGRESSION EVALUATION
# -----------------------------------------------------------------------
mae  = mean_absolute_error(y_test, y_pred)
rmse = np.sqrt(mean_squared_error(y_test, y_pred))
r2   = r2_score(y_test, y_pred)

print("\n" + "=" * 45)
print("📊  REGRESSION PERFORMANCE")
print("=" * 45)
print(f"  MAE  : {mae:.2f} min")
print(f"  RMSE : {rmse:.2f} min")
print(f"  R²   : {r2:.3f}")
print("=" * 45)

# -----------------------------------------------------------------------
# 14. IMPROVED CLASSIFICATION THRESHOLDS
# -----------------------------------------------------------------------
def classify_wait_improved(time):
    if time < 12:
        return "Low"
    elif time < 28:
        return "Medium"
    else:
        return "High"

y_test_class = [classify_wait_improved(x) for x in y_test]
y_pred_class = [classify_wait_improved(x) for x in y_pred]

# -----------------------------------------------------------------------
# 15. CONFUSION MATRIX
# -----------------------------------------------------------------------
print("\n📊 CLASSIFICATION REPORT (improved thresholds):")
print(classification_report(y_test_class, y_pred_class))

cm   = confusion_matrix(y_test_class, y_pred_class, labels=["Low", "Medium", "High"])
disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=["Low", "Medium", "High"])
disp.plot(cmap="Blues")
plt.title("Confusion Matrix (Improved Thresholds)")
plt.tight_layout()
plt.savefig("confusion_matrix.png", dpi=150, bbox_inches="tight")
plt.show()
print("   Saved → confusion_matrix.png")

# -----------------------------------------------------------------------
# 16. FEATURE IMPORTANCE
# -----------------------------------------------------------------------
importance_df = (
    pd.DataFrame({
        "Feature"   : X.columns,
        "Importance": model.feature_importances_,
    })
    .sort_values("Importance", ascending=False)
    .reset_index(drop=True)
)

print("\n🔑 Top 10 Features:")
print(importance_df.head(10).to_string(index=False))

# Plot feature importance
fig, ax = plt.subplots(figsize=(10, 6))
top_n = importance_df.head(12)
ax.barh(top_n["Feature"][::-1], top_n["Importance"][::-1], color="#2da866")
ax.set_xlabel("Feature Importance")
ax.set_title("XGBoost Feature Importance — Wait Time Model")
plt.tight_layout()
plt.savefig("feature_importance.png", dpi=150, bbox_inches="tight")
plt.show()
print("   Saved → feature_importance.png")

# -----------------------------------------------------------------------
# 17. ACTUAL VS PREDICTED SCATTER
# -----------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7, 7))
ax.scatter(y_test, y_pred, alpha=0.3, s=10, color="#2da866")
lims = [min(y_test.min(), y_pred.min()), max(y_test.max(), y_pred.max())]
ax.plot(lims, lims, "r--", linewidth=1.5, label="Perfect prediction")
ax.set_xlabel("Actual Wait Time (min)")
ax.set_ylabel("Predicted Wait Time (min)")
ax.set_title(f"Actual vs Predicted Wait Time  (MAE={mae:.2f}, R²={r2:.3f})")
ax.legend()
plt.tight_layout()
plt.savefig("actual_vs_predicted.png", dpi=150, bbox_inches="tight")
plt.show()
print("   Saved → actual_vs_predicted.png")

# -----------------------------------------------------------------------
# 18. SAVE MODEL
# -----------------------------------------------------------------------
joblib.dump(model, "final_xgboost_balanced.pkl")
joblib.dump(X.columns.tolist(), "model_columns.pkl")
print("\n✅ Model saved → final_xgboost_balanced.pkl")
print(f"   Features in model : {len(model.feature_names_in_)}")