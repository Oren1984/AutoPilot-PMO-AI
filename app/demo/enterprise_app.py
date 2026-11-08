"""DemoCorp Enterprise Suite - a small simulated enterprise application.

It exposes BOTH a structured REST API (/api/...) and a real server-rendered browser UI (/ui/...).
Some capabilities (weekly status reports) exist only in the UI, which is why the agent needs
browser automation in addition to the API. Payments are SIMULATED: nothing leaves this process.
"""

import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.demo.seed import build_seed

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

TASK_STATUSES = ["Not Started", "In Progress", "At Risk", "Blocked", "Done", "Cancelled"]
HEALTH_VALUES = ["On Track", "At Risk", "Off Track"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT, status TEXT, owner TEXT, start_date TEXT,
  end_date TEXT, budget_total REAL, budget_committed REAL, currency TEXT, health TEXT, version INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS milestones (id TEXT PRIMARY KEY, project_id TEXT, name TEXT, due_date TEXT, status TEXT,
  health TEXT, version INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, project_id TEXT, milestone_id TEXT, title TEXT, description TEXT,
  owner TEXT, due_date TEXT, status TEXT, priority TEXT, version INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS purchase_requests (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, vendor TEXT,
  recipient_account_masked TEXT, amount REAL, currency TEXT, justification TEXT, requested_by TEXT, status TEXT,
  version INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, entity_type TEXT, entity_id TEXT, author TEXT, body TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS status_reports (id TEXT PRIMARY KEY, project_id TEXT, rag TEXT, summary TEXT, author TEXT,
  channel TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, purchase_request_id TEXT, amount REAL, currency TEXT,
  recipient TEXT, simulated INTEGER, idempotency_key TEXT UNIQUE, approval_ref TEXT, executed_at TEXT);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, entity TEXT,
  detail TEXT);
"""

TABLES = ["projects", "milestones", "tasks", "purchase_requests", "users", "comments", "status_reports", "payments", "audit"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class EnterpriseDB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        if not self.one("SELECT id FROM projects LIMIT 1"):
            self.reset()

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def reset(self, today: date | None = None) -> None:
        seed = build_seed(today)
        with self.lock:
            for table in TABLES:
                self.conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table list
            for table, rows in seed.items():
                for row in rows:
                    cols = ",".join(row.keys())
                    marks = ",".join("?" for _ in row)
                    self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))  # noqa: S608
        self.audit("system", "reset", "*", {"today": (today or date.today()).isoformat()})

    def audit(self, actor: str, action: str, entity: str, detail: dict) -> None:
        self.exec(
            "INSERT INTO audit (ts, actor, action, entity, detail) VALUES (?,?,?,?,?)",
            (_now(), actor, action, entity, json.dumps(detail, default=str)),
        )


# --------------------------------------------------------------------------------------------
class TaskStatusUpdate(BaseModel):
    status: str


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    author: str = Field(default="AutoPilot Agent", max_length=80)


class MilestoneHealthUpdate(BaseModel):
    health: str


class PaymentIn(BaseModel):
    amount: float = Field(gt=0)
    currency: str
    recipient: str
    approval_ref: str = Field(min_length=3, max_length=64)


class PurchaseRequestAdminUpdate(BaseModel):
    amount: float | None = None
    vendor: str | None = None


class RoleUpdate(BaseModel):
    role: str


def create_enterprise_app(
    db_path: str | Path | None = None, service_token: str | None = None, admin_token: str | None = None
) -> FastAPI:
    db = EnterpriseDB(db_path or os.environ.get("ENTERPRISE_DB_PATH", "data/enterprise.db"))
    svc_token = service_token or os.environ.get("ENTERPRISE_API_TOKEN", "demo-service-token")
    adm_token = admin_token or os.environ.get("ENTERPRISE_ADMIN_TOKEN", "demo-admin-token")
    sessions: dict[str, str] = {}

    app = FastAPI(title="DemoCorp Enterprise Suite (simulated)", version="1.0.0", docs_url="/api/docs")
    app.state.db = db

    def _check(authorization: str | None, allowed: list[tuple[str, str]]) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        for value, role in allowed:
            if hmac.compare_digest(token, value):
                return role
        raise HTTPException(403, "token not permitted for this operation")

    def service_auth(authorization: Annotated[str | None, Header()] = None) -> str:
        return _check(authorization, [(svc_token, "service"), (adm_token, "admin")])

    def admin_auth(authorization: Annotated[str | None, Header()] = None) -> str:
        return _check(authorization, [(adm_token, "admin")])

    def _get(table: str, rid: str) -> dict:
        row = db.one(f"SELECT * FROM {table} WHERE id=?", (rid,))  # noqa: S608 - internal table names
        if not row:
            raise HTTPException(404, f"{table[:-1]} {rid} not found")
        return row

    def _check_version(row: dict, if_match: str | None) -> None:
        if if_match is None:
            raise HTTPException(428, "If-Match (record version) header is required for writes")
        if str(row["version"]) != str(if_match).strip('"'):
            raise HTTPException(409, f"version conflict: record is at version {row['version']}, request had {if_match}")

    # ------------------------------ API ------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "app": "DemoCorp Enterprise Suite", "simulated": True}

    @app.get("/api/projects")
    def list_projects(_: Annotated[str, Depends(service_auth)], status: str | None = None) -> list[dict]:
        if status:
            return db.all("SELECT * FROM projects WHERE lower(status)=lower(?) ORDER BY id", (status,))
        return db.all("SELECT * FROM projects ORDER BY id")

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, _: Annotated[str, Depends(service_auth)]) -> dict:
        p = _get("projects", project_id)
        p["budget_remaining"] = round(p["budget_total"] - p["budget_committed"], 2)
        return p

    @app.get("/api/projects/{project_id}/tasks")
    def list_tasks(project_id: str, _: Annotated[str, Depends(service_auth)]) -> list[dict]:
        _get("projects", project_id)
        return db.all("SELECT * FROM tasks WHERE project_id=? ORDER BY id", (project_id,))

    @app.get("/api/projects/{project_id}/milestones")
    def list_milestones(project_id: str, _: Annotated[str, Depends(service_auth)]) -> list[dict]:
        _get("projects", project_id)
        return db.all("SELECT * FROM milestones WHERE project_id=? ORDER BY due_date", (project_id,))

    @app.get("/api/projects/{project_id}/purchase-requests")
    def list_prs(project_id: str, _: Annotated[str, Depends(service_auth)]) -> list[dict]:
        _get("projects", project_id)
        return db.all("SELECT * FROM purchase_requests WHERE project_id=? ORDER BY id", (project_id,))

    @app.get("/api/projects/{project_id}/status-reports")
    def list_reports(project_id: str, _: Annotated[str, Depends(service_auth)]) -> list[dict]:
        _get("projects", project_id)
        return db.all("SELECT * FROM status_reports WHERE project_id=? ORDER BY created_at DESC", (project_id,))

    @app.get("/api/purchase-requests/{pr_id}")
    def get_pr(pr_id: str, _: Annotated[str, Depends(service_auth)]) -> dict:
        pr = _get("purchase_requests", pr_id)
        pr["payments"] = db.all("SELECT * FROM payments WHERE purchase_request_id=?", (pr_id,))
        return pr

    @app.get("/api/tasks/{task_id}/comments")
    def task_comments(task_id: str, _: Annotated[str, Depends(service_auth)]) -> list[dict]:
        return db.all("SELECT * FROM comments WHERE entity_type='task' AND entity_id=? ORDER BY created_at", (task_id,))

    @app.patch("/api/tasks/{task_id}")
    def update_task(
        task_id: str,
        body: TaskStatusUpdate,
        role: Annotated[str, Depends(service_auth)],
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict:
        task = _get("tasks", task_id)
        _check_version(task, if_match)
        if body.status not in TASK_STATUSES:
            raise HTTPException(422, f"invalid status; allowed: {TASK_STATUSES}")
        db.exec("UPDATE tasks SET status=?, version=version+1 WHERE id=?", (body.status, task_id))
        db.audit(role, "task.status", task_id, {"from": task["status"], "to": body.status})
        return _get("tasks", task_id)

    @app.post("/api/tasks/{task_id}/comments", status_code=201)
    def add_comment(task_id: str, body: CommentIn, role: Annotated[str, Depends(service_auth)]) -> dict:
        _get("tasks", task_id)
        cid = "C-" + uuid.uuid4().hex[:8].upper()
        db.exec(
            "INSERT INTO comments (id, entity_type, entity_id, author, body, created_at) VALUES (?,?,?,?,?,?)",
            (cid, "task", task_id, body.author, body.body, _now()),
        )
        db.audit(role, "task.comment", task_id, {"comment_id": cid})
        return db.one("SELECT * FROM comments WHERE id=?", (cid,))

    @app.patch("/api/milestones/{milestone_id}")
    def update_milestone(
        milestone_id: str,
        body: MilestoneHealthUpdate,
        role: Annotated[str, Depends(service_auth)],
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict:
        ms = _get("milestones", milestone_id)
        _check_version(ms, if_match)
        if body.health not in HEALTH_VALUES:
            raise HTTPException(422, f"invalid health; allowed: {HEALTH_VALUES}")
        db.exec("UPDATE milestones SET health=?, version=version+1 WHERE id=?", (body.health, milestone_id))
        db.audit(role, "milestone.health", milestone_id, {"from": ms["health"], "to": body.health})
        return _get("milestones", milestone_id)

    @app.post("/api/purchase-requests/{pr_id}/payments", status_code=201)
    def execute_payment(
        pr_id: str,
        body: PaymentIn,
        role: Annotated[str, Depends(service_auth)],
        if_match: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict:
        """SIMULATED payment execution. Records a payment row; no funds move anywhere."""
        if not idempotency_key:
            raise HTTPException(428, "Idempotency-Key header is required for payments")
        existing = db.one("SELECT * FROM payments WHERE idempotency_key=?", (idempotency_key,))
        if existing:
            return {"payment": existing, "replayed": True, "purchase_request": _get("purchase_requests", pr_id)}
        pr = _get("purchase_requests", pr_id)
        _check_version(pr, if_match)
        if pr["status"] != "Pending Approval":
            raise HTTPException(409, f"purchase request is '{pr['status']}', not payable")
        if abs(pr["amount"] - body.amount) > 0.005 or pr["currency"] != body.currency or pr["vendor"] != body.recipient:
            raise HTTPException(409, "payment parameters do not match the purchase request")
        project = _get("projects", pr["project_id"])
        if project["budget_committed"] + pr["amount"] > project["budget_total"]:
            raise HTTPException(409, "insufficient remaining project budget")
        pay_id = "PAY-" + uuid.uuid4().hex[:8].upper()
        with db.lock:
            db.exec(
                "INSERT INTO payments (id, purchase_request_id, amount, currency, recipient, simulated, idempotency_key, "
                "approval_ref, executed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pay_id, pr_id, pr["amount"], pr["currency"], pr["vendor"], 1, idempotency_key, body.approval_ref, _now()),
            )
            db.exec("UPDATE purchase_requests SET status='Paid (Simulated)', version=version+1 WHERE id=?", (pr_id,))
            db.exec(
                "UPDATE projects SET budget_committed=budget_committed+?, version=version+1 WHERE id=?",
                (pr["amount"], pr["project_id"]),
            )
        db.audit(role, "payment.simulated", pr_id, {"payment_id": pay_id, "approval_ref": body.approval_ref})
        return {
            "payment": db.one("SELECT * FROM payments WHERE id=?", (pay_id,)),
            "replayed": False,
            "purchase_request": _get("purchase_requests", pr_id),
        }

    @app.post("/api/users/{user_id}/role")
    def change_role(user_id: str, body: RoleUpdate, role: Annotated[str, Depends(admin_auth)]) -> dict:
        _get("users", user_id)
        db.exec("UPDATE users SET role=? WHERE id=?", (body.role, user_id))
        db.audit(role, "user.role", user_id, {"role": body.role})
        return _get("users", user_id)

    # admin-only endpoints (used by operators/tests, never exposed to the agent)
    @app.post("/api/admin/reset")
    def admin_reset(_: Annotated[str, Depends(admin_auth)]) -> dict:
        db.reset()
        sessions.clear()
        return {"status": "reset"}

    @app.patch("/api/admin/purchase-requests/{pr_id}")
    def admin_update_pr(pr_id: str, body: PurchaseRequestAdminUpdate, role: Annotated[str, Depends(admin_auth)]) -> dict:
        pr = _get("purchase_requests", pr_id)
        amount = body.amount if body.amount is not None else pr["amount"]
        vendor = body.vendor if body.vendor is not None else pr["vendor"]
        db.exec("UPDATE purchase_requests SET amount=?, vendor=?, version=version+1 WHERE id=?", (amount, vendor, pr_id))
        db.audit(role, "purchase_request.edit", pr_id, {"amount": amount, "vendor": vendor})
        return _get("purchase_requests", pr_id)

    @app.get("/api/admin/audit")
    def admin_audit(_: Annotated[str, Depends(admin_auth)]) -> list[dict]:
        return db.all("SELECT * FROM audit ORDER BY id DESC LIMIT 200")

    # ------------------------------ UI ------------------------------
    def ui_user(request: Request) -> str | None:
        sid = request.cookies.get("ent_session")
        return sessions.get(sid) if sid else None

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {"user": ui_user(request), "today": date.today().isoformat(), **ctx})

    def require_ui(request: Request) -> str:
        user = ui_user(request)
        if not user:
            raise HTTPException(303, headers={"Location": "/ui/login"})
        return user

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/", status_code=303)

    @app.get("/ui/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return render(request, "login.html", error=None)

    @app.post("/ui/session")
    def create_session(request: Request, token: Annotated[str, Form()]) -> RedirectResponse:
        role = None
        for value, r in ((svc_token, "AutoPilot Agent (service)"), (adm_token, "PMO Administrator")):
            if hmac.compare_digest(token.strip(), value):
                role = r
        if not role:
            return render(request, "login.html", error="Invalid access token")
        sid = secrets.token_urlsafe(24)
        sessions[sid] = role
        resp = RedirectResponse("/ui/", status_code=303)
        resp.set_cookie("ent_session", sid, httponly=True, samesite="strict")
        return resp

    @app.get("/ui/", response_class=HTMLResponse)
    def ui_projects(request: Request, user: Annotated[str, Depends(require_ui)]) -> HTMLResponse:
        return render(request, "projects.html", projects=db.all("SELECT * FROM projects ORDER BY id"))

    @app.get("/ui/projects/{project_id}", response_class=HTMLResponse)
    def ui_project(request: Request, project_id: str, user: Annotated[str, Depends(require_ui)]) -> HTMLResponse:
        project = _get("projects", project_id)
        return render(
            request,
            "project.html",
            project=project,
            tasks=db.all("SELECT * FROM tasks WHERE project_id=? ORDER BY id", (project_id,)),
            milestones=db.all("SELECT * FROM milestones WHERE project_id=? ORDER BY due_date", (project_id,)),
            prs=db.all("SELECT * FROM purchase_requests WHERE project_id=? ORDER BY id", (project_id,)),
            reports=db.all("SELECT * FROM status_reports WHERE project_id=? ORDER BY created_at DESC", (project_id,)),
            flash=request.query_params.get("flash"),
        )

    @app.get("/ui/projects/{project_id}/status-report/new", response_class=HTMLResponse)
    def ui_new_report(request: Request, project_id: str, user: Annotated[str, Depends(require_ui)]) -> HTMLResponse:
        return render(request, "status_report_form.html", project=_get("projects", project_id), error=None)

    @app.post("/ui/projects/{project_id}/status-report")
    def ui_submit_report(
        request: Request,
        project_id: str,
        user: Annotated[str, Depends(require_ui)],
        rag: Annotated[str, Form()],
        summary: Annotated[str, Form()],
    ):
        project = _get("projects", project_id)
        if rag not in ("Green", "Amber", "Red") or not summary.strip() or len(summary) > 4000:
            return render(request, "status_report_form.html", project=project, error="RAG and summary are required")
        rid = "SR-" + uuid.uuid4().hex[:6].upper()
        db.exec(
            "INSERT INTO status_reports (id, project_id, rag, summary, author, channel, created_at) VALUES (?,?,?,?,?,?,?)",
            (rid, project_id, rag, summary.strip(), user, "ui", _now()),
        )
        db.audit(user, "status_report.submit", project_id, {"report_id": rid, "rag": rag})
        return RedirectResponse(f"/ui/projects/{project_id}?flash=Status+report+{rid}+submitted", status_code=303)

    @app.get("/ui/purchase-requests/{pr_id}", response_class=HTMLResponse)
    def ui_pr(request: Request, pr_id: str, user: Annotated[str, Depends(require_ui)]) -> HTMLResponse:
        pr = _get("purchase_requests", pr_id)
        return render(request, "purchase_request.html", pr=pr, project=_get("projects", pr["project_id"]))

    return app


def app_factory() -> FastAPI:  # uvicorn --factory entry point
    return create_enterprise_app()
