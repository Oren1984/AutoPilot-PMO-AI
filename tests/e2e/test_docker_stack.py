"""End-to-end test against the running Docker Compose stack.

Run after `docker compose up --build -d`:
    E2E_BASE_URL=http://127.0.0.1:8000 E2E_UI_URL=http://127.0.0.1:8501 pytest tests/e2e -m docker
Set E2E_RESTART=1 to also restart the backend container while a run waits for approval.
"""

import os
import subprocess
import time

import httpx
import pytest

pytestmark = pytest.mark.docker

BASE = os.environ.get("E2E_BASE_URL")
UI = os.environ.get("E2E_UI_URL", "http://127.0.0.1:8501")
ENT = os.environ.get("E2E_ENTERPRISE_URL", "http://127.0.0.1:8100")
KEY = os.environ.get("APPROVER_API_KEY", "demo-approver-key-change-me")
ADMIN = os.environ.get("ENTERPRISE_ADMIN_TOKEN", "demo-admin-token-change-me")

if not BASE:
    pytest.skip("E2E_BASE_URL not set (Docker stack not under test)", allow_module_level=True)


def wait_state(run_id, states, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            run = httpx.get(f"{BASE}/api/runs/{run_id}", timeout=10).json()
            if run["state"] in states:
                return run
        except httpx.HTTPError:
            pass  # backend restarting
        time.sleep(1)
    raise AssertionError(f"timeout waiting for {states}")


def wait_healthy(url, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise AssertionError(f"{url} not healthy")


def reset():
    httpx.post(f"{ENT}/api/admin/reset", headers={"Authorization": f"Bearer {ADMIN}"}, timeout=10).raise_for_status()


def start():
    r = httpx.post(
        f"{BASE}/api/runs",
        json={
            "assignment": "Review the active project, update permitted task statuses and handle the pending financial request; stop for approval before any payment."
        },
        timeout=30,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def decide(approval_id, decision):
    r = httpx.post(
        f"{BASE}/api/approvals/{approval_id}/decision",
        json={"decision": decision, "approver": "E2E Approver"},
        headers={"X-Approver-Key": KEY},
        timeout=30,
    )
    assert r.status_code == 200, r.text


def test_services_healthy():
    wait_healthy(f"{BASE}/health")
    wait_healthy(f"{ENT}/api/health")
    assert httpx.get(UI, timeout=10).status_code == 200
    assert httpx.get(f"{UI}/_stcore/health", timeout=10).text.strip() == "ok"
    conn = httpx.get(f"{BASE}/api/connection", timeout=20).json()
    assert conn["enterprise"]["api"]["ok"] and conn["enterprise"]["ui"]["ok"]


def test_demo_flow_approve_with_restart():
    reset()
    run_id = start()
    run = wait_state(run_id, {"WAITING_APPROVAL", "FAILED", "BLOCKED"})
    assert run["state"] == "WAITING_APPROVAL", run.get("error")
    aid = run["pending_approval"]["id"]
    if os.environ.get("E2E_RESTART") == "1":
        subprocess.run(["docker", "compose", "restart", "backend"], check=True, timeout=180)
        wait_healthy(f"{BASE}/health")
        assert wait_state(run_id, {"WAITING_APPROVAL"})["pending_approval"]["id"] == aid
    decide(aid, "approve")
    run = wait_state(run_id, {"COMPLETED", "FAILED", "BLOCKED", "REJECTED"})
    assert run["state"] == "COMPLETED", run.get("error")
    assert run["result"]["verified"] is True
    events = httpx.get(f"{BASE}/api/runs/{run_id}/events", timeout=10).json()
    assert {"API", "UI"} <= {e["method"] for e in events if e["type"] == "TOOL_CALL"}


def test_demo_flow_reject():
    reset()
    run_id = start()
    run = wait_state(run_id, {"WAITING_APPROVAL"})
    decide(run["pending_approval"]["id"], "reject")
    run = wait_state(run_id, {"REJECTED", "COMPLETED", "FAILED", "BLOCKED"})
    assert run["state"] == "REJECTED"
    pr = httpx.get(f"{ENT}/api/purchase-requests/PR-2001", headers={"Authorization": f"Bearer {ADMIN}"}, timeout=10).json()
    assert pr["payments"] == [] and pr["status"] == "Pending Approval"
