"""Structured REST adapter for the DemoCorp enterprise application (Method A - API)."""

from __future__ import annotations

import httpx

from app.adapters.base import ActionSpec, AdapterError, AdapterResult, I, S, schema

ID = {"type": "string", "pattern": "^[A-Z]+-[0-9A-Z]+$", "maxLength": 32}

SPECS = [
    ActionSpec("list_projects", "List projects. Optional status filter, e.g. 'Active'.", schema({"status": S}), "api"),
    ActionSpec(
        "get_project", "Get one project including budget and remaining budget.", schema({"project_id": ID}, ["project_id"]), "api"
    ),
    ActionSpec(
        "list_tasks",
        "List tasks of a project (status, owner, due date, version).",
        schema({"project_id": ID}, ["project_id"]),
        "api",
    ),
    ActionSpec("list_milestones", "List milestones of a project.", schema({"project_id": ID}, ["project_id"]), "api"),
    ActionSpec(
        "list_purchase_requests",
        "List purchase / payment approval requests of a project.",
        schema({"project_id": ID}, ["project_id"]),
        "api",
    ),
    ActionSpec("get_purchase_request", "Get a purchase request with its payments.", schema({"pr_id": ID}, ["pr_id"]), "api"),
    ActionSpec(
        "list_status_reports",
        "List submitted weekly status reports of a project.",
        schema({"project_id": ID}, ["project_id"]),
        "api",
    ),
    ActionSpec(
        "update_task_status",
        "Update a task status. Requires the record version you last read (optimistic locking).",
        schema(
            {
                "task_id": ID,
                "status": {"type": "string", "enum": ["Not Started", "In Progress", "At Risk", "Blocked", "Done", "Cancelled"]},
                "expected_version": I,
            },
            ["task_id", "status", "expected_version"],
        ),
        "api",
        "write",
    ),
    ActionSpec(
        "add_task_comment",
        "Add a comment to a task (e.g. a proposed next action or an escalation note).",
        schema({"task_id": ID, "body": {"type": "string", "maxLength": 4000}}, ["task_id", "body"]),
        "api",
        "write",
    ),
    ActionSpec(
        "update_milestone_health",
        "Update a milestone health indicator. Requires the record version you last read.",
        schema(
            {
                "milestone_id": ID,
                "health": {"type": "string", "enum": ["On Track", "At Risk", "Off Track"]},
                "expected_version": I,
            },
            ["milestone_id", "health", "expected_version"],
        ),
        "api",
        "write",
    ),
    ActionSpec(
        "execute_payment",
        "Execute the (SIMULATED) payment of an approved purchase request. Financial commitment: always requires "
        "explicit human approval; the call will pause the run until a human decides.",
        schema(
            {
                "pr_id": ID,
                "amount": {"type": "number", "minimum": 0.01},
                "currency": {"type": "string", "maxLength": 3},
                "recipient": S,
                "expected_version": I,
            },
            ["pr_id", "amount", "currency", "recipient", "expected_version"],
        ),
        "api",
        "financial",
    ),
    ActionSpec(
        "change_user_role",
        "Change an enterprise user's security role.",
        schema({"user_id": ID, "role": S}, ["user_id", "role"]),
        "api",
        "admin",
    ),
]


class DemoApiAdapter:
    name = "demo-enterprise-api"

    def __init__(self, base_url: str, token: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "AutoPilot-PMO-AI/1.0"},
            timeout=timeout,
            transport=transport,
        )

    def capabilities(self) -> list[ActionSpec]:
        return list(SPECS)

    def health(self) -> dict:
        try:
            r = self._client.get("/api/health")
            return {"ok": r.status_code == 200, "detail": r.json() if r.status_code == 200 else r.text[:200]}
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"cannot reach enterprise API: {type(exc).__name__}"}

    def _call(self, method: str, url: str, *, write: bool = False, **kw) -> dict | list:
        try:
            r = self._client.request(method, url, **kw)
        except httpx.TimeoutException as exc:
            # A timed-out write may or may not have been applied.
            raise AdapterError(f"timeout calling {url}", retryable=not write, uncertain=write) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"connection error calling {url}: {type(exc).__name__}", retryable=True) from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise AdapterError(f"{r.status_code}: {detail}", status=r.status_code, retryable=r.status_code >= 500 and not write)
        return r.json()

    def execute(self, action: str, params: dict, context: dict) -> AdapterResult:
        p = params
        match action:
            case "list_projects":
                data = self._call("GET", "/api/projects", params={"status": p["status"]} if p.get("status") else None)
            case "get_project":
                data = self._call("GET", f"/api/projects/{p['project_id']}")
            case "list_tasks":
                data = self._call("GET", f"/api/projects/{p['project_id']}/tasks")
            case "list_milestones":
                data = self._call("GET", f"/api/projects/{p['project_id']}/milestones")
            case "list_purchase_requests":
                data = self._call("GET", f"/api/projects/{p['project_id']}/purchase-requests")
            case "get_purchase_request":
                data = self._call("GET", f"/api/purchase-requests/{p['pr_id']}")
            case "list_status_reports":
                data = self._call("GET", f"/api/projects/{p['project_id']}/status-reports")
            case "update_task_status":
                data = self._call(
                    "PATCH",
                    f"/api/tasks/{p['task_id']}",
                    write=True,
                    json={"status": p["status"]},
                    headers={"If-Match": str(p["expected_version"])},
                )
            case "add_task_comment":
                data = self._call(
                    "POST",
                    f"/api/tasks/{p['task_id']}/comments",
                    write=True,
                    json={"body": p["body"], "author": "AutoPilot Agent"},
                )
            case "update_milestone_health":
                data = self._call(
                    "PATCH",
                    f"/api/milestones/{p['milestone_id']}",
                    write=True,
                    json={"health": p["health"]},
                    headers={"If-Match": str(p["expected_version"])},
                )
            case "execute_payment":
                data = self._call(
                    "POST",
                    f"/api/purchase-requests/{p['pr_id']}/payments",
                    write=True,
                    json={
                        "amount": p["amount"],
                        "currency": p["currency"],
                        "recipient": p["recipient"],
                        "approval_ref": context.get("approval_id", ""),
                    },
                    headers={"If-Match": str(p["expected_version"]), "Idempotency-Key": context["idempotency_key"]},
                )
            case "change_user_role":
                data = self._call("POST", f"/api/users/{p['user_id']}/role", write=True, json={"role": p["role"]})
            case _:
                raise AdapterError(f"unsupported action '{action}'")
        return AdapterResult(data=data, method="api")

    # Used by the approval re-validation step (not exposed to the agent as a separate tool)
    def fetch_purchase_request(self, pr_id: str) -> dict:
        return self._call("GET", f"/api/purchase-requests/{pr_id}")

    def fetch_project(self, project_id: str) -> dict:
        return self._call("GET", f"/api/projects/{project_id}")

    def close(self) -> None:
        self._client.close()
