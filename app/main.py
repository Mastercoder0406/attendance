"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment
      variables always win because load_dotenv() does not override them)

Layout of this file
    1. Configuration and database handles
    2. Time and rounding helpers
    3. Validation types and request models
    4. Serializers (MongoDB document -> API JSON)
    5. Business rules R1-R5 (late, overtime, work hours, attendance date)
    6. Aggregation / query builders (shared by the real endpoints AND /admin/explain)
    7. Startup (indexes)
    8. Endpoints
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, OperationFailure, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and database handles
# --------------------------------------------------------------------------- #
load_dotenv()  # never overrides variables that are already set in the environment

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones,
# so every datetime in this file is aware and arithmetic between them is always valid.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_SECOND = timedelta(seconds=1)

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6


# --------------------------------------------------------------------------- #
# 2. Time and rounding helpers
# --------------------------------------------------------------------------- #
def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive datetimes from MongoDB are UTC by definition; make them explicit."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def trunc(dt: Optional[datetime]) -> Optional[datetime]:
    """R1: instants are truncated to whole seconds before any calculation."""
    dt = aware(dt)
    return None if dt is None else dt.replace(microsecond=0)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """datetime -> epoch milliseconds (exact integer arithmetic, no float)."""
    dt = aware(dt)
    return None if dt is None else (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def half_up(value: Any, places: int) -> float:
    """R8: round half UP (Python's round() is banker's rounding on binary floats)."""
    if isinstance(value, Decimal128):
        d = value.to_decimal()
    elif isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value))


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def count_weekdays(first: Date, last: Date) -> int:
    """Mon-Fri days in [first, last] (R7). At most 31 iterations: calendar math, not data."""
    n, d = 0, first
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def unprocessable(message: str, loc: tuple = ("body",)) -> RequestValidationError:
    """A 422 with FastAPI's standard {"detail": [...]} body."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": message, "input": None}])


# --------------------------------------------------------------------------- #
# 3. Validation types and request models
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _check_date(v: str) -> str:
    if not _DATE_RE.fullmatch(v):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        Date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a valid calendar date")
    return v


def _check_month(v: str) -> str:
    if not _MONTH_RE.fullmatch(v) or int(v[:4]) < 1:
        raise ValueError("must be a month in YYYY-MM format")
    return v


DateStr = Annotated[str, AfterValidator(_check_date)]
MonthStr = Annotated[str, AfterValidator(_check_month)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# StrictInt: floats ("1.5", 1783312500000.0), strings and booleans are rejected with 422.
# The lower bound rejects values that look like epoch SECONDS.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: Annotated[str, Field(min_length=1)]
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: Annotated[str, Field(min_length=1)]
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="before")
    @classmethod
    def _no_explicit_null(cls, data):
        # "Omitted fields are unchanged" - an explicit null is not the same as omitted.
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null; omit it to leave it unchanged")
        return data


# --------------------------------------------------------------------------- #
# 4. Serializers (MongoDB document -> API JSON). _id never leaves the service.
# --------------------------------------------------------------------------- #
def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": to_ms(d.get("created_at")),
    }


def _hist_value(v: Any) -> Any:
    return to_ms(v) if isinstance(v, datetime) else v  # punch_in / punch_out are dates in Mongo


def history_out(entries: Optional[list]) -> list:
    out = []
    for e in entries or []:
        changes = {
            field: {"from": _hist_value(c.get("from")), "to": _hist_value(c.get("to"))}
            for field, c in (e.get("changes") or {}).items()
        }
        out.append({"at": to_ms(e.get("at")), "by": e.get("by"), "reason": e.get("reason"), "changes": changes})
    return out


def record_out(d: dict) -> dict:
    # Legacy documents may omit late/overtime/half_day/history: missing means 0 / false / [].
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": int(d.get("late_minutes") or 0),
        "overtime_minutes": int(d.get("overtime_minutes") or 0),
        "half_day": bool(d.get("half_day") or False),
        "history": history_out(d.get("history")),
    }


# --------------------------------------------------------------------------- #
# 5. Business rules R1 - R5
# --------------------------------------------------------------------------- #
def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _minutes(shift_end) <= _minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in; for an overnight shift, a punch-in earlier
    than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < _minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def _ist_instant(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    day = Date.fromisoformat(date_str) + timedelta(days=plus_days)
    h, m = divmod(_minutes(hhmm), 60)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift_start; then floor minutes
    since shift_start (not since the end of the grace period)."""
    seconds = (trunc(punch_in) - _ist_instant(att_date, shift_start)) // ONE_SECOND
    return seconds // 60 if seconds > 600 else 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    """R3: whole minutes from shift_end to punch_out, counted only if >= 30. The end of an
    overnight shift falls on the next calendar day."""
    end = _ist_instant(att_date, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    minutes = (trunc(punch_out) - end) // timedelta(minutes=1)
    return minutes if minutes >= 30 else 0


def work_hours_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in exact integer arithmetic: floor(seconds * 100 / 3600 + 1/2) == half-up to 2 dp."""
    seconds = (trunc(punch_out) - trunc(punch_in)) // ONE_SECOND
    return (200 * seconds + 3600) // 7200


def derived_for_open(late: int) -> dict:
    return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}


def derived_for_closed(emp: dict, att_date: str, punch_in: datetime, punch_out: datetime, late: int) -> dict:
    hundredths = work_hours_hundredths(punch_in, punch_out)
    return {
        "work_hours": hundredths / 100,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], att_date),
        "half_day": hundredths < 450,  # R5: the ROUNDED work_hours < 4.50
    }


# --------------------------------------------------------------------------- #
# 6. Query / aggregation builders. The explain endpoint calls the SAME builders.
# --------------------------------------------------------------------------- #
# Index key patterns used as hints so the plan is deterministic.
HINT_LOG_EMP_DATE = {"emp_code": 1, "date": 1}
HINT_LOG_DATE = {"date": -1, "emp_code": 1}
HINT_EMP_DEPT_JOINED = {"department": 1, "joined_on": 1}

# Reusable aggregation expressions (they read STORED derived values - never recompute).
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
_DAY_OF_WEEK = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Sun..7=Sat
_IS_WEEKDAY = {"$and": [{"$gte": [_DAY_OF_WEEK, 2]}, {"$lte": [_DAY_OF_WEEK, 6]}]}
_HALF_AWARE_WEIGHT = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}  # R5: half day = 0.5
_PRESENT_ANY_DAY = {"$cond": [_IS_PRESENT, _HALF_AWARE_WEIGHT, 0]}
_PRESENT_WEEKDAY = {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _HALF_AWARE_WEIGHT, 0]}  # R6 + R7
_LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$gt": [_LATE_MIN, 0]}
_HAS_WORK_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def attendance_list_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from must not be after date_to", ("query", "date_from"))
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _PRESENT_WEEKDAY},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}},
                "total_late_minutes": {"$sum": _LATE_MIN},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Runs on `employees` so that people with ZERO logs still produce a row (R9).
    The per-employee month stats are computed inside $lookup (index on emp_code, date),
    then $unwind with preserveNullAndEmptyArrays keeps employees whose stats are empty."""
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": _PRESENT_WEEKDAY},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                            "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}},
                            "total_late_minutes": {"$sum": _LATE_MIN},
                            # Decimal128 keeps the sum of 2-dp values exact; divided half-up in Python.
                            "wh_sum": {"$sum": {"$cond": [_HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
                            "wh_cnt": {"$sum": {"$cond": [_HAS_WORK_HOURS, 1, 0]}},
                        }
                    },
                ],
                "as": "s",
            }
        },
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
                "wh_sum": {"$sum": {"$ifNull": ["$s.wh_sum", 0]}},
                "wh_cnt": {"$sum": {"$ifNull": ["$s.wh_cnt", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist have an empty `emp` and are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank INSIDE the department
    pipeline += [
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking: ties at the cutoff are all kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def department_trend_pipeline(codes: list, joined_on: list, frm: str, to: str) -> list:
    """One row per calendar day, built inside the database:
    $match -> $facet (aggregate the days that have records; $facet always emits exactly ONE
    document, even when there are no records) -> $range/$dateAdd generate every day of the
    range -> join the daily totals back -> $setWindowFields for the moving average.

    Rates are computed as integers scaled by 10000 using floor((2*x + d) / (2*d)), which is
    exact half-up with no floating-point drift. The 7-day average is the half-up mean of those
    (already rounded) daily rates."""
    first = Date.fromisoformat(frm)
    n_days = (Date.fromisoformat(to) - first).days + 1
    first_dt = datetime(first.year, first.month, first.day, tzinfo=UTC)
    window = {"documents": [-6, 0]}  # current row + up to 6 preceding rows, inside the range only
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm, "$lte": to}}},
        {
            "$facet": {
                "daily": [
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {"$sum": _PRESENT_ANY_DAY},
                            "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}},
                        }
                    }
                ]
            }
        },
        {
            "$project": {
                "daily": 1,
                "date": {
                    "$map": {
                        "input": {"$range": [0, n_days]},
                        "as": "i",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {"$dateAdd": {"startDate": first_dt, "unit": "day", "amount": "$$i"}},
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$date"},
        {
            "$addFields": {
                "_m": {
                    "$arrayElemAt": [
                        {"$filter": {"input": "$daily", "as": "d", "cond": {"$eq": ["$$d._id", "$date"]}}},
                        0,
                    ]
                }
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$_m.present_count", 0]},
                "late_count": {"$ifNull": ["$_m.late_count", 0]},
                "headcount": {
                    "$size": {
                        "$filter": {"input": {"$literal": joined_on}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}
                    }
                },
                "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
            }
        },
        {"$addFields": {"is_working_day": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}}},
        {
            "$addFields": {
                "_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_ma_sum": {"$sum": "$_rate", "window": window},
                    "_ma_n": {"$sum": {"$cond": [{"$isNumber": "$_rate"}, 1, 0]}, "window": window},
                },
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
                "attendance_rate": {"$cond": [{"$isNumber": "$_rate"}, {"$divide": ["$_rate", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$_ma_n", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": ["$_ma_sum", 2]}, "$_ma_n"]},
                                            {"$multiply": ["$_ma_n", 2]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def trend_inputs(department: str, frm: str, to: str) -> tuple[list, list]:
    """Validate the range and fetch the department's employee codes / join dates (tiny query
    on the {department, emp_code} index). 404 when the department has no employees."""
    d_from, d_to = Date.fromisoformat(frm), Date.fromisoformat(to)
    if d_to < d_from:
        raise unprocessable("'to' must not be before 'from'", ("query", "to"))
    if (d_to - d_from).days + 1 > 92:
        raise unprocessable("range must not exceed 92 days", ("query", "to"))
    rows = list(employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not rows:
        raise HTTPException(404, "unknown department")
    return [r["emp_code"] for r in rows], sorted(r["joined_on"] for r in rows)


# --------------------------------------------------------------------------- #
# 7. Startup: indexes (idempotent) and error handling
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    specs = [
        # one employee per emp_code - enforced by the database, not by check-then-insert
        (employees, [("emp_code", ASCENDING)], {"unique": True, "name": "uq_emp_code"}),
        # GET /employees?department=...  (filter + sort by emp_code) and trend employee lookup
        (employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
        # department summary: joined_on <= end of month, optionally one department
        (employees, [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
        # at most one record per (emp_code, date): race-safe punch-in + monthly + trend + $lookup
        (logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uq_emp_date"}),
        # GET /attendance sort (date desc, emp_code asc) and the leaderboard date range
        (logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
        # GET /attendance?status=... with the same sort
        (logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
        # punch-out: latest record of an employee whose punch_in <= punched_at
        (logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    ]
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except OperationFailure as exc:
            # One bad index must not stop the others from being created. If existing data
            # breaks a UNIQUE index, keep a plain index with the same keys so queries still
            # use it (the race-safety guarantee is then lost, so this is logged loudly).
            log.error("could not create index %s on %s: %s", opts.get("name"), coll.name, exc)
            if opts.get("unique"):
                plain = {k: v for k, v in opts.items() if k != "unique"}
                coll.create_index(keys, **plain)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for attempt in range(1, 4):
        try:
            ensure_indexes()
            break
        except ConnectionFailure as exc:  # MongoDB not reachable yet: wait and retry
            log.warning("index creation attempt %d failed: %s", attempt, exc)
            time.sleep(1)
        except PyMongoError as exc:  # e.g. bad existing data: do not crash the whole service
            log.error("index creation failed: %s", exc)
            break
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request: Request, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database error"})


# --------------------------------------------------------------------------- #
# 8. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        employees.insert_one(dict(doc))  # the unique index decides; copy keeps _id out of `doc`
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = employees.count_documents(q)  # counts the FILTERED set
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ---- attendance ----------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_for_open(compute_late_minutes(ts, emp["shift_start"], att_date)),
        "history": [],
    }
    try:
        # Single atomic insert guarded by the unique (emp_code, date) index: of N simultaneous
        # requests exactly one insert succeeds, the others raise DuplicateKeyError -> 409.
        logs.insert_one(dict(doc))
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # Latest record whose punch_in <= ts. Works across midnight because it does not use `date`.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = trunc(rec["punch_in"])
    elapsed = (ts - p_in) // ONE_SECOND
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise unprocessable("punched_at must be after punch_in and within 24 hours of it", ("body", "punched_at"))
    derived = derived_for_closed(emp, rec["date"], p_in, ts, int(rec.get("late_minutes") or 0))
    derived.pop("late_minutes")  # unchanged at punch-out
    # Atomic compare-and-set: only succeeds while punch_out is still empty, so of two
    # simultaneous punch-outs exactly one matches; the loser gets 409.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(spec["filter"])
    cursor = (
        logs.find(spec["filter"], {"_id": 0})
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Validate the correction and return (new values of the 7 fields, changes for history)."""
    old_status = rec["status"]
    old_in, old_out = aware(rec.get("punch_in")), aware(rec.get("punch_out"))
    new_status = body.status or old_status
    punch_given = body.punch_in is not None or body.punch_out is not None

    if new_status in ("ABSENT", "LEAVE"):
        if punch_given:
            raise unprocessable("punch_in/punch_out cannot be combined with status ABSENT or LEAVE")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else trunc(old_in)
        new_out = from_ms(body.punch_out) if body.punch_out is not None else trunc(old_out)
        if new_in is None:
            raise unprocessable("a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if new_out is not None:
            elapsed = (new_out - new_in) // ONE_SECOND
            if elapsed <= 0 or elapsed > 24 * 3600:
                raise unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    if new_status == old_status and new_in == trunc(old_in) and new_out == trunc(old_out):
        raise unprocessable("the request changes nothing")

    if new_status in ("ABSENT", "LEAVE"):
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        late = compute_late_minutes(new_in, emp["shift_start"], rec["date"])
        if new_out is None:
            derived = derived_for_open(late)
        else:
            derived = derived_for_closed(emp, rec["date"], new_in, new_out, late)

    old_vals = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }
    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {f: {"from": old_vals[f], "to": new_vals[f]} for f in _FIELDS if old_vals[f] != new_vals[f]}
    return new_vals, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    for _attempt in range(4):
        rec = logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new_vals, changes = plan_regularization(emp, rec, body)
        entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # Optimistic concurrency: the write only applies if the record is exactly as we read it.
        # `history` only ever grows by one entry per correction, so its length acts as a version.
        history = rec.get("history")
        flt: dict = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "history": {"$size": len(history)} if isinstance(history, list) else None,
        }
        update: dict = {"$set": dict(new_vals)}
        if isinstance(history, list) or "history" not in rec:
            update["$push"] = {"history": entry}  # append, never overwrite
        else:
            update["$set"]["history"] = [entry]  # history stored as null: start the array

        updated = logs.find_one_and_update(flt, update, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
        # someone else changed the record between our read and write: re-read and re-apply
    raise HTTPException(409, "record was modified concurrently, please retry")


# ---- analytics ------------------------------------------------------------ #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    start, end = month_bounds(month)
    rows = list(logs.aggregate(employee_monthly_pipeline(emp_code, start, end), hint=HINT_LOG_EMP_DATE))
    agg = rows[0] if rows else {}

    first_day = max(Date.fromisoformat(start), Date.fromisoformat(emp["joined_on"]))  # R7
    last_day = Date.fromisoformat(end)
    working_days = count_weekdays(first_day, last_day) if first_day <= last_day else 0
    present = to_decimal(agg.get("present_days", 0))
    pct = half_up(present / Decimal(working_days) * 100, 2) if working_days else None
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
    start, end = month_bounds(month)
    rows = list(employees.aggregate(department_summary_pipeline(start, end, department), hint=HINT_EMP_DEPT_JOINED))
    items = []
    for r in rows:
        count = int(r["wh_cnt"])
        avg = half_up(to_decimal(r["wh_sum"]) / Decimal(count), 2) if count else None  # mean over RECORDS
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": half_up(r["present_days"], 2),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    rows = list(logs.aggregate(late_leaderboard_pipeline(start, end, limit, department), hint=HINT_LOG_DATE))
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


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: DateStr = Query(..., alias="from"),
    to: DateStr = Query(...),
):
    codes, joined = trend_inputs(department, from_, to)
    rows = list(logs.aggregate(department_trend_pipeline(codes, joined, from_, to), hint=HINT_LOG_EMP_DATE))
    items = [
        {
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": r["present_count"],
            "late_count": int(r["late_count"]),
            "attendance_rate": r["attendance_rate"],
            "moving_avg_7d": r["moving_avg_7d"],
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# ---- admin: explain ------------------------------------------------------- #
def _require(name: str, value: Any, loc: str = "query") -> Any:
    if value is None:
        raise unprocessable(f"{name} is required for this endpoint", (loc, name))
    return value


def _explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))  # ObjectId / dates / Int64 -> plain JSON


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Optional[DateStr] = Query(None, alias="from"),
    to: Optional[DateStr] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Every branch uses the SAME builder as the real endpoint, so the plan is the real plan.
    if endpoint == "attendance_list":
        spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, **spec}
    elif endpoint == "employee_monthly":
        _require("emp_code", emp_code)
        start, end = month_bounds(_require("month", month))
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": employee_monthly_pipeline(emp_code, start, end),
            "cursor": {},
            "hint": HINT_LOG_EMP_DATE,
        }
    elif endpoint == "department_summary":
        start, end = month_bounds(_require("month", month))
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": department_summary_pipeline(start, end, department),
            "cursor": {},
            "hint": HINT_EMP_DEPT_JOINED,
        }
    elif endpoint == "late_leaderboard":
        start, end = month_bounds(_require("month", month))
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": late_leaderboard_pipeline(start, end, limit, department),
            "cursor": {},
            "hint": HINT_LOG_DATE,
        }
    else:  # department_trend
        _require("department", department)
        codes, joined = trend_inputs(department, _require("from", from_), _require("to", to))
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": department_trend_pipeline(codes, joined, from_, to),
            "cursor": {},
            "hint": HINT_LOG_EMP_DATE,
        }
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(command)}