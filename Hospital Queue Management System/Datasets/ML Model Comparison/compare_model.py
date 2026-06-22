"""
Model comparison for ER Wait Time Prediction

Compares:
- Linear Regression
- SVR (SVM Regression)
- Random Forest Regressor
- Gradient Boosting Regressor
- XGBoost Regressor
- Logistic Regression (classification baseline for Low/Medium/High wait time)

Outputs:
- regression_model_comparison.csv
- classification_model_comparison.csv
- final_xgboost_balanced.pkl
- model_columns.pkl
"""

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import joblib

from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    r2_score,
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    classification_report,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.svm import SVR
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor

from xgboost import XGBRegressor

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

# Remove leakage columns
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
# 7. FEATURES & TARGET
# ---------------------------------------------------------------------
X = df.drop(columns=["wait_time"])
y = df["wait_time"]

print("\nDataset summary")
print(f"Samples  : {len(X):,}")
print(f"Features : {X.shape[1]}")

# ---------------------------------------------------------------------
# 8. TRAIN / TEST SPLIT
# ---------------------------------------------------------------------
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)

print(f"Train: {len(X_train):,} | Test: {len(X_test):,}")

# ---------------------------------------------------------------------
# 9. WAIT TIME CLASS LABELS FOR LOGISTIC REGRESSION
# ---------------------------------------------------------------------
def classify_wait(time):
    if time < 12:
        return "Low"
    elif time < 28:
        return "Medium"
    else:
        return "High"

y_train_class = y_train.apply(classify_wait)
y_test_class = y_test.apply(classify_wait)

# Sample weights to reduce bias toward High wait times
weight_map = {"Low": 2.0, "Medium": 1.5, "High": 1.0}
sample_weights = y_train_class.map(weight_map)

# ---------------------------------------------------------------------
# 10. MODELS FOR REGRESSION COMPARISON
# ---------------------------------------------------------------------
models = {
    "Linear Regression": Pipeline([
        ("scaler", StandardScaler()),
        ("model", LinearRegression())
    ]),
    "SVR": Pipeline([
        ("scaler", StandardScaler()),
        ("model", SVR(kernel="rbf", C=10, gamma="scale", epsilon=0.1))
    ]),
    "Random Forest": RandomForestRegressor(
        n_estimators=300,
        random_state=42,
        n_jobs=-1
    ),
    "Gradient Boosting": GradientBoostingRegressor(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=3,
        random_state=42
    ),
    "XGBoost": XGBRegressor(
        n_estimators=300,
        learning_rate=0.05,
        max_depth=5,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        eval_metric="mae",
        verbosity=0
    ),
}

# ---------------------------------------------------------------------
# 11. TRAIN + EVALUATE REGRESSION MODELS
# ---------------------------------------------------------------------
regression_results = []

for name, model in models.items():
    print(f"\nTraining {name}...")

    if name == "XGBoost":
        # Direct sample_weight because XGBRegressor is not inside Pipeline
        model.fit(X_train, y_train, sample_weight=sample_weights)
    else:
        model.fit(X_train, y_train)

    preds = model.predict(X_test)

    mae = mean_absolute_error(y_test, preds)
    rmse = np.sqrt(mean_squared_error(y_test, preds))
    r2 = r2_score(y_test, preds)

    regression_results.append({
        "Model": name,
        "MAE": round(mae, 3),
        "RMSE": round(rmse, 3),
        "R2": round(r2, 4),
    })

regression_results_df = pd.DataFrame(regression_results).sort_values(
    by=["R2", "MAE"], ascending=[False, True]
)

print("\n================ REGRESSION MODEL COMPARISON ================")
print(regression_results_df.to_string(index=False))

regression_results_df.to_csv("regression_model_comparison.csv", index=False)

# ---------------------------------------------------------------------
# 12. LOGISTIC REGRESSION BASELINE FOR CLASSIFICATION
# ---------------------------------------------------------------------
print("\nTraining Logistic Regression classification baseline...")

logistic_model = Pipeline([
    ("scaler", StandardScaler()),
    ("model", LogisticRegression(
        max_iter=3000,
        multi_class="multinomial",
        solver="lbfgs"
    ))
])

logistic_model.fit(X_train, y_train_class)
class_preds = logistic_model.predict(X_test)

classification_results = pd.DataFrame([{
    "Model": "Logistic Regression",
    "Accuracy": round(accuracy_score(y_test_class, class_preds), 4),
    "Precision (Macro)": round(precision_score(y_test_class, class_preds, average="macro"), 4),
    "Recall (Macro)": round(recall_score(y_test_class, class_preds, average="macro"), 4),
    "F1 (Macro)": round(f1_score(y_test_class, class_preds, average="macro"), 4),
}])

print("\n================ CLASSIFICATION BASELINE ================")
print(classification_results.to_string(index=False))

print("\nClassification report:")
print(classification_report(y_test_class, class_preds))

classification_results.to_csv("classification_model_comparison.csv", index=False)

# ---------------------------------------------------------------------
# 13. SAVE BEST MODEL (XGBOOST)
# ---------------------------------------------------------------------
best_model = models["XGBoost"]
joblib.dump(best_model, "final_xgboost_balanced.pkl")
joblib.dump(X.columns.tolist(), "model_columns.pkl")

print("\nSaved files:")
print(" - regression_model_comparison.csv")
print(" - classification_model_comparison.csv")
print(" - final_xgboost_balanced.pkl")
print(" - model_columns.pkl")
print("\nXGBoost model saved successfully.")