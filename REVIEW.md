# REVIEW.md

I read the starter `app/main.py` the way I would read a pull request. Every defect below breaks something in
`openapi.yaml` (an endpoint, a status code, a field, or one of the rules R1-R10) or in `DATA_MODEL.md`.

**How I checked.** "Reproduced" means I ran the starter code against different test cases using postman against sample data in mongodb and then an in-memory MongoDB (mongomock) and saw the
result myself. "From reading" means I worked it out from the code, and the row says why I could not run it.

**About the helper functions.** I kept the three helper names, but I did not reuse them as they were:
- All three built the shift time from the punch's own calendar day, which breaks overnight shifts.
  Now the caller works out the correct shift day (`attendance_date`, `shift_start_dt`, `shift_end_dt`) and passes
  a ready datetime to the helper.
- `compute_late_minutes` compared whole minutes, `compute_work_hours` used `round()`, and `compute_overtime` had no
  30-minute rule. They now follow R2, R4 and R3 exactly.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes()` / `minutes > 10` | Contract R2: late means *strictly more than 10:00* after shift start. The starter compares **whole minutes**, so anything up to 10 min 59 s counts as on time. | Reproduced. Shift 09:30, punch-in 09:40:01 returns 0 and 09:40:59 returns 0. Both should be 10. | Work in seconds: `secs > 600`, then return `secs // 60` (minutes counted from shift start). |
| 2 | `compute_late_minutes()` + `punch_in()` / `punch_in.replace(hour=...)` | Contract R1: the shift start is built on the punch's own day in the server's time zone, with no overnight rule. | Shift 22:00-06:00, punch-in 05:30 IST. Starter: same date, late 0 (the 22:00 it builds is later than the punch). Expected: previous date, late 450. | New `attendance_date()` applies the overnight rule. Shift start is built from that date with IST. |
| 3 | `compute_work_hours()` / `round(..., 2)` | Contract R4: rounding must be half-up. Python's `round()` works on floats and rounds ties to even. | Reproduced. 450 s (0.125 h) gives 0.12, expected 0.13. 9630 s (2.675 h) gives 2.67, expected 2.68. | `Decimal(secs) / Decimal(3600)` then `quantize(Decimal("0.01"), ROUND_HALF_UP)`. |
| 4 | `compute_overtime()` / `max(0, ...)` and shift end from the record date | Contract R3: overtime counts only at 30 minutes or more. The shift end is always put on the record's date, but an overnight shift ends the next day. | Reproduced. Shift end 18:30, punch-out 18:35 returns 5 (expected 0). Shift 22:00-06:00, punch-out 06:40 next day returns 1480 (expected 40). | `return mins if mins >= 30 else 0`. `shift_end_dt()` adds one day when the shift is overnight. |
| 5 | `client = MongoClient(...)` | `DATA_MODEL.md` warns that PyMongo returns **naive** datetimes by default. The starter mixes them with time-zone-aware ones. | Reproduced. Overtime with a naive `punch_out` raises `TypeError: can't subtract offset-naive and offset-aware datetimes`. | `tz_aware=True`, plus a small `as_utc()` safety net. |
| 6 | Startup (no `lifespan`, no `create_index`) | The yaml says to create all indexes at startup. Without unique indexes the 409 rules cannot be enforced, and listing and analytics scan the whole collection. | Reproduced. Two attendance rows for the same employee and day both insert. `index_information()` shows only `_id_`. | `lifespan` creates four indexes: unique `emp_code`, unique (`emp_code`, `date`), (`date` desc, `emp_code` asc), (`department`, `joined_on`). |
| 7 | `GET /health` / `health()` | Yaml: 200 only if MongoDB answers a ping, otherwise 503. The starter always says ok. | Reproduced. It returns `{"status": "ok"}` without touching the database, even with a wrong `MONGO_URI`. | `client.admin.command("ping")` in a try/except. On failure raise 503. |
| 8 | `POST /employees` and `POST /attendance/punch-in` / `find_one` then `insert_one` | Yaml: simultaneous duplicates must give exactly one 201 and the rest 409, "do not rely on check-then-insert". Two requests can both pass the check. | From reading (a real race needs parallel requests). The database accepts duplicates, see row 6. | Removed the checks. Insert directly and catch `DuplicateKeyError` from the unique indexes, then return 409. |
| 9 | `POST /employees` / `EmployeeIn` | Yaml `EmployeeCreate`: `emp_code` like `EMP0001`, valid email, `HH:MM` shifts, real `joined_on`, length limits, `shift_start` different from `shift_end`. The starter accepts anything. | Reproduced. `{"emp_code": "xyz", "name": "", "email": "nope", "shift_start": "9am", "shift_end": "9am", "joined_on": "banana"}` returns 201. | `Field` patterns and lengths from the yaml, plus a validator for equal shifts and a real `joined_on` date. |
| 10 | `POST /employees` / `doc["created_at"] = datetime.now()` | Yaml: `created_at` is epoch milliseconds. The starter stores local time without a time zone and returns an ISO string. | Reproduced. Response has `"created_at": "2026-10-14T11:23:42.628478"`. | Store `datetime.now(UTC)`. Return it through `employee_out()` as epoch milliseconds. |
| 11 | `GET /employees` / `skip`, `total`, no sort, no limits | R10 and yaml: `page` starts at 1 (`skip = page * page_size` skips a page). `total` must count the filtered set. The list is sorted by `emp_code`. `page_size` max is 100. | Reproduced. With 45 employees, `page=1` starts at `EMP0021`. `?department=Ops` (15 people) returns `total: 45`. `page_size=5000` returns 200. | `skip = (page - 1) * page_size`. `count_documents(q)`. `.sort("emp_code", 1)`. `Query(1, ge=1)` and `Query(20, ge=1, le=100)`. |
| 12 | `POST /attendance/punch-in` / `emp["shift_start"]` | Yaml: 404 for an unknown employee. `emp` is `None`, so the code crashes. | Reproduced. `{"emp_code": "NOPE"}` returns 500. | `if emp is None: raise HTTPException(404)`. |
| 13 | `PunchInIn` / `punched_at: Optional[int]`, `status: str` | Yaml: seconds-like numbers (below 100000000000), floats, strings and out-of-range values must be 422. Only `PRESENT`, `WFH`, `ON_DUTY` are allowed on punch-in. | Reproduced. `punched_at` of `1700000000`, `1.7e12`, `"1700000000000"` and `true` all return 201. `status: "LEAVE"` returns 201. | `StrictInt` with `ge=100_000_000_000` and `le=4_102_444_800_000`. `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 14 | `POST /attendance/punch-in` / `fromtimestamp(ms / 1000)` and `ts.date()` | R1: instants are cut to whole seconds, and `date` is the **IST** calendar date. The starter uses the server's local time, keeps milliseconds, and takes the date from that. | Reproduced (my machine was on UTC). A punch at 01:00:00.900 IST on 6 Jul is stored as `2026-07-05T19:30:00.900` with `date: 2026-07-05`. Expected `2026-07-06`, with no `.900`. | Integer maths: `ms // 1000`, then `datetime.fromtimestamp(secs, tz=UTC)`. `date` from `attendance_date()` (IST, overnight rule). |
| 15 | `punch_in()` and `GET /attendance` responses | Yaml `AttendanceRecord`: no `id` ("`_id` never appears"), instants are epoch milliseconds, and older records missing `late_minutes`, `overtime_minutes`, `half_day` or `history` must read as 0, 0, false, `[]`. | Reproduced. The response has an `id` field and `"punch_in": "2026-07-06T04:00:00"`. For an older record the four keys are missing. | One `serialize()` function used by every endpoint. It also converts the dates inside `history[].changes`. |
| 16 | `GET /attendance` / `list(find(q))` then Python sort and slice | Yaml: must stay fast on ~100,000 records, "do not load the collection into memory". Sort must be `date` descending, then `emp_code` ascending. | Reproduced the order problem: two records on the same date for `EMP0005` and `EMP0004` come back in that order. The memory problem shows as slow, heavy responses at 100k records. | `count_documents(q)` and `find(q).sort([("date", -1), ("emp_code", 1)]).skip(...).limit(...)`, so the database does the work. |
| 17 | `GET /attendance` / parameters | Yaml: `date_from` after `date_to` is 422. R10: `page_size` max 100. Dates and `status` must be valid. The starter checks none of this. | Reproduced. `date_from=2026-07-10&date_to=2026-07-01` returns 200. `date_from=yesterday`, `status=BANANA` and `page_size=1000` all return 200. | Date pattern plus a real-date check, 422 when `date_from > date_to`, `Literal` for `status`, `Query` limits for the page values. |

## Looked at and decided it was fine

* **`load_dotenv()`**: python-dotenv does not overwrite variables that already exist, so real environment variables win
  over `.env`, as the yaml asks.
* **`os.getenv("MONGO_URI", ...)` and the default database name**: they read the environment first and only fall back
  to a local default.
* **`IST = timezone(timedelta(hours=5, minutes=30))`**: India has no daylight saving time, so a fixed offset is correct.
* **`create_employee`: `doc.pop("_id", None)`**: `insert_one` adds `_id` to the dict and this line removes it, so `_id`
  is not leaked there. (The leak is `punch_in`'s `id`, row 15.)
* **`list_attendance`: `(page - 1) * page_size`**: the paging maths is right here, unlike `list_employees`.
* **Status codes (201 on create and punch-in), the default shifts `09:30` / `18:30`, and the starting values of a new
  punch-in record** (`punch_out: None`, `work_hours: None`, `overtime_minutes: 0`, `half_day: False`, `history: []`)
  all match the yaml.
* **`int(... / 60)` in the helpers**: for positive numbers it is the same as rounding down, which R2 and R3 ask for.
  Only the comparisons around it were wrong (rows 1 and 4).
* **The TODO list** (punch-out, regularize, the four analytics endpoints and explain): these are missing
  endpoints, not defects. I built them from the yaml.