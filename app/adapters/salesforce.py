"""Salesforce REST adapter (Connected Mode, Method A - API).

Scope in V1.0 (deliberately small, see docs/ARCHITECTURE.md):
  * OAuth 2.0 Client Credentials flow against a Developer Edition org or sandbox (External Client App /
    Connected App with the client-credentials flow enabled and a run-as user).
  * Reads: projects (configurable SObject, default Opportunity) and tasks (standard Task).
  * Writes: Task.Status updates and task comments (appended as a new completed Task linked to the same record).
  * Financial execution is NOT connected: execute_payment raises - V1.0 never commits real funds.
This adapter has unit tests against a mocked transport; it has not been verified against a live org
in this repository unless the operator reports otherwise.
"""

from __future__ import annotations

import re
import threading

import httpx

from app.adapters.base import ActionSpec, AdapterError, AdapterResult, I, schema

SF_ID = {"type": "string", "pattern": "^[a-zA-Z0-9]{15,18}$", "maxLength": 18}
_SAFE = re.compile(r"^[a-zA-Z0-9_]+$")

SPECS = [
    ActionSpec(
        "list_projects",
        "List Salesforce records that represent projects.",
        schema({"status": {"type": "string", "maxLength": 60}}),
        "api",
    ),
    ActionSpec("get_project", "Get one project record.", schema({"project_id": SF_ID}, ["project_id"]), "api"),
    ActionSpec(
        "list_tasks",
        "List open Salesforce Tasks related to a project record.",
        schema({"project_id": SF_ID}, ["project_id"]),
        "api",
    ),
    ActionSpec(
        "update_task_status",
        "Update Task.Status (value must exist in the org's Task Status picklist).",
        schema({"task_id": SF_ID, "status": {"type": "string", "maxLength": 40}, "expected_version": I}, ["task_id", "status"]),
        "api",
        "write",
    ),
    ActionSpec(
        "add_task_comment",
        "Record a note as a completed Task related to the same record.",
        schema({"task_id": SF_ID, "body": {"type": "string", "maxLength": 4000}}, ["task_id", "body"]),
        "api",
        "write",
    ),
]


class SalesforceAdapter:
    name = "salesforce-rest"

    def __init__(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
        api_version: str = "v62.0",
        project_object: str = "Opportunity",
        transport: httpx.BaseTransport | None = None,
    ):
        if not (instance_url and client_id and client_secret):
            raise AdapterError("Salesforce is not configured (SALESFORCE_INSTANCE_URL / CLIENT_ID / CLIENT_SECRET)")
        if not _SAFE.match(project_object):
            raise AdapterError("invalid SALESFORCE_PROJECT_OBJECT")
        self.instance_url = instance_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self.api_version = api_version
        self.project_object = project_object
        self._http = httpx.Client(timeout=20.0, transport=transport)
        self._token: str | None = None
        self._lock = threading.Lock()

    def capabilities(self) -> list[ActionSpec]:
        return list(SPECS)

    def _auth(self) -> str:
        with self._lock:
            if self._token:
                return self._token
            try:
                r = self._http.post(
                    f"{self.instance_url}/services/oauth2/token",
                    data={"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret},
                )
            except httpx.HTTPError as exc:
                raise AdapterError(f"cannot reach Salesforce: {type(exc).__name__}", retryable=True) from exc
            if r.status_code != 200:
                raise AdapterError(
                    f"Salesforce authentication failed (HTTP {r.status_code}); check the connected app and permissions",
                    status=r.status_code,
                )
            self._token = r.json()["access_token"]
            return self._token

    def _req(self, method: str, path: str, *, write: bool = False, **kw):
        token = self._auth()
        url = f"{self.instance_url}/services/data/{self.api_version}{path}"
        try:
            r = self._http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.TimeoutException as exc:
            raise AdapterError("Salesforce timeout", retryable=not write, uncertain=write) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"Salesforce connection error: {type(exc).__name__}", retryable=True) from exc
        if r.status_code == 401:
            self._token = None
            raise AdapterError("Salesforce session expired or unauthorized", status=401, retryable=True)
        if r.status_code >= 400:
            raise AdapterError(f"Salesforce error {r.status_code}: {r.text[:300]}", status=r.status_code)
        return r.json() if r.content else {}

    def _soql(self, q: str) -> list[dict]:
        return self._req("GET", "/query", params={"q": q}).get("records", [])

    def health(self) -> dict:
        try:
            self._auth()
            return {"ok": True, "detail": "authenticated"}
        except AdapterError as exc:
            return {"ok": False, "detail": str(exc)}

    def execute(self, action: str, params: dict, context: dict) -> AdapterResult:
        obj = self.project_object
        match action:
            case "list_projects":
                data = self._soql(f"SELECT Id, Name, StageName, Amount, CloseDate FROM {obj} ORDER BY CloseDate LIMIT 50")  # noqa: S608
            case "get_project":
                data = self._req("GET", f"/sobjects/{obj}/{params['project_id']}")
            case "list_tasks":
                data = self._soql(
                    "SELECT Id, Subject, Status, Priority, ActivityDate, OwnerId, WhatId FROM Task "  # noqa: S608
                    f"WHERE WhatId = '{params['project_id']}' AND IsClosed = false ORDER BY ActivityDate LIMIT 200"
                )
            case "update_task_status":
                self._req("PATCH", f"/sobjects/Task/{params['task_id']}", write=True, json={"Status": params["status"]})
                data = self._req("GET", f"/sobjects/Task/{params['task_id']}")
            case "add_task_comment":
                task = self._req("GET", f"/sobjects/Task/{params['task_id']}")
                data = self._req(
                    "POST",
                    "/sobjects/Task",
                    write=True,
                    json={
                        "Subject": "AutoPilot note",
                        "Description": params["body"],
                        "WhatId": task.get("WhatId"),
                        "Status": "Completed",
                    },
                )
            case "execute_payment":
                raise AdapterError("financial execution is not connected in V1.0 (Salesforce mode is non-financial)")
            case _:
                raise AdapterError(f"action '{action}' is not supported by the Salesforce adapter in V1.0")
        return AdapterResult(data=data, method="api")
