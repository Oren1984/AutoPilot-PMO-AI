"""Shared fixtures: a real DemoCorp enterprise server (uvicorn in a thread) and an isolated agent stack."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.config import Limits, Settings
from app.container import build_container
from app.demo.enterprise_app import create_enterprise_app

SVC_TOKEN = "test-service-token-abc"
ADMIN_TOKEN = "test-admin-token-xyz"
APPROVER_KEY = "test-approver-key-123"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EnterpriseServer:
    def __init__(self, db_path: Path):
        self.port = _free_port()
        self.app = create_enterprise_app(db_path, SVC_TOKEN, ADMIN_TOKEN)
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            try:
                if httpx.get(self.url + "/api/health", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("enterprise server failed to start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)

    def admin(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})

    def svc(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {SVC_TOKEN}"})

    def reset(self):
        with self.admin() as c:
            c.post("/api/admin/reset").raise_for_status()


@pytest.fixture(scope="session")
def enterprise(tmp_path_factory):
    srv = EnterpriseServer(tmp_path_factory.mktemp("ent") / "enterprise.db").start()
    yield srv
    srv.stop()


@pytest.fixture
def settings(tmp_path, enterprise) -> Settings:
    enterprise.reset()
    return Settings(
        mode="demo",
        agent_brain="simulated",
        enterprise="demo",
        data_dir=tmp_path,
        enterprise_base_url=enterprise.url,
        enterprise_api_token=SVC_TOKEN,
        enterprise_admin_token=ADMIN_TOKEN,
        approver_api_key=APPROVER_KEY,
        limits=Limits(),
    )


@pytest.fixture
def make_container(settings):
    made = []

    def _make(**kw):
        s = settings
        if "limits" in kw:
            s = replace(s, limits=kw.pop("limits"))
        if "agent_brain" in kw:
            s = replace(s, agent_brain=kw.pop("agent_brain"))
        c = build_container(s, **kw)
        made.append(c)
        return c

    yield _make
    for c in made:
        c.store.close()


@pytest.fixture
def container(make_container):
    return make_container()


@pytest.fixture
def run_until():
    from app.agent.runtime import wait_for_state

    return wait_for_state
