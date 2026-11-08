"""Real Chromium against the local DemoCorp UI."""

from pathlib import Path

import pytest
from PIL import Image

from app.adapters.base import AdapterError
from app.adapters.demo_ui import DemoUiAdapter
from tests.conftest import SVC_TOKEN

pytestmark = pytest.mark.browser


@pytest.fixture
def ui(enterprise, tmp_path):
    enterprise.reset()
    return DemoUiAdapter(enterprise.url, SVC_TOKEN, tmp_path)


def test_view_project_reads_rendered_page(ui, tmp_path):
    res = ui.execute("ui_view_project", {"project_id": "PRJ-1001"}, {"run_id": "RUN-T", "step": 1})
    assert res.method == "ui" and "Atlas ERP Migration" in res.data["title"]
    assert {"id": "T-101", "status": "In Progress"} in res.data["tasks"]
    shot = tmp_path / res.screenshot
    assert shot.is_file() and Image.open(shot).size[0] == 1280


def test_submit_status_report_through_form(ui, enterprise):
    res = ui.execute(
        "ui_submit_status_report",
        {"project_id": "PRJ-1001", "rag": "Amber", "summary": "UI test report"},
        {"run_id": "RUN-T", "step": 2},
    )
    assert res.data["report_id"].startswith("SR-")
    assert any("Submit report" in s for s in res.meta["browser_steps"])
    assert len(res.meta["screenshots"]) == 2
    with enterprise.svc() as h:
        reports = h.get("/api/projects/PRJ-1001/status-reports").json()
    assert reports[0]["id"] == res.data["report_id"] and reports[0]["channel"] == "ui"


def test_ui_failure_is_reported(ui):
    with pytest.raises(AdapterError):
        ui.execute("ui_view_project", {"project_id": "PRJ-0000"}, {"run_id": "RUN-T", "step": 3})


def test_bad_session_token_fails_closed(enterprise, tmp_path):
    bad = DemoUiAdapter(enterprise.url, "wrong-token", tmp_path)
    with pytest.raises(AdapterError):
        bad.execute("ui_view_project", {"project_id": "PRJ-1001"}, {"run_id": "RUN-T", "step": 4})


def test_screenshots_do_not_contain_token(ui, tmp_path):
    res = ui.execute("ui_view_project", {"project_id": "PRJ-1001"}, {"run_id": "RUN-T", "step": 5})
    assert SVC_TOKEN.encode() not in Path(tmp_path / res.screenshot).read_bytes()
