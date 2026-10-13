# Employee Attendance & Analytics API

FastAPI and MongoDB (6.0 or newer). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit it, or export MONGO_URI and MONGO_DB yourself
uvicorn app.main:app --port 8000
```
`MONGO_URI` is required. `MONGO_DB` is optional if the URI already names a database. Real environment variables take priority over `.env`. Indexes are created at startup.

## Notes
- `moving_avg_7d` is the mean of the exact daily rates in the window, rounded half up to 4 decimals once at the end (R8 rounds reported numbers). Averaging the already rounded `attendance_rate` values would differ in the last digit on some rows.
- The explain endpoint and the real endpoints share the same query builders, and each query uses an index hint so the plan is an `IXSCAN`.

**I used an AI assistant as a reference while building this, and I ran and tested the code myself.**
