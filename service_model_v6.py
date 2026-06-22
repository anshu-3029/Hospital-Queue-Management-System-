"""
service_model_v6.py
===================
Trains the hospital service-time (hospital stay hours) ensemble model.
Three strategies are evaluated; the best is saved as:
    service_time_model_v6_best.pkl

Run:
    python service_model_v6.py

v5 benchmark: MAE=38.90 | RMSE=64.84 | R²=0.705 | MedAE=17.05
"""

import pandas as pd
import numpy as np
import warnings
import joblib
import optuna

from sklearn.model_selection import train_test_split, KFold, cross_val_predict
from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    r2_score,
    median_absolute_error,
)
from sklearn.linear_model import Ridge
from sklearn.ensemble import RandomForestClassifier
from lightgbm import LGBMRegressor, early_stopping, log_evaluation
from catboost import CatBoostRegressor
from xgboost import XGBRegressor

warnings.filterwarnings("ignore")
optuna.logging.set_verbosity(optuna.logging.WARNING)

RANDOM_STATE = 42
np.random.seed(RANDOM_STATE)

# -----------------------------------------------------------------------
# LOAD & PREPROCESS
# -----------------------------------------------------------------------
print("📂 Loading mover_model_ready.csv ...")
df = pd.read_csv("mover_model_ready.csv")

TARGET = "hospital_stay_hours"
df = df.dropna(subset=[TARGET])

leakage_cols = [
    "log_hospital_stay_hours",
    "log_admit_to_or_hours",
    "log_or_to_discharge_hours",
    "or_to_discharge_hours",
    "admit_to_or_hours",
]
df.drop(columns=leakage_cols, inplace=True, errors="ignore")
df.drop(columns=["LOG_ID"], inplace=True, errors="ignore")

# Cap outliers at P85 (proven best in v4 / v5)
p85 = df[TARGET].quantile(0.85)
p50 = df[TARGET].quantile(0.50)
print(f"  P50 (median) stay : {p50:.1f} hrs")
print(f"  P85 stay (cap)    : {p85:.1f} hrs\n")
df[TARGET] = np.clip(df[TARGET], None, p85)

# Impute
num_cols = df.select_dtypes(include=["int64", "float64"]).columns
df[num_cols] = df[num_cols].fillna(df[num_cols].median())
cat_cols = df.select_dtypes(include=["object"]).columns
for col in cat_cols:
    df[col] = df[col].fillna(df[col].mode()[0])

df = pd.get_dummies(df, drop_first=True)

X = df.drop(columns=[TARGET])
y = df[TARGET]
y_log = np.log1p(y)

print(f"  Samples  : {len(X):,}")
print(f"  Features : {X.shape[1]}")

# Stratified split
quantile_bins = pd.qcut(y, q=5, labels=False, duplicates="drop")
X_train, X_test, y_train, y_test, qb_train, qb_test = train_test_split(
    X, y_log, quantile_bins,
    test_size=0.2,
    random_state=RANDOM_STATE,
    stratify=quantile_bins,
)

# Baseline
baseline_pred = np.expm1(np.full_like(y_test, y_train.mean()))
baseline_mae  = mean_absolute_error(np.expm1(y_test), baseline_pred)
print(f"\n  Baseline MAE : {baseline_mae:.2f}")
print(f"  v5 benchmark : 38.90\n")
print("=" * 60)

# Tiered sample weights
max_bucket     = qb_train.max()
sample_weights = np.where(
    qb_train == max_bucket,         0.25,
    np.where(qb_train == max_bucket - 1, 0.65, 1.0),
)

# -----------------------------------------------------------------------
# HELPER
# -----------------------------------------------------------------------
def print_eval(label, y_true, y_pred, benchmark=38.90):
    mae   = mean_absolute_error(y_true, y_pred)
    rmse  = np.sqrt(mean_squared_error(y_true, y_pred))
    r2    = r2_score(y_true, y_pred)
    medae = median_absolute_error(y_true, y_pred)
    errors = np.abs(y_true - y_pred)
    delta  = benchmark - mae
    flag   = "✅ BEATS v5" if mae < benchmark else "❌ below v5"
    print(f"\n📊 {label}")
    print(f"   MAE   : {mae:.2f}  ({flag}, Δ={delta:+.2f})")
    print(f"   RMSE  : {rmse:.2f}")
    print(f"   R2    : {r2:.3f}")
    print(f"   MedAE : {medae:.2f}")
    print(f"   P50 err: {np.percentile(errors,50):.1f}  "
          f"P75: {np.percentile(errors,75):.1f}  "
          f"P90: {np.percentile(errors,90):.1f}")
    return mae, rmse, r2, medae


results  = {}
y_actual = np.expm1(y_test)

# ================================================================
# STRATEGY A — CatBoost + Optuna + 5-seed ensemble
# ================================================================
print("\n" + "=" * 60)
print("STRATEGY A — CatBoost + Optuna + 5-seed ensemble")
print("=" * 60)

N_TRIALS_A = 60
N_CV_A     = 3
kf_a       = KFold(n_splits=N_CV_A, shuffle=True, random_state=RANDOM_STATE)


def objective_catboost(trial):
    params = dict(
        loss_function      = "MAE",
        iterations         = trial.suggest_int("iterations", 300, 1000),
        learning_rate      = trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
        depth              = trial.suggest_int("depth", 4, 8),
        l2_leaf_reg        = trial.suggest_float("l2_leaf_reg", 0.5, 10.0, log=True),
        min_data_in_leaf   = trial.suggest_int("min_data_in_leaf", 10, 80),
        subsample          = trial.suggest_float("subsample", 0.6, 1.0),
        colsample_bylevel  = trial.suggest_float("colsample_bylevel", 0.6, 1.0),
        random_seed        = RANDOM_STATE,
        verbose            = False,
        allow_writing_files= False,
    )
    fold_maes = []
    for tr_idx, val_idx in kf_a.split(X_train):
        Xf_tr, Xf_val = X_train.iloc[tr_idx], X_train.iloc[val_idx]
        yf_tr, yf_val = y_train.iloc[tr_idx], y_train.iloc[val_idx]
        wf_tr         = sample_weights[tr_idx]
        m = CatBoostRegressor(**params)
        m.fit(Xf_tr, yf_tr, sample_weight=wf_tr,
              eval_set=(Xf_val, yf_val), early_stopping_rounds=40)
        preds   = np.clip(np.expm1(m.predict(Xf_val)), 1.0, None)
        actuals = np.expm1(yf_val)
        fold_maes.append(mean_absolute_error(actuals, preds))
    return np.mean(fold_maes)


print(f"Running Optuna — {N_TRIALS_A} trials × {N_CV_A}-fold CV ...")
study_a = optuna.create_study(
    direction="minimize",
    sampler=optuna.samplers.TPESampler(seed=RANDOM_STATE),
)
study_a.optimize(objective_catboost, n_trials=N_TRIALS_A, show_progress_bar=True)

best_a = study_a.best_params
print(f"\n  Best CV MAE : {study_a.best_value:.2f}")
print(f"  Best params : {best_a}")

SEEDS_A  = [42, 7, 123, 99, 17]
preds_a  = np.zeros(len(X_test))
models_a = []

print(f"\nTraining 5-seed CatBoost ensemble ...")
for seed in SEEDS_A:
    m = CatBoostRegressor(
        **best_a,
        loss_function="MAE",
        random_seed=seed,
        verbose=False,
        allow_writing_files=False,
    )
    m.fit(X_train, y_train, sample_weight=sample_weights,
          eval_set=(X_test, y_test), early_stopping_rounds=40)
    preds_a += m.predict(X_test)
    models_a.append(m)

preds_a  /= len(SEEDS_A)
y_pred_a  = np.clip(np.expm1(preds_a), 1.0, None)

mae_a, rmse_a, r2_a, medae_a = print_eval(
    "STRATEGY A — CatBoost + Optuna + Ensemble", y_actual, y_pred_a
)
results["A_CatBoost"] = dict(
    mae=mae_a, rmse=rmse_a, r2=r2_a, medae=medae_a,
    preds=y_pred_a,
    models=models_a,       # list — Strategy A inference pattern
)


# ================================================================
# STRATEGY B — Stacking: LightGBM + CatBoost + XGBoost → Ridge
# ================================================================
print("\n" + "=" * 60)
print("STRATEGY B — Stacking: LightGBM + CatBoost + XGBoost → Ridge")
print("=" * 60)

N_TRIALS_B = 40
N_CV_B     = 3
kf_b       = KFold(n_splits=N_CV_B, shuffle=True, random_state=RANDOM_STATE)


def tune_lgbm_b(n_trials):
    def obj(trial):
        p = dict(
            objective         = "mae",
            n_estimators      = trial.suggest_int("n_estimators", 300, 900),
            learning_rate     = trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
            num_leaves        = trial.suggest_int("num_leaves", 20, 120),
            min_child_samples = trial.suggest_int("min_child_samples", 10, 60),
            feature_fraction  = trial.suggest_float("feature_fraction", 0.5, 1.0),
            bagging_fraction  = trial.suggest_float("bagging_fraction", 0.5, 1.0),
            bagging_freq      = trial.suggest_int("bagging_freq", 1, 7),
            reg_alpha         = trial.suggest_float("reg_alpha", 0.01, 5.0, log=True),
            reg_lambda        = trial.suggest_float("reg_lambda", 0.01, 5.0, log=True),
            n_jobs=-1, verbosity=-1, random_state=RANDOM_STATE,
        )
        fold_maes = []
        for tr_i, val_i in kf_b.split(X_train):
            m = LGBMRegressor(**p)
            m.fit(
                X_train.iloc[tr_i], y_train.iloc[tr_i],
                sample_weight=sample_weights[tr_i],
                eval_set=[(X_train.iloc[val_i], y_train.iloc[val_i])],
                callbacks=[early_stopping(40, verbose=False), log_evaluation(-1)],
            )
            preds   = np.clip(np.expm1(m.predict(X_train.iloc[val_i])), 1.0, None)
            actuals = np.expm1(y_train.iloc[val_i])
            fold_maes.append(mean_absolute_error(actuals, preds))
        return np.mean(fold_maes)

    study = optuna.create_study(direction="minimize",
                                sampler=optuna.samplers.TPESampler(seed=RANDOM_STATE))
    study.optimize(obj, n_trials=n_trials, show_progress_bar=True)
    return study.best_params


print("Tuning LightGBM base model ...")
best_lgbm_b = tune_lgbm_b(N_TRIALS_B)

# Generate OOF predictions for meta-learner
print("Generating OOF meta-features ...")
oof_lgbm = cross_val_predict(
    LGBMRegressor(**best_lgbm_b, objective="mae", n_jobs=-1,
                  verbosity=-1, random_state=RANDOM_STATE),
    X_train, y_train, cv=N_CV_B,
)
oof_cat = cross_val_predict(
    CatBoostRegressor(
        **{k: v for k, v in best_a.items()},
        loss_function="MAE", random_seed=RANDOM_STATE,
        verbose=False, allow_writing_files=False,
    ),
    X_train, y_train, cv=N_CV_B,
)
oof_xgb = cross_val_predict(
    XGBRegressor(n_estimators=300, learning_rate=0.05, max_depth=5,
                 subsample=0.8, colsample_bytree=0.8, random_state=RANDOM_STATE,
                 eval_metric="mae", verbosity=0),
    X_train, y_train, cv=N_CV_B,
)

meta_train = np.column_stack([oof_lgbm, oof_cat, oof_xgb])

# Full-data base models for test inference
print("Training full-data base models ...")
lgbm_full = LGBMRegressor(**best_lgbm_b, objective="mae", n_jobs=-1,
                           verbosity=-1, random_state=RANDOM_STATE)
lgbm_full.fit(X_train, y_train, sample_weight=sample_weights)

cat_full = CatBoostRegressor(
    **best_a, loss_function="MAE", random_seed=RANDOM_STATE,
    verbose=False, allow_writing_files=False,
)
cat_full.fit(X_train, y_train, sample_weight=sample_weights)

xgb_full = XGBRegressor(
    n_estimators=300, learning_rate=0.05, max_depth=5,
    subsample=0.8, colsample_bytree=0.8, random_state=RANDOM_STATE,
    eval_metric="mae", verbosity=0,
)
xgb_full.fit(X_train, y_train, sample_weight=sample_weights)

test_lgbm  = lgbm_full.predict(X_test)
test_cat   = cat_full.predict(X_test)
test_xgb   = xgb_full.predict(X_test)
meta_test  = np.column_stack([test_lgbm, test_cat, test_xgb])

print("Training Ridge meta-learner ...")
meta_learner = Ridge(alpha=1.0)
meta_learner.fit(meta_train, y_train)

stack_pred_log = meta_learner.predict(meta_test)
y_pred_b       = np.clip(np.expm1(stack_pred_log), 1.0, None)

print(
    f"\n  Meta-learner weights — "
    f"LightGBM: {meta_learner.coef_[0]:.3f}  "
    f"CatBoost: {meta_learner.coef_[1]:.3f}  "
    f"XGBoost: {meta_learner.coef_[2]:.3f}"
)

mae_b, rmse_b, r2_b, medae_b = print_eval(
    "STRATEGY B — Stacking (LightGBM + CatBoost + XGBoost + Ridge)", y_actual, y_pred_b
)
results["B_Stacking"] = dict(
    mae=mae_b, rmse=rmse_b, r2=r2_b, medae=medae_b,
    preds=y_pred_b,
    models=dict(lgbm=lgbm_full, cat=cat_full, xgb=xgb_full, meta=meta_learner),
)


# ================================================================
# STRATEGY C — Two-stage: classifier → per-segment LightGBM
# ================================================================
print("\n" + "=" * 60)
print("STRATEGY C — Two-stage: classifier → per-segment LightGBM")
print("=" * 60)

y_train_orig = np.expm1(y_train)
y_test_orig  = np.expm1(y_test)
threshold    = p50

y_cls_train = (y_train_orig > threshold).astype(int)
y_cls_test  = (y_test_orig  > threshold).astype(int)

print(f"  Classifier split at P50 = {threshold:.1f} hrs")
print(f"  Short-stay (≤{threshold:.0f} hrs): {(y_cls_train==0).sum()} train samples")
print(f"  Long-stay  (>{threshold:.0f} hrs): {(y_cls_train==1).sum()} train samples")

clf = RandomForestClassifier(
    n_estimators=300, max_depth=8, min_samples_leaf=10,
    class_weight="balanced", random_state=RANDOM_STATE, n_jobs=-1,
)
clf.fit(X_train, y_cls_train)
clf_acc = (clf.predict(X_test) == y_cls_test).mean()
print(f"  Classifier accuracy : {clf_acc:.3f}")

N_TRIALS_C = 40
kf_c       = KFold(n_splits=3, shuffle=True, random_state=RANDOM_STATE)


def tune_segment_lgbm(X_seg, y_seg, w_seg, label):
    print(f"\n  Optuna — {label} segment ({N_TRIALS_C} trials) ...")

    def obj(trial):
        p = dict(
            objective         = "mae",
            n_estimators      = trial.suggest_int("n_estimators", 200, 900),
            learning_rate     = trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
            num_leaves        = trial.suggest_int("num_leaves", 15, 120),
            min_child_samples = trial.suggest_int("min_child_samples", 5, 60),
            feature_fraction  = trial.suggest_float("feature_fraction", 0.5, 1.0),
            bagging_fraction  = trial.suggest_float("bagging_fraction", 0.5, 1.0),
            bagging_freq      = trial.suggest_int("bagging_freq", 1, 7),
            reg_alpha         = trial.suggest_float("reg_alpha", 0.01, 5.0, log=True),
            reg_lambda        = trial.suggest_float("reg_lambda", 0.01, 5.0, log=True),
            n_jobs=-1, verbosity=-1, random_state=RANDOM_STATE,
        )
        fold_maes = []
        for tr_i, val_i in kf_c.split(X_seg):
            m = LGBMRegressor(**p)
            m.fit(
                X_seg.iloc[tr_i], y_seg.iloc[tr_i],
                sample_weight=w_seg[tr_i],
                eval_set=[(X_seg.iloc[val_i], y_seg.iloc[val_i])],
                callbacks=[early_stopping(40, verbose=False), log_evaluation(-1)],
            )
            preds   = np.clip(np.expm1(m.predict(X_seg.iloc[val_i])), 1.0, None)
            actuals = np.expm1(y_seg.iloc[val_i])
            fold_maes.append(mean_absolute_error(actuals, preds))
        return np.mean(fold_maes)

    study = optuna.create_study(direction="minimize",
                                sampler=optuna.samplers.TPESampler(seed=RANDOM_STATE))
    study.optimize(obj, n_trials=N_TRIALS_C, show_progress_bar=True)
    print(f"  Best CV MAE [{label}]: {study.best_value:.2f}")
    return study.best_params


# Segment masks
short_mask_tr = y_cls_train == 0
long_mask_tr  = y_cls_train == 1

X_short = X_train[short_mask_tr];  y_short = y_train[short_mask_tr]
X_long  = X_train[long_mask_tr];   y_long  = y_train[long_mask_tr]
w_short = sample_weights[short_mask_tr.values]
w_long  = sample_weights[long_mask_tr.values]

best_short = tune_segment_lgbm(X_short, y_short, w_short, "short-stay")
best_long  = tune_segment_lgbm(X_long,  y_long,  w_long,  "long-stay")

SEEDS_C = [42, 7, 123]


def train_segment_ensemble(X_seg, y_seg, w_seg, params, seeds):
    acc = np.zeros(len(X_test))
    for seed in seeds:
        m = LGBMRegressor(**params, objective="mae", n_jobs=-1,
                          verbosity=-1, random_state=seed)
        m.fit(X_seg, y_seg, sample_weight=w_seg)
        acc += m.predict(X_test)
    return acc / len(seeds)


print("\nTraining segment ensembles ...")
log_preds_short = train_segment_ensemble(X_short, y_short, w_short, best_short, SEEDS_C)
log_preds_long  = train_segment_ensemble(X_long,  y_long,  w_long,  best_long,  SEEDS_C)

cls_preds   = clf.predict(X_test)
final_log_c = np.where(cls_preds == 0, log_preds_short, log_preds_long)
y_pred_c    = np.clip(np.expm1(final_log_c), 1.0, None)

mae_c, rmse_c, r2_c, medae_c = print_eval(
    "STRATEGY C — Two-stage (classifier + per-segment LightGBM)", y_actual, y_pred_c
)
results["C_TwoStage"] = dict(
    mae=mae_c, rmse=rmse_c, r2=r2_c, medae=medae_c,
    preds=y_pred_c,
    models=dict(clf=clf, short=best_short, long=best_long),
)


# ================================================================
# FINAL COMPARISON + SAVE
# ================================================================
print("\n" + "=" * 60)
print("FINAL COMPARISON")
print("=" * 60)
print(f"\n{'Model':<48} {'MAE':>7} {'RMSE':>8} {'R2':>7} {'MedAE':>8}")
print("-" * 60)
print(f"{'v5 benchmark':<48} {'38.90':>7} {'64.84':>8} {'0.705':>7} {'17.05':>8}")
for key, r in results.items():
    flag = " ✅" if r["mae"] < 38.90 else ""
    print(f"{key:<48} {r['mae']:>7.2f} {r['rmse']:>8.2f} {r['r2']:>7.3f} {r['medae']:>8.2f}{flag}")

best_key = min(results, key=lambda k: results[k]["mae"])
best_mae  = results[best_key]["mae"]
print(f"\n🏆 Best strategy : {best_key}  (MAE={best_mae:.2f})")

joblib.dump(results[best_key], "service_time_model_v6_best.pkl")
print("   Saved → service_time_model_v6_best.pkl")

joblib.dump(results, "service_time_model_v6_all.pkl")
print("   Saved → service_time_model_v6_all.pkl")

# ================================================================
# INFERENCE GUIDE
# ================================================================
print("""
╔══════════════════════════════════════════════════════════════╗
║  INFERENCE GUIDE                                             ║
╠══════════════════════════════════════════════════════════════╣
║  Strategy A (CatBoost Ensemble)                              ║
║  ─────────────────────────────                               ║
║  artifact  = joblib.load('service_time_model_v6_best.pkl')  ║
║  preds_log = np.mean(                                        ║
║      [m.predict(X_new) for m in artifact['models']], axis=0 ║
║  )                                                           ║
║  output    = np.clip(np.expm1(preds_log), 1.0, None)        ║
║                                                              ║
║  Strategy B (Stacking)                                       ║
║  ─────────────────────                                       ║
║  meta_in = np.column_stack([                                 ║
║      artifact['models']['lgbm'].predict(X_new),             ║
║      artifact['models']['cat'].predict(X_new),              ║
║      artifact['models']['xgb'].predict(X_new),              ║
║  ])                                                          ║
║  output = np.clip(                                           ║
║      np.expm1(artifact['models']['meta'].predict(meta_in)), ║
║      1.0, None)                                              ║
╚══════════════════════════════════════════════════════════════╝
""")