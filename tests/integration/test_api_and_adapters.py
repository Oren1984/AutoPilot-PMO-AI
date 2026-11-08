"""Backend HTTP API and adapter tests."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.base import AdapterError
from app.adapters.salesforce import SalesforceAdapter
from app.api.main import create_app
from tests.conftest import APPROVER_KEY, SVC_TOKEN


@pytest.fixture
def client(container):
    with TestClient(create_app(container)) as c:
        yield c


def test_health_config_policy(client):
    assert client.get("/health").json()["status"] == "ok"
    cfg = client.get("/api/config").json()
    assert cfg["mode"] == "demo" and "not Claude" in cfg["agent_label"]
    assert cfg["credit"] == "Developed and Built by Oren Salami — V1.0"
    assert "Computer Use" in cfg["execution_methods"]["ui"]  # honest labelling
    assert client.get("/api/policy").json()["actions"]["execute_payment"]["level"] == "approval_required"
    conn = client.get("/api/connection").json()
    assert conn["enterprise"]["api"]["ok"] is True


def test_input_validation(client):
    assert client.post("/api/runs", json={"assignment": ""}).status_code == 422
    assert client.post("/api/runs", json={"assignment": "x" * 5000}).status_code == 422
    assert client.get("/api/runs/RUN-NOPE").status_code == 404


@pytest.mark.browser
def test_full_flow_over_http(client, container, run_until):
    run = client.post("/api/runs", json={"assignment": "Review the active project and handle pending financial requests."}).json()
    run = run_until(container.store, run["id"], {"WAITING_APPROVAL", "FAILED"}, 120)
    assert run["state"] == "WAITING_APPROVAL"
    detail = client.get(f"/api/runs/{run['id']}").json()
    aid = detail["pending_approval"]["id"]
    assert detail["state_label"] == "Awaiting Approval"

    url = f"/api/approvals/{aid}/decision"
    body = {"decision": "approve", "approver": "Oren"}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={"X-Approver-Key": "wrong"}).status_code == 401
    assert client.post(url, json={**body, "approver": "agent"}, headers={"X-Approver-Key": APPROVER_KEY}).status_code == 403
    assert client.post(url, json={**body, "decision": "yes"}, headers={"X-Approver-Key": APPROVER_KEY}).status_code == 422
    ok = client.post(url, json=body, headers={"X-Approver-Key": APPROVER_KEY})
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"
    assert client.post(url, json=body, headers={"X-Approver-Key": APPROVER_KEY}).status_code == 409

    run = run_until(container.store, run["id"], {"COMPLETED", "FAILED", "BLOCKED"}, 120)
    assert run["state"] == "COMPLETED"
    events = client.get(f"/api/runs/{run['id']}/events").json()
    assert any(e["type"] == "APPROVAL_DECISION" for e in events)
    shot = next(e["screenshot"] for e in events if e.get("screenshot"))
    img = client.get(f"/api/screenshots/{shot}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"

    audit = client.get(f"/api/runs/{run['id']}/audit").json()
    blob = json.dumps(audit)
    assert SVC_TOKEN not in blob and APPROVER_KEY not in blob
    assert audit["approvals"][0]["status"] == "CONSUMED" and audit["executions"]
    assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 409  # terminal


def test_screenshot_path_traversal_blocked(client):
    assert client.get("/api/screenshots/..%2F..%2Fagent.db/x.png").status_code == 404
    assert client.get("/api/screenshots/RUN-1/..%2F..%2Fagent.db").status_code == 404


def test_claude_mode_without_credentials_is_explained(container, monkeypatch):
    from dataclasses import replace

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    container.settings = replace(container.settings, agent_brain="claude")
    with TestClient(create_app(container)) as c:
        r = c.post("/api/runs", json={"assignment": "Review the active project."})
        assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
        assert c.get("/api/connection").json()["claude"]["ok"] is False


def test_snapshot(client):
    snap = client.get("/api/enterprise/snapshot?project_id=PRJ-1001").json()
    assert snap["project"]["id"] == "PRJ-1001" and len(snap["tasks"]) == 6


# ---------------- adapters ----------------
def test_api_adapter_connection_error_is_retryable():
    from app.adapters.demo_api import DemoApiAdapter

    a = DemoApiAdapter("http://127.0.0.1:9", "t", timeout=0.5)
    with pytest.raises(AdapterError) as e:
        a.execute("list_projects", {}, {})
    assert e.value.retryable is True
    assert a.health()["ok"] is False


def _sf_transport(log):
    def handler(request: httpx.Request):
        log.append((request.method, str(request.url), request.content.decode() if request.content else ""))
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "sf-access-token"})
        if "/query" in request.url.path:
            return httpx.Response(200, json={"records": [{"Id": "00T000000000001AAA", "Status": "In Progress"}]})
        if request.method == "PATCH":
            return httpx.Response(204)
        if request.method == "GET" and "/sobjects/Task/" in request.url.path:
            return httpx.Response(200, json={"Id": "00T000000000001AAA", "Status": "Deferred", "WhatId": "006000000000001AAA"})
        return httpx.Response(201, json={"id": "00T000000000002AAA", "success": True})

    return httpx.MockTransport(handler)


def test_salesforce_adapter_mocked():
    log = []
    sf = SalesforceAdapter("https://example.my.salesforce.com", "cid", "csecret", transport=_sf_transport(log))
    tasks = sf.execute("list_tasks", {"project_id": "006000000000001AAA"}, {}).data
    assert tasks[0]["Status"] == "In Progress"
    upd = sf.execute("update_task_status", {"task_id": "00T000000000001AAA", "status": "Deferred"}, {}).data
    assert upd["Status"] == "Deferred"
    assert sf.health()["ok"] is True
    assert "grant_type=client_credentials" in log[0][2]
    assert all("Bearer" not in entry[2] for entry in log)
    with pytest.raises(AdapterError, match="financial execution is not connected"):
        sf.execute("execute_payment", {}, {})


def test_salesforce_requires_configuration():
    with pytest.raises(AdapterError, match="not configured"):
        SalesforceAdapter("", "", "")
    with pytest.raises(AdapterError):
        SalesforceAdapter("https://x", "a", "b", project_object="Opportunity; DROP")
