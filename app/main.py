"""
Employee Attendance & Analytics API
"""
import os
import re
import math
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EXPLAIN_ENDPOINTS = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ms_to_dt(value, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 100_000_000_000:
        raise HTTPException(422, "timestamps must be epoch milliseconds as an integer")
    try:
        d = datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "timestamp out of range")
    return d


def dt_to_ms(value):
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def money_round(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_day(s):
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s:
            raise ValueError()
        return d
    except Exception:
        raise HTTPException(422, "date must be YYYY-MM-DD")


def parse_month(s):
    if not re.fullmatch(r"\d{4}-\d{2}", s or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        return date.fromisoformat(s + "-01")
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")


def valid_time(s):
    if not re.fullmatch(r"\d{2}:\d{2}", s or ""):
        raise ValueError("time must be HH:MM")
    try:
        time.fromisoformat(s)
    except ValueError:
        raise ValueError("time must be HH:MM")
    return s


def shift_start_dt(day, shift_start):
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(day, time(h, m), IST).astimezone(UTC)


def shift_end_dt(day, shift_start, shift_end):
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    return datetime.combine(end_day, time(eh, em), IST).astimezone(UTC)


def attendance_day(punch_in, shift_start, shift_end):
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in, shift_start, day_str):
    start = shift_start_dt(parse_day(day_str), shift_start)
    seconds = (as_utc(punch_in) - start).total_seconds()
    if seconds <= 600:
        return 0
    return max(0, int(seconds // 60))


def compute_work_hours(punch_in, punch_out):
    return money_round((as_utc(punch_out) - as_utc(punch_in)).total_seconds() / 3600, 2)


def compute_overtime(punch_out, shift_start, shift_end, day_str):
    end = shift_end_dt(parse_day(day_str), shift_start, shift_end)
    seconds = (as_utc(punch_out) - end).total_seconds()
    mins = max(0, int(seconds // 60))
    return mins if mins >= 30 else 0


def compute_fields(doc, emp):
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    if pin:
        doc["late_minutes"] = compute_late_minutes(pin, emp["shift_start"], doc["date"])
    else:
        doc["late_minutes"] = 0
    if pin and pout:
        doc["work_hours"] = compute_work_hours(pin, pout)
        doc["overtime_minutes"] = compute_overtime(pout, emp["shift_start"], emp["shift_end"], doc["date"])
        doc["half_day"] = doc["work_hours"] < 4.50
    else:
        doc["work_hours"] = None
        doc["overtime_minutes"] = 0
        doc["half_day"] = False
    return doc


def api_doc(doc):
    if not doc:
        return doc
    d = dict(doc)
    d.pop("_id", None)
    for key in ("punch_in", "punch_out", "created_at", "at"):
        if key in d and isinstance(d[key], datetime):
            d[key] = dt_to_ms(d[key])
    if "history" in d:
        hist = []
        for entry in d.get("history") or []:
            e = dict(entry)
            if isinstance(e.get("at"), datetime):
                e["at"] = dt_to_ms(e["at"])
            hist.append(e)
        d["history"] = hist
    return d


def require_page(page, page_size):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be 1..100")


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_emp_date_late")


@app.on_event("startup")
def startup():
    create_indexes()


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def emp_code_valid(cls, v):
        if not re.fullmatch(r"EMP\d{4,6}", v or ""):
            raise ValueError("emp_code must be EMP followed by 4 to 6 digits")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def shift_valid(cls, v):
        return valid_time(v)

    @field_validator("joined_on")
    @classmethod
    def joined_valid(cls, v):
        try:
            return parse_day(v).isoformat()
        except HTTPException as e:
            raise ValueError(str(e.detail))


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def status_valid(cls, v):
        if v not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class CorrectionIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return api_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_day(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "attendance already exists for this employee and date")
    return api_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(404, "no matching punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= as_utc(record["punch_in"]) or ts > as_utc(record["punch_in"]) + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    update = {"punch_out": ts}
    temp = dict(record)
    temp.update(update)
    compute_fields(temp, emp)
    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": {k: temp[k] for k in ("punch_out", "work_hours", "overtime_minutes", "half_day")}})
    if result.modified_count != 1:
        raise HTTPException(409, "attendance changed concurrently")
    temp.update(update)
    return api_doc(temp)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: parse_day(date_from)
    if date_to: parse_day(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status and status not in STATUSES:
        raise HTTPException(422, "invalid status")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    for d in docs:
        for k in ("punch_in", "punch_out"):
            if isinstance(d.get(k), datetime): d[k] = dt_to_ms(d[k])
        for h in d.get("history", []):
            if isinstance(h.get("at"), datetime): h["at"] = dt_to_ms(h["at"])
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{day}")
def correct_attendance(emp_code: str, day: str, body: CorrectionIn):
    parse_day(day)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not rec: raise HTTPException(404, "attendance record not found")
    status = body.status if body.status is not None else rec.get("status")
    if status not in STATUSES: raise HTTPException(422, "invalid status")
    pin = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    pout = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pin is not None and attendance_day(pin, emp["shift_start"], emp["shift_end"]) != day:
        raise HTTPException(422, "punch_in must belong to attendance date")
    if pout is not None and (pin is None or pout <= pin or pout > pin + timedelta(hours=24)):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    changes = {}
    for key, value in (("status", status), ("punch_in", pin), ("punch_out", pout)):
        old = rec.get(key)
        if isinstance(old, datetime): old = as_utc(old)
        if isinstance(value, datetime): value = as_utc(value)
        if old != value: changes[key] = value
    if not changes: raise HTTPException(422, "no changes supplied")
    temp = dict(rec); temp.update(changes); compute_fields(temp, emp)
    for k in ("late_minutes", "work_hours", "overtime_minutes", "half_day"): changes[k] = temp[k]
    hist_changes = {k: (dt_to_ms(v) if isinstance(v, datetime) else v) for k, v in changes.items() if k in {"status", "punch_in", "punch_out"}}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": hist_changes}
    result = db.attendance_logs.update_one({"_id": rec["_id"]}, {"$set": changes, "$push": {"history": entry}})
    if result.modified_count != 1: raise HTTPException(409, "attendance changed concurrently")
    rec.update(changes); rec.setdefault("history", []).append(entry)
    return api_doc(rec)


def month_bounds(month):
    first = parse_month(month)
    nxt = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    return first, nxt - timedelta(days=1)


def month_pipeline(emp_code, month):
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    start = max(first, parse_day(emp["joined_on"]))
    working_days = sum(1 for n in range((last - start).days + 1) if (start + timedelta(days=n)).weekday() < 5) if start <= last else 0
    raw = list(db.attendance_logs.aggregate(month_pipeline(emp_code, month)))
    agg = raw[0] if raw else {}
    present = agg.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": money_round(present),
            "leave_days": agg.get("leave_days", 0), "late_count": agg.get("late_count", 0),
            "total_late_minutes": agg.get("total_late_minutes", 0), "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
            "attendance_pct": money_round(present / working_days * 100, 2) if working_days else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = month_bounds(month)
    empq = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None: empq["department"] = department
    employees = list(db.employees.find(empq, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    grouped = {}
    for e in employees:
        grouped.setdefault(e["department"], []).append(e)
    out = []
    for dep, emps in sorted(grouped.items()):
        codes = [e["emp_code"] for e in emps]
        logs = list(db.attendance_logs.aggregate([
            {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            {"$group": {"_id": None,
                        "present_days": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                        "hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", None]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
        ]))
        a = logs[0] if logs else {}
        out.append({"department": dep, "month": month, "headcount": len(emps), "present_days": money_round(a.get("present_days", 0)),
                    "avg_work_hours": money_round(a["hours"]) if a.get("hours") is not None else None,
                    "late_count": a.get("late_count", 0), "total_late_minutes": a.get("total_late_minutes", 0),
                    "leave_count": a.get("leave_count", 0), "on_duty_count": a.get("on_duty_count", 0)})
    return {"items": out}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
                 {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
                 {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "rank": 1}}]
    return {"items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_day(from_date), parse_day(to_date)
    if end < start: raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92: raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department has no employees")
    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
    ]
    existing = {x["date"]: x for x in db.attendance_logs.aggregate(pipeline)}
    # Densify output calendar; per-day headcount comes from MongoDB query.
    emp_dates = list(db.employees.find({"department": department, "joined_on": {"$lte": to_date}}, {"_id": 0, "joined_on": 1}))
    rows = []
    for i in range((end - start).days + 1):
        day = start + timedelta(days=i); ds = day.isoformat()
        headcount = sum(1 for e in emp_dates if e["joined_on"] <= ds)
        item = existing.get(ds, {})
        present = item.get("present_count", 0); working = day.weekday() < 5
        rate = money_round(present / headcount * 100, 4) if working and headcount else None
        rows.append({"date": ds, "is_working_day": working, "headcount": headcount, "present_count": present,
                     "late_count": item.get("late_count", 0), "attendance_rate": rate})
    for i, row in enumerate(rows):
        vals = [x["attendance_rate"] for x in rows[max(0, i-6):i+1] if x["attendance_rate"] is not None]
        row["moving_avg_7d"] = money_round(sum(vals) / len(vals), 4) if vals else None
    return {"department": department, "from": from_date, "to": to_date, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, month: Optional[str] = None, emp_code: Optional[str] = None,
            department: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
            status: Optional[str] = None, page: int = 1, page_size: int = 20, limit: int = 10,
            from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to")):
    if endpoint not in EXPLAIN_ENDPOINTS: raise HTTPException(422, "unknown explain endpoint")
    if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"} and not month:
        raise HTTPException(422, "month is required")
    if endpoint == "employee_monthly" and not emp_code: raise HTTPException(422, "emp_code is required")
    if endpoint in {"department_summary", "late_leaderboard"} and department is None:
        # department filter is optional for these endpoints
        pass
    if endpoint == "department_trend" and (not department or not from_date or not to_date):
        raise HTTPException(422, "department, from, and to are required")
    if endpoint == "attendance_list":
        require_page(page, page_size)
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        result = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1},
                                       "skip": (page-1)*page_size, "limit": page_size}, verbosity="executionStats")
    elif endpoint == "employee_monthly":
        pipeline = month_pipeline(emp_code, month)
        result = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    elif endpoint == "department_summary":
        first, last = month_bounds(month)
        q = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}
        result = db.command("explain", {"aggregate": "attendance_logs", "pipeline": [{"$match": q}, {"$group": {"_id": "$emp_code", "total": {"$sum": 1}}}], "cursor": {}}, verbosity="executionStats")
    elif endpoint == "late_leaderboard":
        first, last = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
                    {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
        result = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    else:
        parse_day(from_date); parse_day(to_date)
        result = db.command("explain", {"aggregate": "attendance_logs", "pipeline": [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}], "cursor": {}}, verbosity="executionStats")
    return result
