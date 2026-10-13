"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI (required), MONGO_DB (optional if the URI names a database). A local .env
      is read; real environment variables win.

Layout of this file:
  1. config and mongo handles
  2. time helpers and business rules (R1 to R5)
  3. request validation types
  4. indexes
  5. response shaping
  6. employee and attendance endpoints
  7. aggregation plans (shared by the analytics endpoints and the explain endpoint)
  8. analytics endpoints
  9. explain endpoint
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import ReturnDocument, MongoClient
from pymongo.errors import ConfigurationError, ConnectionFailure, DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config and mongo handles
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False, so real environment variables win over .env

log = logging.getLogger("attendance")

MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set (see .env.example)")

# tz_aware=True makes PyMongo hand back UTC-aware datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
# MONGO_DB names the database; without it the database named in the URI is used.
DB_NAME = os.environ.get("MONGO_DB")
if not DB_NAME:
    try:
        DB_NAME = client.get_default_database().name
    except ConfigurationError:
        raise RuntimeError("Set MONGO_DB, or put a database name in MONGO_URI")
db = client[DB_NAME]
employees = db["employees"]
logs = db["attendance_logs"]

IDX_EMP_CODE = "emp_code_1"
IDX_EMP_DEPT = "department_1_joined_on_1"
IDX_LOG_KEY = "emp_code_1_date_1"
IDX_LOG_DATE = "date_-1_emp_code_1_late_minutes_1"
IDX_LOG_STATUS = "status_1_date_-1_emp_code_1"

# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_CENTS = 450  # 4.50 hours, compared on the already rounded value
MAX_PUNCH_GAP = timedelta(hours=24)
MAX_SKIP = 10**12

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_ms(dt: datetime) -> int:
    return (to_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch ms to an aware UTC datetime, cut down to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def whole_seconds(dt: datetime) -> datetime:
    return to_utc(dt).replace(microsecond=0)


def now_seconds() -> datetime:
    return whole_seconds(datetime.now(UTC))


def now_millis() -> datetime:
    """Current time at the precision MongoDB keeps for dates (milliseconds)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in, or the day before for an early overnight punch-in."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    return datetime.combine(Date.fromisoformat(date_str), hm(shift_start), tzinfo=IST)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(Date.fromisoformat(date_str), hm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def late_minutes_for(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start, counted from shift start."""
    seconds = int((punch_in - shift_start_at(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def overtime_for(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only when that is at least 30."""
    seconds = int((punch_out - shift_end_at(date_str, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def work_hours_for(punch_in: datetime, punch_out: datetime) -> tuple[float, bool]:
    """R4 and R5. Integer maths so the half-up rounding is exact."""
    seconds = int((punch_out - punch_in).total_seconds())
    cents = (seconds * 200 + 3600) // 7200  # floor(seconds * 100 / 3600 + 0.5)
    return cents / 100, cents < HALF_DAY_CENTS


def derive(emp: dict, status: str, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """The four stored derived fields for a record, from its final status and times."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    out["late_minutes"] = late_minutes_for(punch_in, date_str, emp["shift_start"])
    if punch_out is not None:
        hours, half = work_hours_for(punch_in, punch_out)
        out["work_hours"] = hours
        out["half_day"] = half
        out["overtime_minutes"] = overtime_for(punch_out, date_str, emp["shift_start"], emp["shift_end"])
    return out


def half_up(value: Decimal, places: int) -> float:
    """R8: round half up (not banker's rounding)."""
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def clean_sum(value: Any) -> Decimal:
    """A sum of doubles from MongoDB with float noise (1e-14) removed."""
    return Decimal(repr(float(value))).quantize(Decimal("0.000001"))


def month_range(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_weekdays(start: Date, end: Date) -> int:
    if end < start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Request validation types
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
HM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def _valid_date(v: str) -> str:
    if not DATE_RE.fullmatch(v):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        Date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a real calendar date")
    return v


def _valid_month(v: str) -> str:
    if not MONTH_RE.fullmatch(v):
        raise ValueError("must be a month in YYYY-MM format")
    try:
        Date(int(v[:4]), int(v[5:7]), 1)
    except ValueError:
        raise ValueError("year out of range")
    return v


DateStr = Annotated[str, AfterValidator(_valid_date)]
MonthStr = Annotated[str, AfterValidator(_valid_month)]
# strict=True rejects floats, strings and booleans; the range rejects values that look like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
LimitQ = Annotated[int, Query(ge=1, le=50)]


def reject(loc: str, msg: str, kind: str = "value_error", where: str = "query"):
    """Raise a 422 in FastAPI's normal body shape."""
    raise RequestValidationError([{"type": kind, "loc": (where, loc), "msg": msg, "input": None}])


def require(**params: Any) -> None:
    for name, value in params.items():
        if value is None:
            reject(name.rstrip("_"), "Field required", "missing")


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HM_PATTERN)
    shift_end: str = Field("18:30", pattern=HM_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


# --------------------------------------------------------------------------- #
# 4. Indexes
# --------------------------------------------------------------------------- #
_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> bool:
    """Idempotent. Returns True once every index exists."""
    global _indexes_ready
    wanted = [
        (employees, [("emp_code", 1)], {"unique": True, "name": IDX_EMP_CODE}),
        (employees, [("department", 1), ("joined_on", 1)], {"name": IDX_EMP_DEPT}),
        (logs, [("emp_code", 1), ("date", 1)], {"unique": True, "name": IDX_LOG_KEY}),
        # late_minutes is in the key so the leaderboard can be answered from the index alone
        (logs, [("date", -1), ("emp_code", 1), ("late_minutes", 1)], {"name": IDX_LOG_DATE}),
        (logs, [("status", 1), ("date", -1), ("emp_code", 1)], {"name": IDX_LOG_STATUS}),
    ]
    with _index_lock:
        if _indexes_ready:
            return True
        ok = True
        for coll, keys, options in wanted:
            try:
                coll.create_index(keys, **options)
            except ConnectionFailure as exc:
                log.error("mongo not reachable while creating indexes: %s", exc)
                return False
            except PyMongoError as exc:
                ok = False
                log.error("could not create index %s: %s", options["name"], exc)
        _indexes_ready = ok
        return ok


def hint_for(index_name: str) -> Optional[str]:
    """Use an index hint only when startup managed to build the indexes."""
    return index_name if _indexes_ready else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database error"})


# --------------------------------------------------------------------------- #
# 5. Response shaping
# --------------------------------------------------------------------------- #
def ms_or_none(value: Optional[datetime]) -> Optional[int]:
    return None if value is None else to_ms(value)


def plain(value: Any) -> Any:
    return to_ms(value) if isinstance(value, datetime) else value


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": ms_or_none(d.get("created_at")),
    }


def history_out(h: dict) -> dict:
    changes = {
        field: {"from": plain(c.get("from")), "to": plain(c.get("to"))}
        for field, c in (h.get("changes") or {}).items()
    }
    return {"at": ms_or_none(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def record_out(d: dict) -> dict:
    """Stored document to API shape. Missing legacy fields read as 0 / false / empty."""
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": ms_or_none(d.get("punch_in")),
        "punch_out": ms_or_none(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [history_out(h) for h in (d.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 6. Employee and attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", responses={503: {"description": "MongoDB did not answer"}})
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201, responses={409: {"description": "emp_code already exists"}})
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        employees.insert_one(doc)  # the unique index decides, not a prior lookup
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    flt = {} if department is None else {"department": department}
    skip = min((page - 1) * page_size, MAX_SKIP)
    total = employees.count_documents(flt)
    items = employees.find(flt, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    return {"items": [employee_out(d) for d in items], "total": total, "page": page, "page_size": page_size}


@app.post(
    "/attendance/punch-in",
    status_code=201,
    responses={404: {"description": "Unknown employee"}, 409: {"description": "Already punched in for that date"}},
)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, body.status, day, ts, None),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): of two racing requests only one gets in
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post(
    "/attendance/punch-out",
    responses={404: {"description": "Unknown employee or no punch-in found"}, 409: {"description": "Already punched out"}},
)
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()

    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    p_in = to_utc(rec["punch_in"])
    if ts <= p_in:
        reject("punched_at", "punched_at must be after the punch-in", where="body")
    if ts - p_in > MAX_PUNCH_GAP:
        reject("punched_at", "punched_at is more than 24 hours after the punch-in", where="body")

    fields = derive(emp, rec["status"], rec["date"], p_in, ts)
    # The filter repeats what we read (punch_out still empty, punch_in unchanged), so a
    # second punch-out or a concurrent correction makes this update match nothing.
    updated = logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was already punched out or changed by someone else")
    return record_out(updated)


def list_index(emp_code, status) -> str:
    if emp_code is not None:
        return IDX_LOG_KEY  # one employee's records, already ordered by date
    if status is not None:
        return IDX_LOG_STATUS  # status first, so filtering and counting stay inside the index
    return IDX_LOG_DATE


def attendance_list_plan(emp_code, date_from, date_to, status, page, page_size) -> dict:
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        rng = {}
        if date_from is not None:
            rng["$gte"] = date_from
        if date_to is not None:
            rng["$lte"] = date_to
        flt["date"] = rng
    if status is not None:
        flt["status"] = status
    return {
        "filter": flt,
        "sort": [("date", -1), ("emp_code", 1)],
        "skip": min((page - 1) * page_size, MAX_SKIP),
        "limit": page_size,
        "hint": hint_for(list_index(emp_code, status)),
    }


def check_date_order(date_from, date_to):
    if date_from is not None and date_to is not None and date_from > date_to:
        reject("date_from", "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    check_date_order(date_from, date_to)
    plan = attendance_list_plan(emp_code, date_from, date_to, status, page, page_size)
    flt = plan["filter"]
    options = {"hint": plan["hint"]} if plan["hint"] else {}
    if flt:
        total = logs.count_documents(flt, **options)
    else:
        total = logs.estimated_document_count()  # no filter: metadata count, no scan
    cursor = logs.find(flt, {"_id": 0})
    if plan["hint"]:
        cursor = cursor.hint(plan["hint"])
    cursor = cursor.sort(plan["sort"]).skip(plan["skip"]).limit(plan["limit"])
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch(
    "/attendance/{emp_code}/{date}",
    responses={404: {"description": "Unknown employee or no record for that date"}, 409: {"description": "Record changed by someone else"}},
)
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_in = rec.get("punch_in")
    old_out = rec.get("punch_out")
    status = body.status or rec["status"]

    if status in PRESENCE:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            reject("punch_in", "a presence status needs a punch_in", "missing", where="body")
        new_in = to_utc(new_in)
        new_out = None if new_out is None else to_utc(new_out)
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            reject("punch_in", "punch_in must fall on the record's attendance date", where="body")
        if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
            if new_out <= new_in:
                reject("punch_out", "punch_out must be after punch_in", where="body")
            if new_out - new_in > MAX_PUNCH_GAP:
                reject("punch_out", "punch_out must be within 24 hours of punch_in", where="body")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            reject("status", "ABSENT and LEAVE clear the punch times, so none can be supplied", where="body")
        new_in = new_out = None

    new_fields = {"status": status, "punch_in": new_in, "punch_out": new_out}
    new_fields.update(derive(emp, status, date, new_in, new_out))

    old_fields = {
        "status": rec["status"],
        "punch_in": None if old_in is None else to_utc(old_in),
        "punch_out": None if old_out is None else to_utc(old_out),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {
        k: {"from": old_fields[k], "to": new_fields[k]} for k in new_fields if new_fields[k] != old_fields[k]
    }
    if not changes:
        reject("body", "this request changes nothing", where="body")

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic lock. history only ever grows by one per correction, so its length is
    # a version number. If anyone changed the record since we read it, nothing matches.
    if "history" in rec and rec["history"] is not None:
        version = {"history": {"$size": len(rec["history"])}}
    else:
        version = {"history": {"$exists": False}}
    updated = logs.find_one_and_update(
        {"emp_code": emp_code, "date": date, "status": rec["status"], "punch_in": old_in, "punch_out": old_out, **version},
        {"$set": new_fields, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 7. Aggregation plans
#    Each analytics endpoint builds a plan here and the explain endpoint builds the
#    very same plan, so the explain output is for the query that really runs.
# --------------------------------------------------------------------------- #
WEEKDAYS = [2, 3, 4, 5, 6]  # $dayOfWeek: 1 = Sunday ... 7 = Saturday

LATE = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE, 0]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
DAY_WEIGHT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
PRESENT_ANY_DAY = {"$cond": [IS_PRESENT, DAY_WEIGHT, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def is_weekday(date_field: str) -> dict:
    parsed = {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d", "onError": None, "onNull": None}}
    return {"$in": [{"$dayOfWeek": parsed}, WEEKDAYS]}


PRESENT_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, is_weekday("$date")]}, DAY_WEIGHT, 0]}


def count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def monthly_plan(emp_code: str, month: str) -> dict:
    start, end = month_range(month)
    return {
        "collection": "attendance_logs",
        "hint": hint_for(IDX_LOG_KEY),
        "pipeline": [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {"$sum": PRESENT_WEEKDAY},
                    "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
                    "late_count": count_if(IS_LATE),
                    "total_late_minutes": {"$sum": LATE},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ],
    }


def department_summary_plan(month: str, department: Optional[str]) -> dict:
    """
    Logs are summed per employee first. The employees are then unioned in and everything is
    merged on emp_code, which attaches the department without a lookup per employee.
    Employees with no logs still arrive through the union, so headcount counts them.
    Logs of anyone who is not in the union (joined later, other department, unknown) are dropped.
    """
    start, end = month_range(month)
    emp_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        emp_match["department"] = department
    sums = ["present_days", "hours_sum", "hours_n", "late_count", "late_minutes", "leave_count", "on_duty_count"]
    return {
        "collection": "attendance_logs",
        "hint": hint_for(IDX_LOG_DATE),
        "pipeline": [
            {"$match": {"date": {"$gte": start, "$lte": end}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {"$sum": PRESENT_WEEKDAY},
                    "hours_sum": {"$sum": {"$cond": [HAS_HOURS, "$work_hours", 0]}},
                    "hours_n": count_if(HAS_HOURS),
                    "late_count": count_if(IS_LATE),
                    "late_minutes": {"$sum": LATE},
                    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }
            },
            {
                "$unionWith": {
                    "coll": "employees",
                    "pipeline": [
                        {"$match": emp_match},
                        {"$sort": {"emp_code": 1}},  # makes MongoDB walk the emp_code index, not the whole collection
                        {"$project": {"_id": "$emp_code", "department": 1, "is_emp": {"$literal": True}}},
                    ],
                }
            },
            {
                "$group": {
                    "_id": "$_id",
                    "department": {"$max": "$department"},
                    "is_emp": {"$max": "$is_emp"},
                    **{name: {"$sum": f"${name}"} for name in sums},
                }
            },
            {"$match": {"is_emp": True}},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$present_days"},
                    "hours_sum": {"$sum": "$hours_sum"},
                    "hours_n": {"$sum": "$hours_n"},
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"},
                }
            },
            {"$sort": {"_id": 1}},
        ],
    }


def leaderboard_plan(month: str, limit: int, department: Optional[str]) -> dict:
    """Same join idea as the summary: sum the logs, union the employees in, merge on emp_code."""
    start, end = month_range(month)
    emp_pipeline: list = []
    if department is not None:
        emp_pipeline.append({"$match": {"department": department}})  # rank inside the department only
    emp_pipeline += [
        {"$sort": {"emp_code": 1}},
        {"$project": {"_id": "$emp_code", "name": 1, "department": 1, "is_emp": {"$literal": True}}},
    ]
    return {
        "collection": "attendance_logs",
        "hint": hint_for(IDX_LOG_DATE),
        "pipeline": [
            {"$match": {"date": {"$gte": start, "$lte": end}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE}, "late_count": count_if(IS_LATE)}},
            {"$unionWith": {"coll": "employees", "pipeline": emp_pipeline}},
            {
                "$group": {
                    "_id": "$_id",
                    "name": {"$max": "$name"},
                    "department": {"$max": "$department"},
                    "is_emp": {"$max": "$is_emp"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "late_count": {"$sum": "$late_count"},
                }
            },
            # logs of unknown employees have no is_emp and drop out here
            {"$match": {"is_emp": True, "total_late_minutes": {"$gt": 0}}},
            # $rank is standard competition ranking: ties share a rank, the next rank is skipped.
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},  # limit after ranking
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ],
    }


def trend_plan(department: str, from_str: str, to_str: str) -> dict:
    """
    One row per calendar day, built inside MongoDB:
      1. per employee of the department, fetch the logs in range (index on emp_code, date)
      2. $facet: totals per day, and the list of join dates, side by side in one document
      3. $range builds every day of the interval; each day picks up its totals (or 0) and
         counts the join dates up to that day, which is the headcount
      4. rate and the 7 row window average
    The reported rate is rounded with integer maths (units of 0.0001) so half-up is exact.
    The moving average is taken over the exact rates and rounded once, at the end (R8 rounds
    reported numbers; R5 says "rounded" explicitly where a rounded value must be reused).
    """
    first = Date.fromisoformat(from_str)
    ndays = (Date.fromisoformat(to_str) - first).days + 1
    first_dt = datetime(first.year, first.month, first.day, tzinfo=UTC)
    day_label = {
        "$dateToString": {
            "format": "%Y-%m-%d",
            "date": {"$dateAdd": {"startDate": first_dt, "unit": "day", "amount": "$$i"}},
        }
    }
    window = {"documents": [-6, 0]}
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
                    {"$project": {"_id": 0, "date": 1, "pc": PRESENT_ANY_DAY, "lc": {"$cond": [IS_LATE, 1, 0]}}},
                ],
                "as": "logs",
            }
        },
        {
            "$facet": {
                "days": [
                    {"$unwind": "$logs"},
                    {"$group": {"_id": "$logs.date", "pc": {"$sum": "$logs.pc"}, "lc": {"$sum": "$logs.lc"}}},
                ],
                "joined": [{"$group": {"_id": None, "dates": {"$push": "$joined_on"}}}],
            }
        },
        {
            "$project": {
                "_id": 0,
                "rows": {
                    "$map": {
                        "input": {"$range": [0, ndays]},
                        "as": "i",
                        "in": {
                            "$let": {
                                "vars": {"d": day_label},
                                "in": {
                                    "$let": {
                                        "vars": {
                                            "hit": {
                                                "$arrayElemAt": [
                                                    {"$filter": {"input": "$days", "as": "x", "cond": {"$eq": ["$$x._id", "$$d"]}}},
                                                    0,
                                                ]
                                            },
                                            "started": {
                                                "$filter": {
                                                    "input": {"$ifNull": [{"$arrayElemAt": ["$joined.dates", 0]}, []]},
                                                    "as": "j",
                                                    "cond": {"$lte": ["$$j", "$$d"]},
                                                }
                                            },
                                        },
                                        "in": {
                                            "date": "$$d",
                                            "headcount": {"$size": "$$started"},
                                            "present_count": {"$ifNull": ["$$hit.pc", 0]},
                                            "late_count": {"$ifNull": ["$$hit.lc", 0]},
                                        },
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$set": {"is_working_day": is_weekday("$date")}},
        {
            "$set": {
                # rate in 1e-4 units, half up: floor((20000 * present + headcount) / (2 * headcount))
                "rate_k": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
                                    {"$multiply": ["$headcount", 2]},
                                ]
                            }
                        },
                        None,
                    ]
                },
                # exact rate, only used as input for the moving average
                "rate_raw": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                # $avg skips nulls and gives null when the window holds no rate at all
                "output": {"avg_rate": {"$avg": "$rate_raw", "window": window}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {"$cond": [{"$eq": ["$rate_k", None]}, None, {"$divide": ["$rate_k", 10000]}]},
                # half up to 4 decimals; the tiny extra 1e-9 only absorbs binary float noise
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$avg_rate", None]},
                        None,
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": ["$avg_rate", 10000]}, 0.500000001]}},
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    return {"collection": "employees", "hint": hint_for(IDX_EMP_DEPT), "pipeline": pipeline}


def run_plan(plan: dict) -> list:
    options = {"hint": plan["hint"]} if plan["hint"] else {}
    return list(db[plan["collection"]].aggregate(plan["pipeline"], allowDiskUse=True, **options))


def check_trend_range(from_str: str, to_str: str) -> None:
    if to_str < from_str:
        reject("to", "to must not be before from")
    if (Date.fromisoformat(to_str) - Date.fromisoformat(from_str)).days + 1 > 92:
        reject("to", "the range may not exceed 92 days")


# --------------------------------------------------------------------------- #
# 8. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", responses={404: {"description": "Unknown employee"}})
def employee_monthly(emp_code: str, month: MonthStr):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_range(month)
    rows = run_plan(monthly_plan(emp_code, month))
    agg = rows[0] if rows else {}

    # R7: working days run from the join date for a mid-month joiner
    working_days = count_weekdays(Date.fromisoformat(max(emp["joined_on"], first)), Date.fromisoformat(last))
    present = Decimal(str(agg.get("present_days", 0)))
    pct = None if working_days == 0 else half_up(present * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    rows = run_plan(department_summary_plan(month, department))
    items = []
    for r in rows:
        n = int(r["hours_n"])
        avg = None if n == 0 else half_up(clean_sum(r["hours_sum"]) / n, 2)
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": half_up(Decimal(str(r["present_days"])), 2),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: LimitQ = 10, department: Optional[str] = None):
    rows = run_plan(leaderboard_plan(month, limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", responses={404: {"description": "Unknown department"}})
def department_trend(
    department: str,
    from_: Annotated[DateStr, Query(alias="from")],
    to: DateStr,
):
    check_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = run_plan(trend_plan(department, from_, to))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# 9. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def json_safe(doc: Any) -> Any:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: LimitQ = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    if endpoint == "attendance_list":
        check_date_order(date_from, date_to)
        plan = attendance_list_plan(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command: dict = {
            "find": collection,
            "filter": plan["filter"],
            "sort": dict(plan["sort"]),
            "skip": plan["skip"],
            "limit": plan["limit"],
        }
        if plan["hint"]:
            command["hint"] = plan["hint"]
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            plan = monthly_plan(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            plan = department_summary_plan(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            plan = leaderboard_plan(month, limit, department)
        else:
            require(department=department, from_=from_, to=to)
            check_trend_range(from_, to)
            plan = trend_plan(department, from_, to)
        collection = plan["collection"]
        command = {"aggregate": collection, "pipeline": plan["pipeline"], "cursor": {}}
        if plan["hint"]:
            command["hint"] = plan["hint"]

    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json_safe(result)}
