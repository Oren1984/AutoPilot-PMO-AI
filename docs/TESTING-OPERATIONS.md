# Testing and Operations

## Run the application (Docker — recommended)

Requirements: Docker Desktop (Windows 11 with WSL 2 backend, or Linux/macOS), about 3 GB free disk for the image, and about 2.5 GB RAM available to Docker (the backend container is limited to 1.5 GB because it runs headless Chromium).

```bash
cp .env.example .env          # optional - demo defaults work without it
docker compose up --build -d
docker compose ps             # all three services should report (healthy)
```

| URL | What |
|---|---|
| http://localhost:8501 | Operator console (start here) |
| http://localhost:8000/docs | Backend API (OpenAPI) |
| http://localhost:8100 | DemoCorp, the simulated enterprise app (web UI at `/ui/`; sign in with `ENTERPRISE_API_TOKEN`; also linked from the console sidebar) |

First build downloads Chromium and its system libraries and takes several minutes; later builds reuse the cached layer. Stop with `docker compose down` (data kept) or `docker compose down -v` (data volumes removed).

### Demo walkthrough

1. Open the console, keep the default assignment, press **Start Task**.
2. Watch the timeline: API reads, a UI page read, the injection finding, API writes, the UI status-report submission.
3. The banner turns to **WAITING FOR HUMAN APPROVAL** with the PR-2001 card. Press **Approve** (or **Reject**).
4. The run resumes, verifies and ends **Completed** (or **Rejected** with the payment unexecuted).
5. Use **Reset demo enterprise data** in the sidebar to repeat.

## Demo Mode vs Connected Mode

| | Demo Mode (default) | Connected Mode |
|---|---|---|
| Setting | `APP_MODE=demo` | `APP_MODE=connected` |
| Agent | Deterministic simulated agent, labelled as a simulation | Claude via the Anthropic API (`ANTHROPIC_API_KEY` required) |
| Enterprise system | Local DemoCorp (API + web UI) | Salesforce Developer Edition / sandbox, or DemoCorp with `ENTERPRISE_TARGET=demo` |
| Credentials | None | Anthropic API key; Salesforce External Client App with OAuth client-credentials flow and a run-as user (`SALESFORCE_INSTANCE_URL`, `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET`) |
| Cost | Free | Anthropic API usage is billed by Anthropic; estimates are shown |
| Payments | Simulated | Not available (no financial action exists for Salesforce) |

Recommended first connected test: `APP_MODE=connected AGENT_BRAIN=claude ENTERPRISE_TARGET=demo` — real Claude reasoning against the local DemoCorp, so the full hybrid and approval flow is available without touching Salesforce. A Claude.ai / Claude Desktop subscription does not provide API access; an API key from the Anthropic Console is needed.

If credentials are missing the console says so on the connection panel and a run fails immediately with the reason; nothing is simulated in its place.

## Test commands (local development)

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[ui,dev]"     # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m playwright install chromium
```

| Command | Scope |
|---|---|
| `ruff check .` and `ruff format --check .` | Lint and formatting |
| `pytest tests/unit -q` | Policy, approvals, state machine, redaction, limits, costs (no network) |
| `pytest tests/integration -q` | Real HTTP against a DemoCorp server started by the fixtures, real headless Chromium, workflow, recovery, security, API, adapters, fake Anthropic client |
| `pytest tests -q` | Both (the Docker E2E module is skipped unless `E2E_BASE_URL` is set) |
| `pip-audit --skip-editable` | Known-vulnerability check of installed dependencies |

Docker end-to-end (stack must be running):

```bash
E2E_BASE_URL=http://127.0.0.1:8000 E2E_RESTART=1 pytest tests/e2e -m docker -v
```

It checks the three health endpoints, runs the full approve path while restarting the backend container during the approval wait, and runs the reject path.

## Expected validation results

Results recorded on 2026-11-08 on Windows 11 with Docker Desktop:

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | passed |
| Unit tests | 43 passed |
| Integration tests | 41 passed (full local run: 84 passed, 1 skipped = Docker module, run separately) |
| Docker build + `docker compose up --build -d` | all three services healthy |
| Docker E2E (`tests/e2e`, with `E2E_RESTART=1`) | 3 passed |
| `pip-audit` | not run locally (the sandboxed workstation could not reach PyPI over TLS); runs in CI |

The integration suite takes about 5–6 minutes because it drives real Chromium. These tests use DemoCorp and a deterministic fake Anthropic client; **no test in this repository calls the live Anthropic API or a live Salesforce org.**

## CI

`.github/workflows/ci.yml` runs on push to `main` and on pull requests, without secrets:

1. `quality`: install, Playwright Chromium, ruff lint + format check, unit tests, integration tests, `pip-audit`.
2. `docker`: `docker compose up --build -d --wait`, Docker E2E with restart, logs on failure, tear-down.

## Operational checks

| Check | How |
|---|---|
| Health | `docker compose ps`; `GET /health` (backend), `GET /api/health` (DemoCorp), `/_stcore/health` (console) |
| Connection and mode | Console sidebar, or `GET /api/config` and `GET /api/connection` |
| Logs | `docker compose logs backend` — structured JSON, redacted |
| Run history and audit | Console Diagnostics tab; `GET /api/runs/{id}/audit` |
| Pending approvals | `GET /api/approvals?status=PENDING` |
| Restart recovery | `docker compose restart backend` — active runs resume, waiting runs keep their approval |
| Data reset | Console sidebar **Reset demo enterprise data**, or `docker compose down -v` |

## Known limitations

- **Claude:** implemented with the official SDK and tested only with a fake client; not exercised against the live API here (no API key was used).
- **Salesforce:** adapter tested only against a mocked HTTP transport; not verified against a real org. Scope is reads plus Task status/comments; no UI automation and no financial actions on Salesforce.
- **UI automation** is Playwright browser automation directed by the agent, not Anthropic's native Computer Use tool. Selectors are specific to DemoCorp.
- **Console** has no user login; the approver key is held by the console process. Suitable for a local POC only.
- **Concurrency:** one backend process with background threads; SQLite. Not designed for many simultaneous runs or multiple backend replicas.
- **Costs** are estimates from provider-reported tokens and configured prices; no billing API is queried.
- Priority ERP and other systems are not implemented; the adapter interface is the extension point.

Developed and Built by Oren Salami — V1.0
