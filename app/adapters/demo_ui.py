"""Browser UI adapter (Method B - UI interaction) for the DemoCorp enterprise application.

Implementation: Playwright-driven Chromium in an isolated, headless browser context. This is
agent-directed browser automation; it is NOT Anthropic's native Computer Use tool. The agent chooses
the UI action, and this adapter performs it with deterministic selectors and captures screenshots.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.adapters.base import ActionSpec, AdapterError, AdapterResult, schema
from app.adapters.demo_api import ID

SPECS = [
    ActionSpec(
        "ui_view_project",
        "Open the project page in the enterprise web UI and read what is displayed (browser automation).",
        schema({"project_id": ID}, ["project_id"]),
        "ui",
    ),
    ActionSpec(
        "ui_submit_status_report",
        "Submit the weekly project status report through the enterprise web UI form. This capability exists only "
        "in the UI (no API endpoint).",
        schema(
            {
                "project_id": ID,
                "rag": {"type": "string", "enum": ["Green", "Amber", "Red"]},
                "summary": {"type": "string", "maxLength": 4000},
            },
            ["project_id", "rag", "summary"],
        ),
        "ui",
        "write",
    ),
]


class DemoUiAdapter:
    name = "demo-enterprise-ui (Playwright Chromium)"

    def __init__(self, base_url: str, token: str, screenshot_dir: Path, headless: bool = True, timeout_ms: int = 15000):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.screenshot_dir = Path(screenshot_dir)
        self.headless = headless
        self.timeout_ms = timeout_ms

    def capabilities(self) -> list[ActionSpec]:
        return list(SPECS)

    def health(self) -> dict:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            return {"ok": False, "detail": "playwright not installed"}
        return {"ok": True, "detail": "playwright available (browser launched per action)"}

    def _shot(self, page, run_id: str, step: int, label: str, steps: list[str]) -> str:
        folder = self.screenshot_dir / run_id
        folder.mkdir(parents=True, exist_ok=True)
        name = f"step{step:03d}-{label}-{int(time.time() * 1000) % 100000}.png"
        # Mask anything flagged sensitive (e.g. bank account fields) before capture.
        page.screenshot(path=str(folder / name), full_page=True, mask=[page.locator("[data-sensitive]")])
        steps.append(f"screenshot: {label}")
        return f"{run_id}/{name}"

    def execute(self, action: str, params: dict, context: dict) -> AdapterResult:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise AdapterError("browser automation unavailable: playwright is not installed") from exc

        run_id = context.get("run_id", "adhoc")
        step = int(context.get("step", 0))
        browser_steps: list[str] = []
        shots: list[str] = []
        write_started = False
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self.headless, args=["--disable-dev-shm-usage"])
                try:
                    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
                    ctx.set_default_timeout(self.timeout_ms)
                    # Establish an authenticated session without typing credentials into the page
                    # (token never appears on screen or in screenshots).
                    resp = ctx.request.post(f"{self.base_url}/ui/session", form={"token": self._token}, max_redirects=0)
                    if resp.status not in (200, 303):
                        raise AdapterError(f"UI sign-in failed (HTTP {resp.status})")
                    browser_steps.append("sign in with service session (token not displayed)")
                    page = ctx.new_page()
                    url = f"{self.base_url}/ui/projects/{params['project_id']}"
                    page.goto(url)
                    browser_steps.append(f"open {url.replace(self.base_url, '')}")
                    if page.locator("#project-title").count() == 0:
                        raise AdapterError(f"project page did not load for {params['project_id']}")
                    title = page.locator("#project-title").inner_text()

                    if action == "ui_view_project":
                        rows = page.locator("#tasks tr[data-task]")
                        tasks = []
                        for i in range(rows.count()):
                            cells = rows.nth(i).locator("td")
                            tasks.append({"id": cells.nth(0).inner_text(), "status": cells.nth(4).inner_text()})
                        browser_steps.append(f"read {len(tasks)} task rows from page")
                        shots.append(self._shot(page, run_id, step, "project-page", browser_steps))
                        data = {"title": title, "tasks": tasks, "url": url}

                    elif action == "ui_submit_status_report":
                        page.click("#new-status-report")
                        browser_steps.append("click 'New status report'")
                        page.wait_for_selector("#status-report-form")
                        page.select_option("#rag", params["rag"])
                        browser_steps.append(f"select RAG = {params['rag']}")
                        page.fill("#summary", params["summary"])
                        browser_steps.append(f"fill summary ({len(params['summary'])} chars)")
                        shots.append(self._shot(page, run_id, step, "form-filled", browser_steps))
                        write_started = True
                        page.click("#submit-report")
                        browser_steps.append("click 'Submit report'")
                        page.wait_for_selector("#flash")
                        flash = page.locator("#flash").inner_text()
                        report_id = next((w for w in flash.split() if w.startswith("SR-")), None)
                        browser_steps.append(f"confirmation banner: {flash}")
                        shots.append(self._shot(page, run_id, step, "submitted", browser_steps))
                        if not report_id:
                            raise AdapterError("status report submission not confirmed by the UI", uncertain=True)
                        data = {"report_id": report_id, "confirmation": flash, "project": title}
                    else:
                        raise AdapterError(f"unsupported UI action '{action}'")
                finally:
                    browser.close()
        except AdapterError:
            raise
        except PlaywrightError as exc:
            msg = str(exc).splitlines()[0][:300]
            raise AdapterError(
                f"browser automation failure: {msg}", retryable=not write_started, uncertain=write_started
            ) from exc
        return AdapterResult(
            data=data,
            method="ui",
            screenshot=shots[-1] if shots else None,
            meta={"browser_steps": browser_steps, "screenshots": shots},
        )
