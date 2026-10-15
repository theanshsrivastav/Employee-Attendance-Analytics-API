"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import json
import re
from calendar import monthrange
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, time, date
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

load_dotenv(override=False)

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS = 100_000_000_000      # below this the value looks like seconds -> 422
MAX_MS = 4_102_444_800_000    # 2100-01-01T00:00:00Z
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]  # StrictInt: no floats/strings/bools
date_type = date

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]     # R6: these count as present
NON_PRESENCE = ("ABSENT", "LEAVE")
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def compute_late_minutes(punch_in: datetime, start: datetime) -> int:
    """R2: late only if strictly > 10:00 after shift start; minutes counted from shift start."""
    secs = int((punch_in - start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(pin: datetime, pout: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals, half-up (exact Decimal math)."""
    secs = int((pout - pin).total_seconds())
    return (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_overtime(pout: datetime, end: datetime) -> int:
    """R3: whole minutes after shift end, only when >= 30."""
    mins = int((pout - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def round_half_up(value, places: int) -> float:
    """R8: half-up rounding. Decimal(str(x)) so 2.675 is treated as 2.675."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    try:
        return date_type.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid date: {value}")
    
# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ts_from_ms(ms: Optional[int]) -> datetime:
    """Epoch ms (or server clock) -> aware UTC datetime truncated to whole seconds (R1)."""
    secs = ms // 1000 if ms is not None else _time.time_ns() // 1_000_000_000
    return datetime.fromtimestamp(secs, tz=UTC)


def as_utc(v: datetime) -> datetime:
    if v.tzinfo is None:                 # safety net if a naive datetime ever shows up
        v = v.replace(tzinfo=UTC)
    return v.astimezone(UTC)


def to_ms(v: Optional[datetime]) -> Optional[int]:
    return None if v is None else (as_utc(v) - EPOCH) // timedelta(milliseconds=1)


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(emp: dict) -> bool:
    return hm(emp["shift_end"]) <= hm(emp["shift_start"])


def attendance_date(ts: datetime, emp: dict) -> date:
    """R1: IST calendar date of punch-in; overnight shift + early-morning punch -> previous day."""
    local = ts.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < hm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d


def shift_start_dt(d: date, emp: dict) -> datetime:
    return datetime.combine(d, hm(emp["shift_start"]), tzinfo=IST)


def shift_end_dt(d: date, emp: dict) -> datetime:
    end = datetime.combine(d, hm(emp["shift_end"]), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(emp) else end


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @model_validator(mode="after")
    def check_shift_and_date(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date_type.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a real calendar date")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY"]] = None   # only these three on punch-in


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------
# Serialization (BSON datetimes -> epoch ms, no _id)
# --------------------------------------------------------------------------
def _ms_if_dt(v):
    return to_ms(v) if isinstance(v, datetime) else v


def serialize_history(entry: dict) -> dict:
    changes = {}
    for field, ch in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": _ms_if_dt(ch.get("from")), "to": _ms_if_dt(ch.get("to"))}
        else:
            changes[field] = ch
    return {"at": to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def serialize(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),                       # legacy: missing -> false
        "history": [serialize_history(h) for h in doc.get("history", [])],  # legacy: missing -> []
    }
    
def employee_out(e: dict) -> dict:
    return {
        "emp_code": e["emp_code"],
        "name": e["name"],
        "email": e["email"],
        "department": e["department"],
        "shift_start": e["shift_start"],
        "shift_end": e["shift_end"],
        "joined_on": e["joined_on"],
        "created_at": to_ms(e.get("created_at")),
    }

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command('ping')
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)      # the unique emp_code index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
            .sort("emp_code", ASCENDING)
            .skip(skip)
            .limit(page_size)
        )
    return {"items": [employee_out(e) for e in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Unknown employee")

    ts = ts_from_ms(body.punched_at)
    d = attendance_date(ts, emp)

    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status or "PRESENT",
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start_dt(d, emp)),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index is the concurrency guard
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already recorded for this date")
    return serialize(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    parsed_date_from = None
    parsed_date_to = None

    try:
        if date_from:
            parsed_date_from = datetime.strptime(date_from, "%Y-%m-%d").date()
        if date_to:
            parsed_date_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date. Expected a real date as YYYY-MM-DD.")


    if parsed_date_from and parsed_date_to and parsed_date_from > parsed_date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
    
    if emp_code:
        q["emp_code"] = emp_code
        
    if status:
        q["status"] = status

    if parsed_date_from or parsed_date_to:
        q["date"] = {}
        if parsed_date_from:
            q["date"]["$gte"] = parsed_date_from.isoformat()
        if parsed_date_to:
            q["date"]["$lte"] = parsed_date_to.isoformat()
    
    # Get total count directly in MongoDB
    total = db.attendance_logs.count_documents(q)

    # Calculate pagination skip offset
    skip = (page - 1) * page_size

    # Query with sorting and limiting
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    
    return {"items": [serialize(d) for d in cursor], "total": total, "page": page, "page_size": page_size}

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Unknown employee")

    ts = ts_from_ms(body.punched_at)

    # Latest record that actually has a punch-in (skips seeded ABSENT / LEAVE).
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in record found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    pin = as_utc(record["punch_in"])
    diff = int((ts - pin).total_seconds())
    if diff <= 0 or diff > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    d = date.fromisoformat(record["date"])
    work_hours = compute_work_hours(pin, ts)
    update = {
        "punch_out": ts,
        "work_hours": float(work_hours),
        "half_day": work_hours < Decimal("4.50"),  # R5: on the rounded value
        "overtime_minutes": compute_overtime(ts, shift_end_dt(d, emp)),
        # status is deliberately NOT touched: WFH / ON_DUTY must survive punch-out
    }

    res = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None}, {"$set": update}
    )
    if res.matched_count == 0:  # lost a race with a concurrent punch-out
        raise HTTPException(status_code=409, detail="Already punched out")

    return serialize({**record, **update})



def derive(status: str, pin: Optional[datetime], pout: Optional[datetime], emp: dict, d: date):
    """Derived fields from the FINAL state of the record (R2-R5)."""
    if status in NON_PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pin, shift_start_dt(d, emp))
    if pout is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(pin, pout)
    return {
        "work_hours": float(wh),
        "late_minutes": late,
        "overtime_minutes": compute_overtime(pout, shift_end_dt(d, emp)),
        "half_day": wh < Decimal("4.50"),
    }


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    if not re.match(DATE_PATTERN, date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    try:
        d = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be a valid calendar date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_history = record.get("history") or []
    old = {
        "status": record["status"],
        "punch_in": as_utc(record["punch_in"]) if record.get("punch_in") else None,
        "punch_out": as_utc(record["punch_out"]) if record.get("punch_out") else None,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_status = body.status or old["status"]
    if new_status in NON_PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT/LEAVE records have no punch times; set a presence status to supply them",
            )
        new_in = new_out = None
    else:
        new_in = ts_from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ts_from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")
        if attendance_date(new_in, emp) != d:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")
        if new_out is not None:
            diff = int((new_out - new_in).total_seconds())
            if diff <= 0 or diff > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive(new_status, new_in, new_out, emp, d)}

    changes = {f: {"from": old[f], "to": new[f]} for f in new if new[f] != old[f]}
    if not changes:
        raise HTTPException(status_code=422, detail="The request changes nothing")

    now = datetime.now(UTC)
    entry = {
        "at": now.replace(microsecond=now.microsecond // 1000 * 1000),  # Mongo keeps ms precision
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: every successful PATCH grows history by one, so the
    # filter only matches if nobody else corrected this record since we read it.
    if old_history:
        guard = {"history": {"$size": len(old_history)}}
    else:
        guard = {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    res = db.attendance_logs.update_one(
        {"_id": record["_id"], **guard},
        {"$set": {f: c["to"] for f, c in changes.items()}, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=409, detail="Record was modified concurrently; retry")

    return serialize({**record, **{f: c["to"] for f, c in changes.items()}, "history": old_history + [entry]})


# ==========================================================================
# Analytics + admin endpoints
# ==========================================================================

def month_bounds(month: str):
    """'2026-07' -> (date(2026,7,1), date(2026,7,31))"""
    year, mon = int(month[:4]), int(month[5:7])
    return date_type(year, mon, 1), date_type(year, mon, monthrange(year, mon)[1])


def count_weekdays(start: date, end: date) -> int:
    """R7: Mon-Fri days from start to end, both inclusive."""
    n = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            n += 1
        day += timedelta(days=1)
    return n


def weekend_dates(start: date, end: date) -> list:
    """The Saturdays and Sundays between start and end, as 'YYYY-MM-DD' strings."""
    days = []
    day = start
    while day <= end:
        if day.weekday() >= 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def date_range(start: date, end: date) -> dict:
    return {"$gte": start.isoformat(), "$lte": end.isoformat()}

# Reusable MongoDB expressions. Stored derived values are used as they are (no recomputing);
# a missing late_minutes / half_day (legacy records) means 0 / false.
IS_PRESENT = {"$in": ["$status", PRESENCE]}
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}
DAY_VALUE = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}   # a half day counts 0.5


def present_weekday_days(weekends: list) -> dict:
    """R6/R7: a present-status record on Mon-Fri counts 1 (0.5 for a half day); weekend records count 0."""
    is_weekday = {"$eq": [{"$in": ["$date", weekends]}, False]}
    return {"$cond": [{"$and": [IS_PRESENT, is_weekday]}, DAY_VALUE, 0]}


# ---------------------------- employee monthly -------------------------------------------------------
def monthly_pipeline(emp_code: str, start: date, end: date) -> list:
    present_days = present_weekday_days(weekend_dates(start, end))
    return [
        {"$match": {"emp_code": emp_code, "date": date_range(start, end)}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_days},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MINUTES},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Unknown employee")

    first, last = month_bounds(month)
    start = max(first, date_type.fromisoformat(emp["joined_on"]))   # R7: count from joined_on
    working_days = count_weekdays(start, last)                      # 0 if joined after the month

    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    stats = rows[0] if rows else {}
    present_days = float(stats.get("present_days", 0))

    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(str(present_days)) / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# --------------------------- department summary --------------------------------------------------------
def headcount_pipeline(end: date, department: Optional[str]) -> list:
    """R9: everyone who joined on or before the last day of the month, logs or not."""
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        match["department"] = department
    return [{"$match": match}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}]


def summary_pipeline(start: date, end: date, department: Optional[str]) -> list:
    present_days = present_weekday_days(weekend_dates(start, end))
    pipeline = [
        {"$match": {"date": date_range(start, end)}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},   # logs without an employee are dropped
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.append({"$group": {
        "_id": "$emp.department",
        "present_days": {"$sum": present_days},
        # average over present-status records that have a work_hours value ($avg skips nulls)
        "avg_work_hours": {"$avg": {"$cond": [
            {"$and": [IS_PRESENT, {"$gt": ["$work_hours", None]}]}, "$work_hours", None]}},
        "late_count": {"$sum": IS_LATE},
        "total_late_minutes": {"$sum": LATE_MINUTES},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    }})
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = month_bounds(month)
    headcounts = list(db.employees.aggregate(headcount_pipeline(last, department)))
    stats = {r["_id"]: r for r in db.attendance_logs.aggregate(summary_pipeline(first, last, department))}

    items = []
    for h in sorted(headcounts, key=lambda r: r["_id"]):   # departments with headcount 0 never show up here
        s = stats.get(h["_id"], {})
        avg = s.get("avg_work_hours")
        items.append({
            "department": h["_id"],
            "headcount": h["headcount"],
            "present_days": float(s.get("present_days", 0)),
            "avg_work_hours": None if avg is None else round_half_up(avg, 2),
            "late_count": s.get("late_count", 0),
            "total_late_minutes": s.get("total_late_minutes", 0),
            "leave_count": s.get("leave_count", 0),
            "on_duty_count": s.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


# ----------------------------- late leaderboard ------------------------------------------------------------   
def leaderboard_pipeline(start: date, end: date, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": date_range(start, end), "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},   # logs whose employee does not exist are ignored
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$emp.name"},
            "department": {"$first": "$emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(first, last, department))

    items = []
    rank = 0
    previous_total = None
    for position, row in enumerate(rows, start=1):
        if row["total_late_minutes"] != previous_total:   # new total -> rank = position (1, 2, 2, 4)
            rank = position
            previous_total = row["total_late_minutes"]
        if rank > limit:                                  # limit applies after ranking; ties at rank <= limit stay
            break
        items.append({
            "rank": rank,
            "emp_code": row["_id"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })
    return {"month": month, "items": items}


# ------------------------------ department trend ------------------------------
def trend_pipeline(emp_codes: list, start: date, end: date) -> list:
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)   # upper bound is exclusive
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": date_range(start, end)}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [IS_PRESENT, DAY_VALUE, 0]}},   # weekdays and weekends
            "late_count": {"$sum": IS_LATE},
        }},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        # $densify adds a row for every day that has no records
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"date": 1}},
    ]


def build_trend_items(rows: list, joined_on_list: list) -> list:
    """rows: one per calendar day, oldest first. Adds headcount, rate and the 7-day moving average."""
    items = []
    rates = []
    for row in rows:
        working = date_type.fromisoformat(row["date"]).weekday() < 5
        headcount = sum(1 for joined in joined_on_list if joined <= row["date"])   # R9
        present = row["present_count"]

        rate = None
        if working and headcount > 0:
            rate = round_half_up(Decimal(str(present)) / headcount, 4)
        rates.append(rate)

        window = [r for r in rates[-7:] if r is not None]   # this row + up to 6 before it, inside the range
        moving = None
        if window:
            moving = round_half_up(sum(Decimal(str(r)) for r in window) / len(window), 4)

        items.append({
            "date": row["date"],
            "is_working_day": working,
            "headcount": headcount,
            "present_count": float(present),
            "late_count": row["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
    return items


def check_trend_range(start: date, end: date):
    if end < start:
        raise HTTPException(status_code=422, detail="'to' must not be before 'from'")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Range must not exceed 92 days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
    to: str = Query(..., pattern=DATE_PATTERN),
):
    start, end = parse_date(from_), parse_date(to)
    check_trend_range(start, end)

    employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(status_code=404, detail="Unknown department")

    emp_codes = [e["emp_code"] for e in employees]
    rows = list(db.attendance_logs.aggregate(trend_pipeline(emp_codes, start, end)))
    return {"department": department, "items": build_trend_items(rows, [e["joined_on"] for e in employees])}


# ---------------------------------- explain ----------------------------------
def need(**values):
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Every branch builds the same query / pipeline the real endpoint builds.
    if endpoint == "attendance_list":
        flt = {}
        if emp_code:
            flt["emp_code"] = emp_code
        if date_from or date_to:
            flt["date"] = {}
            if date_from:
                flt["date"]["$gte"] = parse_date(date_from).isoformat()
            if date_to:
                flt["date"]["$lte"] = parse_date(date_to).isoformat()
        if date_from and date_to and parse_date(date_from) > parse_date(date_to):
            raise HTTPException(status_code=422, detail="date_from must not be after date_to")
        if status:
            flt["status"] = status
        command = {"find": "attendance_logs", "filter": flt, "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size}

    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "attendance_logs", "pipeline": monthly_pipeline(emp_code, first, last), "cursor": {}}

    elif endpoint == "department_summary":
        need(month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "attendance_logs", "pipeline": summary_pipeline(first, last, department), "cursor": {}}

    elif endpoint == "late_leaderboard":
        need(month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "attendance_logs", "pipeline": leaderboard_pipeline(first, last, department), "cursor": {}}

    else:  # department_trend
        need(department=department, **{"from": from_}, to=to)
        start, end = parse_date(from_), parse_date(to)
        check_trend_range(start, end)
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        command = {"aggregate": "attendance_logs", "pipeline": trend_pipeline(emp_codes, start, end), "cursor": {}}

    result = db.command("explain", command, verbosity="executionStats")
    # explain output contains BSON types (dates, ObjectIds); json_util turns them into plain JSON
    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": json.loads(json_util.dumps(result)),
    }