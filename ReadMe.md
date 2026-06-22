# 🏥 Hospital Queue Management System

A full-stack hospital queue management system with AI-powered wait time prediction, load forecasting, overload alerts - built with Flask (Python backend) + HTML/CSS/JS (frontend).

---

## 📁 Project Structure

```
hospital-queue-system/
│
├── hospital_queue_system.html   ← Full frontend (single-file)
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

## 🎨 Design Notes

- **Color Theme**: Pale green (#f0faf4) + white — calm, clinical aesthetic
- **Fonts**: Syne (display / headings) + DM Sans (body text)
- **Sidebar**: Dark (#0f172a) with green accent highlights
- **Charts**: Pure SVG — no chart library dependency
- **Single-file frontend**: The entire UI is `hospital_queue_system.html` — easy to deploy anywhere

---

