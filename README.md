# Employee Attendance & Analytics API

A FastAPI + MongoDB service for punch-in / punch-out, manual corrections, and attendance analytics.
The contract is in `openapi.yaml`, the stored data is described in `DATA_MODEL.md`.

## How to run
1. Create a virtual environment and install the packages (tested with Python 3.12):
   ```
   python -m venv venv
   venv\Scripts\activate          # Windows   (Mac/Linux: source venv/bin/activate)
   pip install -r requirements.txt
   ```
2. Set the database. Either create a `.env` file next to `app/`, or use real environment variables
   (real environment variables win over `.env`):
   ```
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```
3. Start the server (the indexes are created at startup):
   ```
   uvicorn app.main:app --port 8000
   ```


## Good to know
- Times in requests and responses are **epoch milliseconds**. Dates are `YYYY-MM-DD` (IST), months `YYYY-MM`.
- The department trend uses `$densify`, which needs **MongoDB 5.1 or newer**.
- `REVIEW.md` lists the defects found in the starter code. `DECISIONS.md` explains the main design choices.