"""AutoPilot PMO AI backend API (FastAPI)."""

from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import CREDIT, __version__
from app.config import load_settings
from app.container import Container, build_container
from app.core.redaction import redact
from app.core.states import LABELS, RunState
from app.observability.logging import configure_logging, get_logger
from app.workflows.approvals import ApprovalError

log = get_logger("api")


class RunIn(BaseModel):
    assignment: str = Field(min_length=5, max_length=4000)


class DecisionIn(BaseModel):
    decision: str = Field(pattern="(?i)^(approve|reject)$")
    approver: str = Field(min_length=2, max_length=80)
    comment: str = Field(default="", max_length=500)


def create_app(container: Container | None = None) -> FastAPI:
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = app.state.container
        purged = c.store.purge_older_than(c.settings.retention_days)
        resumed = c.runtime.recover()
        log.info(
            "startup",
            extra={
                "ctx": {"mode": c.settings.mode, "brain": c.settings.agent_brain, "purged_runs": purged, "resumed_runs": resumed}
            },
        )
        yield

    app = FastAPI(title="AutoPilot PMO AI", version=__version__, description=CREDIT, lifespan=lifespan)
    app.state.container = container or build_container(load_settings())

    def c() -> Container:
        return app.state.container

    def _run_or_404(run_id: str) -> dict:
        run = c().store.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        return run

    def _public_run(run: dict) -> dict:
        out = {k: v for k, v in run.items() if k not in ("brain_state",)}
        out["state_label"] = LABELS.get(RunState(run["state"]), run["state"])
        if run.get("pending_approval_id"):
            out["pending_approval"] = c().store.get_approval(run["pending_approval_id"])
        return redact(out)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    def config() -> dict:
        s = c().settings
        return {
            **s.public_view(),
            "version": __version__,
            "credit": CREDIT,
            "policy_version": c().policy.version,
            "agent_label": "Deterministic demo agent (simulation - not Claude)"
            if s.agent_brain != "claude"
            else f"Claude agent ({s.claude_model})",
            "execution_methods": {
                "api": "REST API (structured tools)",
                "ui": "Playwright Chromium browser automation (not native Claude Computer Use)",
            },
        }

    @app.get("/api/connection")
    def connection() -> dict:
        s = c().settings
        out = {"enterprise": c().toolbox.health()}
        if s.agent_brain == "claude":
            out["claude"] = {
                "ok": s.has_anthropic_credentials,
                "detail": "credentials present"
                if s.has_anthropic_credentials
                else "ANTHROPIC_API_KEY not set - Claude agent unavailable",
            }
        else:
            out["claude"] = {"ok": None, "detail": "not used in Demo Mode (deterministic simulated agent)"}
        return out

    @app.get("/api/policy")
    def policy() -> dict:
        return c().policy.document()

    @app.get("/api/capabilities")
    def capabilities() -> list[dict]:
        return [
            {"name": s.name, "method": s.method, "kind": s.kind, "description": s.description} for s in c().toolbox.capabilities()
        ]

    @app.post("/api/runs", status_code=201)
    def start_run(body: RunIn) -> dict:
        s = c().settings
        if s.agent_brain == "claude" and not s.has_anthropic_credentials:
            raise HTTPException(
                503, "Claude agent selected but ANTHROPIC_API_KEY is not configured. Use Demo Mode or add credentials."
            )
        try:
            run = c().runtime.start_run(body.assignment)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _public_run(run)

    @app.get("/api/runs")
    def list_runs(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
        return c().store.list_runs(limit)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = _run_or_404(run_id)
        _sweep_expired(run)
        return _public_run(c().store.get_run(run_id))

    def _sweep_expired(run: dict) -> None:
        """An approval that expires while pending can never be approved; let the run continue safely."""
        if run["state"] == "WAITING_APPROVAL" and run.get("pending_approval_id"):
            a = c().store.get_approval(run["pending_approval_id"])
            if a and c().approvals.expire_if_needed(a)["status"] == "EXPIRED":
                c().runtime.resume_after_decision(run["id"])

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0) -> list[dict]:
        _run_or_404(run_id)
        return c().store.list_events(run_id, after)

    @app.post("/api/runs/{run_id}/{op}")
    def control(run_id: str, op: str) -> dict:
        _run_or_404(run_id)
        rt = c().runtime
        try:
            if op == "pause":
                return _public_run(rt.request_pause(run_id))
            if op == "resume":
                return _public_run(rt.request_resume(run_id))
            if op == "cancel":
                return _public_run(rt.request_cancel(run_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        raise HTTPException(404, "unknown operation")

    @app.get("/api/runs/{run_id}/audit")
    def audit(run_id: str) -> dict:
        run = _run_or_404(run_id)
        st = c().store
        return redact(
            {
                "run": {k: v for k, v in run.items() if k != "brain_state"},
                "events": st.list_events(run_id),
                "approvals": st.list_approvals(run_id=run_id),
                "executions": st.list_executions(run_id),
                "policy_version": c().policy.version,
                "generated_by": CREDIT,
            }
        )

    @app.get("/api/approvals")
    def approvals(status: str | None = None) -> list[dict]:
        items = c().store.list_approvals(status=status.upper() if status else None)
        return [c().approvals.expire_if_needed(a) for a in items]

    @app.get("/api/approvals/{approval_id}")
    def approval(approval_id: str) -> dict:
        a = c().store.get_approval(approval_id)
        if not a:
            raise HTTPException(404, "approval not found")
        return c().approvals.expire_if_needed(a)

    @app.post("/api/approvals/{approval_id}/decision")
    def decide(approval_id: str, body: DecisionIn, x_approver_key: Annotated[str | None, Header()] = None) -> dict:
        try:
            a = c().approvals.decide(approval_id, body.decision, body.approver, x_approver_key, body.comment)
        except ApprovalError as exc:
            if exc.status == 410:
                existing = c().store.get_approval(approval_id)
                _sweep_expired(c().store.get_run(existing["run_id"]))
            raise HTTPException(exc.status, str(exc)) from exc
        run = c().store.get_run(a["run_id"])
        c().store.add_event(
            a["run_id"],
            "APPROVAL_DECISION",
            state=run["state"],
            action=a["action"],
            result=a["status"],
            message=f"{a['status']} by {a['decided_by']}" + (f": {a['decision_comment']}" if a.get("decision_comment") else ""),
            detail={"approval_id": a["id"]},
        )
        c().runtime.resume_after_decision(a["run_id"])
        return a

    @app.get("/api/diagnostics")
    def diagnostics(limit: int = Query(100, ge=1, le=500)) -> dict:
        return {
            "recent_events": c().store.recent_events(limit),
            "limits": c().settings.limits.__dict__,
            "pending_approvals": len(c().store.list_approvals(status="PENDING")),
        }

    @app.get("/api/screenshots/{run_id}/{name}")
    def screenshot(run_id: str, name: str) -> FileResponse:
        base = c().settings.screenshot_dir.resolve()
        path = (base / run_id / name).resolve()
        if base not in path.parents or path.suffix != ".png" or not path.is_file():
            raise HTTPException(404, "screenshot not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/enterprise/snapshot")
    def snapshot(project_id: str = Query("PRJ-1001", pattern="^[A-Za-z0-9-]{3,32}$")) -> dict:
        """Read-only view of enterprise records for the operator UI (not part of any run)."""
        s = c().settings
        if s.enterprise != "demo":
            raise HTTPException(501, "snapshot is available for the demo enterprise app only")
        headers = {"Authorization": f"Bearer {s.enterprise_api_token}"}
        out = {}
        try:
            with httpx.Client(base_url=s.enterprise_base_url, headers=headers, timeout=10) as h:
                for key, path in (
                    ("project", f"/api/projects/{project_id}"),
                    ("tasks", f"/api/projects/{project_id}/tasks"),
                    ("milestones", f"/api/projects/{project_id}/milestones"),
                    ("purchase_requests", f"/api/projects/{project_id}/purchase-requests"),
                    ("status_reports", f"/api/projects/{project_id}/status-reports"),
                ):
                    r = h.get(path)
                    r.raise_for_status()
                    out[key] = r.json()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"enterprise application unreachable: {type(exc).__name__}") from exc
        return out

    return app


def app_factory() -> FastAPI:  # uvicorn --factory entry point
    return create_app()
