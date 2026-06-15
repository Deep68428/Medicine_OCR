# Picknote Scanner System

A pharmaceutical picknote scanning system for warehouse operations. A camera captures medicine packages, YOLO v8 detects batch label regions, RapidOCR extracts batch numbers, and results are matched against picknote data from a remote ERP database (SAP HANA / PostgreSQL).

---

## Architecture

Three-tier system communicating over HTTP and WebSocket:

```
Electron/React Frontend (port 5173 dev)
        |
        | WebSocket (ws://localhost:8001/ws) + HTTP (http://localhost:8000)
        v
Machine Controller — machine_code/ (port 8001)
  - Runs the full ML pipeline locally: YOLO detection → RapidOCR → batch matching
  - Manages per-session state (current picknote, product scan progress)
  - Receives camera frames via POST /trigger
  - Sends real-time updates to frontend over WebSocket
        |
        | HTTP REST calls (HTTPX async) — picknote search + machine config only
        v
Backend API — backend/ (port 8000)
  - Picknote search/submit against remote DB
  - Config endpoints (machine config lookup)
```

**Critical data flow:** Camera frame → `machine_code` `/trigger` → local YOLO detection → local RapidOCR → local batch matching against loaded picknote products → WebSocket broadcast to frontend.

**Ambiguous state:** When multiple batch numbers score equally, the machine controller sends an `ambiguous` WebSocket message; the frontend shows a selection dialog; the user resolves it via an `ambiguous_resolved` message.

---

## Getting Started

### Prerequisites

- Python 3.12 (see `.python-version` files in each service directory)
- [`uv`](https://github.com/astral-sh/uv) for Python dependency management
- Node.js + npm for the frontend

### Backend (port 8000)

```bash
cd backend
cp .env.example .env        # fill in your DB config
uv sync                     # install deps from uv.lock
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Machine Controller (port 8001)

```bash
cd machine_code
cp .env.example .env        # fill in model paths, camera, backend URL
uv sync
python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev              # Vite dev server only (port 5173)
npm run electron:dev     # Vite + Electron desktop app together
npm run electron:build   # Package as AppImage / DMG / NSIS installer
```

---

## Configuration

### Backend (`backend/.env`)

| Variable | Description |
|---|---|
| `REMOTE_DB_HOST` | Remote ERP database host |
| `REMOTE_DB_PORT` | Remote ERP database port |
| `REMOTE_DB_USER` | Remote DB username |
| `REMOTE_DB_PASSWORD` | Remote DB password |
| `REMOTE_DB_NAME` | Remote DB name |
| `REMOTE_DB_TYPE` | `hana`, `postgresql`, or `mysql` |
| `LOCAL_DB_*` | Local PostgreSQL connection (fallback) |

### Machine Controller (`machine_code/.env`)

| Variable | Description |
|---|---|
| `GPU_API_URL` | URL of the backend API (default: `http://localhost:8000`) |
| `MODEL_PATH` | Path to YOLO `.pt` or `.onnx` model file |
| `MODEL_CLASSES` | List of model class names |
| `MODEL_THRESHOLD` | Detection confidence threshold |
| `MODEL_NAME` / `MODEL_VERSION` | Used to download model from GitLab registry if missing |
| `OCR_DET_MODEL_PATH` | Path to RapidOCR detection ONNX model |
| `OCR_REC_MODEL_PATH` | Path to RapidOCR recognition ONNX model |
| `OCR_KEYS_PATH` | Path to OCR character dictionary file |
| `CAMERA_INDEX` | OpenCV camera device index |
| `CAMERA_WIDTH` / `CAMERA_HEIGHT` | Camera capture resolution |
| `GITLAB_URL` / `GITLAB_PROJECT_ID` / `GITLAB_TOKEN` | GitLab model registry credentials |
| `STATE_DB_PATH` | SQLite file path for persisting session state |

Frontend service URLs are hardcoded in `frontend/src/App.jsx`:
- WebSocket: `ws://localhost:8001/ws`
- Backend API: `http://localhost:8000`

---

## ML Pipeline

All ML inference runs inside the **machine controller** — the backend has no ML dependencies.

### Detection
- YOLO v8 OBB (oriented bounding box) model detects batch label regions in the camera frame
- Global model instance protected by a `threading.Lock` for concurrent requests
- Returns cropped and perspective-transformed label images

### OCR
- RapidOCR with ONNX models (`det_model.onnx`, `rec_model.onnx`, `ppocrv5_dict.txt`)
- If OCR confidence is below 0.8, retries automatically with the image rotated 180°
- Global OCR instance also behind a thread lock

### Matching
- Matches OCR-extracted text against `state.products` (loaded into memory from picknote search)
- Returns one of three outcomes:
  - `accepted` — single unambiguous match
  - `ambiguous` — multiple equally-scored matches (user selects via dialog)
  - `rejected` — no match found

---

## Key Files

| Path | Purpose |
|---|---|
| `backend/core/config.py` | Env-var config — DB connection settings |
| `backend/routes/picknote.py` | Picknote search and submit endpoints |
| `backend/routes/config.py` | Machine config lookup endpoint |
| `machine_code/core/config.py` | Machine controller config — model paths, camera, OCR settings |
| `machine_code/api/app.py` | WebSocket `/ws` and HTTP `/trigger` endpoints |
| `machine_code/services/pipeline.py` | Orchestrates detection → OCR → matching pipeline |
| `machine_code/services/recognition.py` | YOLO detection + RapidOCR inference (local) |
| `machine_code/services/matching.py` | Batch number matching against loaded picknote products |
| `machine_code/services/controller.py` | Top-level coordinator: state, backend client, pipeline, WebSocket |
| `machine_code/services/state.py` | Session state, snapshot builder, stats aggregation |
| `machine_code/services/backend_client.py` | HTTPX client — picknote search and machine config calls |
| `frontend/src/App.jsx` | Entire frontend UI (single component) |
| `frontend/src/hooks/useMachineSocket.js` | WebSocket lifecycle, reconnection, message routing |

---

## Tech Stack

| Layer | Technologies |
|---|---|
| Frontend | React 19, Vite 7, Electron 40, SweetAlert2 |
| Machine Controller | FastAPI 0.135, Uvicorn, YOLO v8 (Ultralytics), RapidOCR 1.4, OpenCV, HTTPX |
| Backend | FastAPI 0.135, Uvicorn, async SQLAlchemy 2.0 |
| Databases | SAP HANA (hdbcli), PostgreSQL (asyncpg) |
| Logging | Loguru throughout both Python services |




cd backend
docker build -t epiu/medicinestrip-ai:prod .
