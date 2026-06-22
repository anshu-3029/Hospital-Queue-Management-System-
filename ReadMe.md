# 🏥 Smart Hospital Queue & Resource Load Prediction System

A full-stack hospital queue management system with AI-powered wait time prediction, load forecasting, overload alerts, and staff reallocation — built with Flask (Python backend) + vanilla HTML/CSS/JS (frontend).

---

## 📁 Project Structure

```
hospital-queue-system/
│
├── hospital_queue_system.html   ← Full frontend (single-file SPA)
├── app.py                       ← Flask REST API backend
├── requirements.txt             ← Python dependencies
│
├── wait_time_prediction_model.py  ← Train XGBoost wait-time model
├── service_model_v6.py            ← Train service-time ensemble model
│
├── ER Wait Time Dataset.csv       ← Dataset for wait-time model
├── mover_model_ready.csv          ← Dataset for service-time model
│
├── final_xgboost_balanced.pkl     ← Trained wait-time model (auto-generated)
├── service_time_model_v6_best.pkl ← Trained service-time model (auto-generated)
│
└── README.md
```

---

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Train the Models

> **Skip this step if you already have the `.pkl` files.**

```bash
# Train wait-time model (uses ER Wait Time Dataset.csv)
python wait_time_prediction_model.py

# Train service-time model (uses mover_model_ready.csv)
python service_model_v6.py
```

### 3. Start the Flask API

```bash
python app.py
```

API will be running at: `http://127.0.0.1:5000`

### 4. Open the Frontend

Open `hospital_queue_system.html` directly in any browser — **no build step needed**.

Or visit `http://127.0.0.1:5000` if you want the Flask server to serve it.

---

## 🔐 Login Credentials

| Role  | Username | Password |
|-------|----------|----------|
| Admin | `admin`  | `1234`   |
| Staff | `staff`  | `1234`   |

---

## 🧭 Module Overview

### 👨‍💼 Admin Module (Full Access)

| Page | Description |
|------|-------------|
| Dashboard | Live stats, queue overview, donut chart, load forecast chart, alerts, staff suggestions |
| New Appointment | Register a new patient with auto token |
| Token Queue | Full queue list, Now Serving display, department summary |
| Today's Appointments | All appointments with filters |
| Patient Search | Search by name / ID |
| Waiting Time Prediction | Full AI prediction form with all parameters |
| Load Forecasting | 24-hour load chart, department-level breakdowns |
| Overload Alerts | Active + historical alerts |
| Staff Reallocation | AI suggestions with approve/dismiss actions |
| Doctors | All doctors, status, specialization |
| Departments | Department cards with key stats |
| Model Insights | MAE/R²/RMSE, classification report, feature importance bars, explainability logic |
| Schedules | Daily + weekly schedule table |
| Settings | Peak hours, thresholds, notification toggles, account settings |

### 👨‍⚕️ User / Staff Module (Restricted)

| Page | Description |
|------|-------------|
| Dashboard | Queue snapshot, quick actions, Now Serving |
| Predict Wait Time | Simplified prediction form |
| Queue Status | Token queue with search, Now Serving display |
| New Appointment | Register patient + generate token |

---

## 🤖 ML Models

### Wait Time Model (`final_xgboost_balanced.pkl`)

- **Algorithm**: XGBoost Regressor
- **Dataset**: `ER Wait Time Dataset.csv`
- **Target**: `Total Wait Time (min)`
- **Key Features**: Nurse-to-Patient Ratio, Urgency Score, Load Index, is_peak, hour, Facility Size
- **Sample Weighting**: Low×2.0, Medium×1.5, High×1.0 (reduces bias toward high-wait predictions)
- **Performance**: MAE ≈ 4.2 min | R² ≈ 0.87 | RMSE ≈ 5.9

### Service Time Model (`service_time_model_v6_best.pkl`)

- **Algorithm**: Best of 3 strategies (CatBoost Ensemble / Stacking / Two-Stage)
- **Dataset**: `mover_model_ready.csv`
- **Target**: `hospital_stay_hours` (log-transformed)
- **Key Features**: age, asa_score, comorbidity_count, icu_admission, is_inpatient, total_procedure_events, discharge_severity
- **v5 Benchmark**: MAE=38.90 | R²=0.705

#### Strategy Comparison

| Strategy | Description |
|----------|-------------|
| A | CatBoost + Optuna (60 trials) + 5-seed ensemble |
| B | Stacking: LightGBM + CatBoost + XGBoost → Ridge meta-learner |
| C | Two-Stage: RandomForest classifier → per-segment LightGBM (short/long stay) |

---

## 🌐 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Serve frontend HTML |
| GET | `/health` | Health check + model info |
| POST | `/predict` | Single prediction |
| POST | `/predict/batch` | Batch predictions |
| GET | `/queue/summary` | Department queue summary |
| GET | `/alerts` | Active overload alerts |
| GET | `/model/info` | Model metadata |

### `/predict` — Request Body

```json
{
  "nurse_ratio":    3,
  "facility_size":  200,
  "hour":           14,
  "day":            3,
  "month":          4,
  "urgency":        2,
  "age":            40,
  "asa_score":      2,
  "comorbidity_count": 1,
  "icu":            0,
  "inpatient":      1,
  "procedures":     2,
  "severity":       1
}
```

### `/predict` — Response

```json
{
  "predicted_wait_time": 27.5,
  "service_time":        48.3,
  "load_status":         "Medium",
  "reasons": [
    "Moderate urgency patients contributing to delays",
    "Moderate queue buildup in department"
  ],
  "is_peak": false,
  "load_index": 50.0
}
```

---

## 🎨 Design Notes

- **Color Theme**: Pale green (#f0faf4) + white — calm, clinical aesthetic
- **Fonts**: Syne (display / headings) + DM Sans (body text)
- **Sidebar**: Dark (#0f172a) with green accent highlights
- **Charts**: Pure SVG — no chart library dependency
- **Single-file frontend**: The entire UI is `hospital_queue_system.html` — easy to deploy anywhere

---

## 🔧 Production Deployment

```bash
# Linux / macOS — use gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app

# Windows — use waitress
pip install waitress
waitress-serve --port=5000 app:app
```

---

## ⚠️ Important Notes

1. **Models must be trained first** before starting the Flask server (or the `.pkl` files must be present in the same directory as `app.py`).
2. The frontend currently uses **simulated predictions** via JavaScript when running standalone (without the Flask backend). To use real model predictions, connect it to the Flask API by replacing the `runPrediction()` function with a `fetch('/predict', ...)` call.
3. Queue data, doctor lists, and alert data are **mock/demo data** — replace with a real database (e.g. PostgreSQL + SQLAlchemy) for production.

---

## 🎓 Viva Tips

- The **Model Insights** page (Admin) demonstrates MAE, R², confusion matrix, feature importance, and explainability — key for technical evaluation.
- The **two-role system** (Admin vs Staff) shows access control design.
- The **Load Forecasting** and **Staff Reallocation** pages demonstrate proactive resource management beyond simple prediction.
- The **Explanation Layer** in `app.py` (`generate_reason()`) shows rule-based XAI (Explainable AI) on top of ML predictions.

---

*Made with ❤️ for Smart Hospital Queue Management — 2024*