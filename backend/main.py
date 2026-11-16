import os
import secrets
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import BigInteger, Column, Date, DateTime, Integer, MetaData, String, Table, URL, create_engine, func, select, text

load_dotenv()
DEMO = os.getenv("SPENDLENS_DEMO", "1") == "1"
TOKEN = os.getenv("SPENDLENS_TOKEN", "")
database = os.getenv("DATABASE_URL")
if not database and os.getenv("PGHOST"):
    database = URL.create("postgresql+psycopg", username=os.environ["PGUSER"], password=os.getenv("PGPASSWORD"),
                          host=os.environ["PGHOST"], database=os.environ["PGDATABASE"])
if not database:
    folder = Path(os.getenv("SPENDLENS_DATA", ".data"))
    folder.mkdir(parents=True, exist_ok=True)
    database = f"sqlite:///{folder / 'spend.db'}"
engine = create_engine(database, pool_pre_ping=True)
schema = MetaData()
rates = Table("rates", schema,
    Column("model", String(80), primary_key=True), Column("effective", Date, primary_key=True),
    Column("input_rate", Integer, nullable=False), Column("output_rate", Integer, nullable=False))
events = Table("usage_events", schema,
    Column("tenant", String(80), primary_key=True), Column("request_id", String(80), primary_key=True),
    Column("model", String(80), nullable=False), Column("feature", String(80), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False), Column("day", Date, nullable=False, index=True),
    Column("input_tokens", Integer, nullable=False), Column("output_tokens", Integer, nullable=False),
    Column("price_date", Date, nullable=False), Column("cost_nanos", BigInteger, nullable=False))
budgets = Table("budgets", schema,
    Column("tenant", String(80), primary_key=True), Column("month", Date, primary_key=True),
    Column("limit_nanos", BigInteger, nullable=False))
alerts = Table("alerts", schema,
    Column("tenant", String(80), primary_key=True), Column("month", Date, primary_key=True),
    Column("threshold", Integer, primary_key=True), Column("spend_nanos", BigInteger, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False))


def today():
    return datetime.now(timezone.utc).date()


@contextmanager
def write(lock):
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock))"), {"lock": lock})
        yield conn


def authorized(authorization: str = Header(default="")):
    if TOKEN and not secrets.compare_digest(authorization.encode(), ("Bearer " + TOKEN).encode()):
        raise HTTPException(401, "Enter a valid workspace token")


Slug = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")]
TokenCount = Annotated[int, Field(ge=0, le=10_000_000)]


class Usage(BaseModel):
    tenant: Slug
    request_id: Slug
    model: Slug
    feature: Slug
    at: AwareDatetime
    input_tokens: TokenCount
    output_tokens: TokenCount


class Rate(BaseModel):
    model: Slug
    effective: date
    input_rate: int = Field(ge=0, le=100_000_000)
    output_rate: int = Field(ge=0, le=100_000_000)


class Budget(BaseModel):
    tenant: Slug
    month: date
    limit_cents: int = Field(gt=0, le=100_000_000)


def month_bounds(value):
    start = value.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1)


def check_budget(conn, tenant, day):
    month, end = month_bounds(day)
    limit = conn.scalar(select(budgets.c.limit_nanos).where(budgets.c.tenant == tenant, budgets.c.month == month))
    if limit is None:
        return
    spend = conn.scalar(select(func.coalesce(func.sum(events.c.cost_nanos), 0)).where(
        events.c.tenant == tenant, events.c.day >= month, events.c.day < end))
    recorded = set(conn.scalars(select(alerts.c.threshold).where(alerts.c.tenant == tenant, alerts.c.month == month)))
    for threshold in (80, 100):
        if threshold not in recorded and spend * 100 >= limit * threshold:
            conn.execute(alerts.insert().values(tenant=tenant, month=month, threshold=threshold,
                         spend_nanos=spend, at=datetime.now(timezone.utc)))


def ingest(conn, usage):
    data = usage.model_dump()
    data["at"] = usage.at.astimezone(timezone.utc)
    existing = conn.execute(events.select().where(events.c.tenant == usage.tenant,
                            events.c.request_id == usage.request_id)).mappings().first()
    if existing is not None:
        actual = {key: existing[key] for key in data}
        actual["at"] = actual["at"].replace(tzinfo=timezone.utc) if actual["at"].tzinfo is None else actual["at"]
        if data != actual:
            raise HTTPException(409, "Request ID already exists with different usage")
        return existing["cost_nanos"], False
    day = data["at"].date()
    price = conn.execute(rates.select().where(rates.c.model == usage.model, rates.c.effective <= day)
                         .order_by(rates.c.effective.desc()).limit(1)).mappings().first()
    if price is None:
        raise HTTPException(422, "No rate available for this model at the event date")
    # USD per million tokens in mills converts directly to USD nanounits per token.
    cost = usage.input_tokens * price["input_rate"] + usage.output_tokens * price["output_rate"]
    conn.execute(events.insert().values(**data, day=day, price_date=price["effective"], cost_nanos=cost))
    check_budget(conn, usage.tenant, day)
    return cost, True


def seed():
    anchor = today()
    with write("tenant:northstar") as conn:
        if conn.scalar(select(func.count()).select_from(events)):
            return
        start = date(2020, 1, 1)
        conn.execute(rates.insert(), [
            {"model": "atlas-mini", "effective": start, "input_rate": 500, "output_rate": 2000},
            {"model": "atlas-pro", "effective": start, "input_rate": 5000, "output_rate": 20000},
        ])
        month, _ = month_bounds(anchor)
        conn.execute(budgets.insert().values(tenant="northstar", month=month, limit_nanos=50_000_000_000))
        for offset in range(29, -1, -1):
            day = anchor - timedelta(days=offset)
            spike_day = anchor - timedelta(days=min(2, anchor.day - 1))
            for tenant, feature in (("northstar", "document-summary"), ("meridian", "support-chat"), ("harbor", "classification")):
                for index in range(3):
                    spike = day == spike_day and tenant == "northstar"
                    ingest(conn, Usage(tenant=tenant, request_id=f"demo-{day}-{index}", model="atlas-pro" if spike else "atlas-mini",
                        feature=feature, at=datetime(day.year, day.month, day.day, 10 + index, tzinfo=timezone.utc),
                        input_tokens=2_000_000 if spike else 150_000 + ((day.day + index * 13) % 7) * 32_000,
                        output_tokens=500_000 if spike else 22_000 + index * 11_000))


@asynccontextmanager
async def lifespan(app):
    if not DEMO and len(TOKEN) < 24:
        raise RuntimeError("Set SPENDLENS_TOKEN to at least 24 characters outside demo mode")
    with write("schema") as conn:
        schema.create_all(conn)
    if DEMO:
        seed()
    yield


app = FastAPI(title="SpendLens", lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"status": "ok", "demo": DEMO}


@app.post("/api/usage", dependencies=[Depends(authorized)])
def add_usage(usage: Usage, response: Response):
    with write("tenant:" + usage.tenant) as conn:
        cost, created = ingest(conn, usage)
    response.status_code = 201 if created else 200
    return {"cost_nanos": cost, "created": created}


@app.post("/api/rates", status_code=201, dependencies=[Depends(authorized)])
def add_rate(rate: Rate):
    with write("rates") as conn:
        if conn.execute(rates.select().where(rates.c.model == rate.model, rates.c.effective == rate.effective)).first():
            raise HTTPException(409, "This rate version already exists")
        conn.execute(rates.insert().values(**rate.model_dump()))
    return rate


@app.put("/api/budgets", dependencies=[Depends(authorized)])
def set_budget(budget: Budget):
    if budget.month.day != 1:
        raise HTTPException(422, "Use the first day of the month")
    key = (budgets.c.tenant == budget.tenant, budgets.c.month == budget.month)
    with write("tenant:" + budget.tenant) as conn:
        if not conn.execute(budgets.update().where(*key).values(limit_nanos=budget.limit_cents * 10_000_000)).rowcount:
            conn.execute(budgets.insert().values(tenant=budget.tenant, month=budget.month,
                         limit_nanos=budget.limit_cents * 10_000_000))
        check_budget(conn, budget.tenant, budget.month)
    return budget


@app.get("/api/dashboard", dependencies=[Depends(authorized)])
def dashboard(tenant: Slug | None = None, feature: Slug | None = None, model: Slug | None = None,
              day: date | None = None, page: int = Query(1, ge=1, le=10000)):
    end = today()
    start = end - timedelta(days=29)
    conditions = [events.c.day >= start, events.c.day <= end]
    for column, value in ((events.c.tenant, tenant), (events.c.feature, feature), (events.c.model, model)):
        if value:
            conditions.append(column == value)
    cost = func.sum(events.c.cost_nanos)
    with engine.connect() as conn:
        def group(column):
            return [{"key": key, "cost_nanos": amount} for key, amount in conn.execute(
                select(column, cost).where(*conditions).group_by(column).order_by(cost.desc(), column)).all()]
        timeline = {row["key"]: row["cost_nanos"] for row in group(events.c.day)}
        breakdown = {key: group(column) for key, column in (
            ("tenant", events.c.tenant), ("feature", events.c.feature), ("model", events.c.model))}
        options = {column.name: list(conn.scalars(select(column).distinct().order_by(column)))
                   for column in (events.c.tenant, events.c.feature, events.c.model)}
        query = events.select().where(*conditions)
        if day:
            query = query.where(events.c.day == day)
        rows = [dict(row) for row in conn.execute(query.order_by(events.c.at.desc(), events.c.tenant, events.c.request_id)
                .offset((page - 1) * 15).limit(16)).mappings()]
        month, _ = month_bounds(end)
        budget_query = budgets.select().where(budgets.c.month == month)
        alert_query = alerts.select().where(alerts.c.month == month)
        if tenant:
            budget_query = budget_query.where(budgets.c.tenant == tenant)
            alert_query = alert_query.where(alerts.c.tenant == tenant)
        current_alerts = list(map(dict, conn.execute(alert_query.order_by(alerts.c.threshold.desc())).mappings()))
        current_budgets = list(map(dict, conn.execute(budget_query.order_by(budgets.c.tenant)).mappings()))
        for budget in current_budgets:
            budget["spend_nanos"] = conn.scalar(select(func.coalesce(cost, 0)).where(
                events.c.tenant == budget["tenant"], events.c.day >= month, events.c.day <= end))
    return {"total_nanos": sum(timeline.values()), "timeline": [
                {"day": start + timedelta(days=i), "cost_nanos": timeline.get(start + timedelta(days=i), 0)}
                for i in range(30)],
            "breakdown": breakdown, "options": options, "events": rows[:15], "has_next": len(rows) > 15,
            "alerts": current_alerts, "budgets": current_budgets}


dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if dist.is_dir():
    app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
