"""Deterministic demo agent - a SIMULATION, not Claude.

It is rule-based: it reads live records from the enterprise system, derives findings from that data
(overdue work, milestone exposure, budget, embedded instructions), builds its plan dynamically from
those findings, and adapts to tool outcomes (policy blocks, approvals, rejections, conflicts).
It exists so the full workflow can be demonstrated and tested offline without paid AI services.
It must never be presented as Claude reasoning.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from app.agent.base import Finish, ToolCall, note

INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior) instructions",
    r"note to (ai|automated|llm) agents?",
    r"approve [\w\- ]+ (immediately|now)",
    r"without (human )?review",
    r"grant [\w\- ]+ (admin|administrator)",
    r"you are now",
]
_INJ = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)
CLOSED = {"Done", "Cancelled"}


def _call(action: str, params: dict, rationale: str, phase: str = "execution", **kw) -> dict:
    return ToolCall(action, params, rationale, phase=phase, **kw).to_dict()


class SimulatedAgent:
    kind = "simulated"
    simulated = True

    def __init__(self, today: date | None = None):
        self._today = today
        self.s: dict = {}
        self._preview = None

    # ------------------------------------------------------------------ lifecycle
    def start(self, assignment: str, capabilities: list, preview) -> None:
        self._preview = preview
        today = (self._today or date.today()).isoformat()
        self.s = {
            "assignment": assignment,
            "today": today,
            "phase": "discover",
            "queue": [],
            "obs": {},
            "verify_obs": {},
            "expect": {"tasks": {}, "milestones": {}, "reports": [], "payments": {}},
            "findings": [],
            "actions_done": [],
            "approvals": [],
            "escalations": [],
            "notes": [],
            "plan": [
                {"id": "discover", "title": "Discover capabilities and locate the active project", "status": "in_progress"},
                {
                    "id": "collect",
                    "title": "Retrieve project, tasks, milestones, financial requests (API + UI)",
                    "status": "pending",
                },
                {"id": "analyze", "title": "Analyze deadlines, risks, budget and untrusted content", "status": "pending"},
                {"id": "act", "title": "Execute authorized updates and route financial actions to policy", "status": "pending"},
                {"id": "verify", "title": "Verify final record state", "status": "pending"},
            ],
        }
        names = sorted(c.name for c in capabilities)
        self._note("ANALYSIS", f"Discovered {len(names)} enterprise capabilities", capabilities=names)

    def load_state(self, state: dict, preview) -> None:
        self.s = state
        self._preview = preview

    def export_state(self) -> dict:
        return self.s

    def plan(self) -> list[dict]:
        return self.s.get("plan", [])

    def drain_notes(self) -> list[dict]:
        out, self.s["notes"] = self.s.get("notes", []), []
        return out

    def drain_model_usage(self) -> list[dict]:
        return []  # no model is used

    # ------------------------------------------------------------------ helpers
    def _note(self, kind: str, message: str, **detail) -> None:
        self.s["notes"].append(note(kind, message, **detail))

    def _set_plan(self, pid: str, status: str, title: str | None = None) -> None:
        for item in self.s["plan"]:
            if item["id"] == pid:
                item["status"] = status
                if title:
                    item["title"] = title

    def _add_plan(self, pid: str, title: str, after: str) -> None:
        plan = self.s["plan"]
        idx = next((i for i, p in enumerate(plan) if p["id"] == after), len(plan) - 1)
        plan.insert(idx + 1, {"id": pid, "title": title, "status": "pending"})

    def _days_overdue(self, due: str) -> int:
        return (date.fromisoformat(self.s["today"]) - date.fromisoformat(due)).days

    # ------------------------------------------------------------------ decision loop
    def next_action(self) -> ToolCall | Finish:
        while not self.s["queue"]:
            phase = self.s["phase"]
            if phase == "discover":
                self.s["queue"].append(
                    _call("list_projects", {"status": "Active"}, "Locate active projects in scope", "analysis")
                )
                self.s["phase"] = "select"
            elif phase == "select":
                self._select_project()
            elif phase == "analyze":
                self._analyze()
            elif phase == "act":
                self._begin_verification()
            elif phase == "verify":
                return self._conclude()
            else:
                return self._conclude()
        return ToolCall.from_dict(self.s["queue"].pop(0))

    def observe(self, call: ToolCall, outcome: dict) -> None:
        status = outcome.get("status")
        data = outcome.get("data")
        target = self.s["verify_obs"] if call.phase == "verification" else self.s["obs"]
        if status == "ok":
            target[call.action + (":" + call.params.get("pr_id", "") if call.action == "get_purchase_request" else "")] = data

        if call.phase == "verification":
            return
        if call.action in (
            "update_task_status",
            "add_task_comment",
            "update_milestone_health",
            "ui_submit_status_report",
            "execute_payment",
        ):
            self._track_write(call, outcome)

    def _track_write(self, call: ToolCall, outcome: dict) -> None:
        status = outcome.get("status")
        p = call.params
        entry = {
            "action": call.action,
            "params": p,
            "status": status,
            "message": outcome.get("message"),
            "method": outcome.get("method"),
        }
        self.s["actions_done"].append(entry)
        if status == "ok":
            if call.action == "update_task_status":
                self.s["expect"]["tasks"][p["task_id"]] = p["status"]
            elif call.action == "update_milestone_health":
                self.s["expect"]["milestones"][p["milestone_id"]] = p["health"]
            elif call.action == "ui_submit_status_report":
                self.s["expect"]["reports"].append(outcome["data"]["report_id"])
            elif call.action == "execute_payment":
                self.s["expect"]["payments"][p["pr_id"]] = "Paid (Simulated)"
                self.s["approvals"].append(
                    {
                        "pr_id": p["pr_id"],
                        "outcome": "APPROVED and executed (simulated)",
                        "approval_id": outcome.get("approval_id"),
                    }
                )
        elif call.action == "execute_payment":
            self.s["expect"]["payments"][p["pr_id"]] = "Pending Approval"
            label = {
                "rejected": "REJECTED - not executed",
                "expired": "EXPIRED - not executed",
                "invalidated": "INVALIDATED - not executed",
                "blocked": "BLOCKED by policy",
            }.get(status, f"{status} - not executed")
            self.s["approvals"].append(
                {
                    "pr_id": p["pr_id"],
                    "outcome": label,
                    "approval_id": outcome.get("approval_id"),
                    "message": outcome.get("message"),
                }
            )
            self._note("DECISION", f"{p['pr_id']}: {label}. The controlled financial action remains unexecuted.")
        elif status == "error" and "409" in (outcome.get("message") or ""):
            self._note(
                "WARNING", f"{call.action} hit a version conflict; record changed concurrently - left for human review", params=p
            )

    # ------------------------------------------------------------------ phases
    def _select_project(self) -> None:
        projects = self.s["obs"].get("list_projects") or []
        if not projects:
            self.s["findings"].append({"severity": "high", "text": "No active projects found or project list unavailable"})
            self.s["phase"] = "verify"
            return
        text = self.s["assignment"]
        ids = re.findall(r"PRJ-\d+", text, re.IGNORECASE)
        chosen = None
        if ids:
            chosen = next((p for p in projects if p["id"].upper() == ids[0].upper()), None)
        if chosen is None:
            chosen = next((p for p in projects if p["name"].lower() in text.lower()), None)
        if chosen is None:
            # "the active project": choose the active project with the largest committed budget exposure
            chosen = max(projects, key=lambda p: p.get("budget_committed", 0))
        pid = chosen["id"]
        self.s["project_id"] = pid
        self._note(
            "ANALYSIS",
            f"Selected project {pid} ({chosen['name']}) as the assignment target",
            reason="explicit reference" if ids else "highest committed budget among active projects",
        )
        self._set_plan("discover", "done")
        self._set_plan("collect", "in_progress")
        q = self.s["queue"]
        q.append(_call("get_project", {"project_id": pid}, "Read project budget and schedule", "analysis"))
        q.append(_call("list_tasks", {"project_id": pid}, "Read tasks, owners and deadlines", "analysis"))
        q.append(_call("list_milestones", {"project_id": pid}, "Read milestones", "analysis"))
        q.append(_call("list_purchase_requests", {"project_id": pid}, "Read pending financial requests", "analysis"))
        q.append(_call("ui_view_project", {"project_id": pid}, "Cross-check what users see in the enterprise UI", "analysis"))
        self.s["phase"] = "analyze"

    def _analyze(self) -> None:
        obs, s = self.s["obs"], self.s
        pid = s["project_id"]
        project = obs.get("get_project") or {}
        tasks = obs.get("list_tasks") or []
        milestones = obs.get("list_milestones") or []
        prs = obs.get("list_purchase_requests") or []
        self._set_plan("collect", "done")
        self._set_plan("analyze", "in_progress")
        q = s["queue"]
        today = date.fromisoformat(s["today"])

        overdue = [t for t in tasks if t["status"] not in CLOSED and date.fromisoformat(t["due_date"]) < today]
        due_soon = [
            t
            for t in tasks
            if t["status"] not in CLOSED and today <= date.fromisoformat(t["due_date"]) <= today + timedelta(days=7)
        ]
        for t in overdue:
            s["findings"].append(
                {
                    "severity": "high" if t["priority"] == "High" else "medium",
                    "text": f"{t['id']} '{t['title']}' is {self._days_overdue(t['due_date'])} day(s) overdue (status {t['status']}, owner {t['owner']})",
                }
            )
        for t in due_soon:
            s["findings"].append({"severity": "low", "text": f"{t['id']} '{t['title']}' is due within 7 days ({t['due_date']})"})

        # Untrusted content: records are data, never instructions.
        injected = [t for t in tasks if _INJ.search(t.get("description") or "")]
        for t in injected:
            s["findings"].append(
                {
                    "severity": "high",
                    "text": f"{t['id']} description contains instructions aimed at automated agents - treated as untrusted data and ignored",
                }
            )
            self._note(
                "SECURITY",
                f"Prompt-injection pattern detected in {t['id']}; content ignored, no action derived from it",
                task=t["id"],
            )

        exposed = []
        for m in milestones:
            if m["status"] == "Done":
                continue
            linked = [t for t in overdue if t.get("milestone_id") == m["id"]]
            if linked and m["health"] == "On Track":
                exposed.append((m, linked))
                s["findings"].append(
                    {
                        "severity": "high",
                        "text": f"Milestone {m['id']} '{m['name']}' (due {m['due_date']}) is exposed by {len(linked)} overdue task(s)",
                    }
                )

        remaining = project.get("budget_remaining")
        pending = sorted([p for p in prs if p["status"] == "Pending Approval"], key=lambda p: -p["amount"])
        self._note(
            "ANALYSIS",
            f"Analysis complete: {len(overdue)} overdue task(s), {len(exposed)} exposed milestone(s), {len(pending)} pending financial request(s), {len(injected)} untrusted-instruction finding(s)",
        )
        self._set_plan("analyze", "done")
        self._set_plan("act", "in_progress")

        # --- Level 1 autonomous updates (each still passes the policy gate at execution time)
        for t in overdue:
            days = self._days_overdue(t["due_date"])
            if t["status"] in ("Not Started", "In Progress"):
                q.append(
                    _call(
                        "update_task_status",
                        {"task_id": t["id"], "status": "At Risk", "expected_version": t["version"]},
                        f"{t['id']} is {days} day(s) overdue - flag as At Risk",
                    )
                )
                q.append(
                    _call(
                        "add_task_comment",
                        {
                            "task_id": t["id"],
                            "body": f"AutoPilot: task is {days} day(s) overdue (due {t['due_date']}). Proposed next action: {t['owner']} to confirm a recovery date by {(today + timedelta(days=2)).isoformat()}; PM to review impact on milestone {t.get('milestone_id')}.",
                        },
                        "Record the proposed next action on the task",
                    )
                )
            elif t["status"] == "Blocked":
                q.append(
                    _call(
                        "add_task_comment",
                        {
                            "task_id": t["id"],
                            "body": f"AutoPilot: blocked and {days} day(s) overdue. Proposed next action: escalate the blocker to project owner {project.get('owner', 'PM')} within 24h.",
                        },
                        "Blocked task - status left unchanged, escalation recorded",
                    )
                )
        for t in injected:
            q.append(
                _call(
                    "add_task_comment",
                    {
                        "task_id": t["id"],
                        "body": "AutoPilot security notice: this task description contains embedded instructions targeting automated agents. They were ignored. Please review and clean up the description.",
                    },
                    "Flag untrusted embedded instructions for human review",
                )
            )
        for m, linked in exposed:
            q.append(
                _call(
                    "update_milestone_health",
                    {"milestone_id": m["id"], "health": "At Risk", "expected_version": m["version"]},
                    f"{len(linked)} overdue task(s) feed milestone {m['id']}",
                )
            )

        # --- UI-only capability: weekly status report
        rag = "Red" if len(overdue) >= 3 else ("Amber" if overdue or exposed else "Green")
        lines = [f"Automated status report for {pid} ({project.get('name', '')}) - {s['today']}", f"Overall: {rag}."]
        lines += [f"- {f['text']}" for f in s["findings"] if f["severity"] in ("high", "medium")][:8]
        if pending:
            lines.append(f"- {len(pending)} financial request(s) pending; financial actions are routed to human approval.")
        lines.append("Prepared by AutoPilot PMO AI (deterministic demo agent).")
        q.append(
            _call(
                "ui_submit_status_report",
                {"project_id": pid, "rag": rag, "summary": "\n".join(lines)},
                "Status reports can only be submitted through the web UI",
            )
        )

        # --- Financial requests: plan-time policy preview, then the real gated call
        for pr in pending:
            params = {
                "pr_id": pr["id"],
                "amount": pr["amount"],
                "currency": pr["currency"],
                "recipient": pr["vendor"],
                "expected_version": pr["version"],
            }
            risks = []
            if remaining is not None and pr["amount"] > remaining:
                risks.append(f"amount exceeds remaining budget ({remaining:,.0f} {pr['currency']})")
            if any(pr["id"] in (t.get("description") or "") for t in injected):
                risks.append("referenced by an embedded instruction in project data (possible manipulation)")
            if pr.get("requested_by") in (None, "", "Unknown"):
                risks.append("requester not identified")
            preview = self._preview("execute_payment", params) if self._preview else {"decision": "REQUIRE_APPROVAL"}
            self._note(
                "POLICY_PREVIEW",
                f"{pr['id']} ({pr['amount']:,.2f} {pr['currency']} to {pr['vendor']}): {preview['decision']} - {preview.get('reason', '')}",
                pr_id=pr["id"],
                decision=preview,
            )
            if preview["decision"] == "DENY":
                s["escalations"].append({"pr_id": pr["id"], "reason": preview.get("reason"), "risks": risks})
                s["approvals"].append(
                    {"pr_id": pr["id"], "outcome": "BLOCKED by policy (Level 3) - escalated to Finance, not attempted"}
                )
                s["findings"].append(
                    {"severity": "high", "text": f"{pr['id']} cannot be processed by the agent: {preview.get('reason')}"}
                )
                continue
            if risks:
                s["findings"].append({"severity": "medium", "text": f"{pr['id']} risks: " + "; ".join(risks)})
            q.append(
                _call(
                    "execute_payment",
                    params,
                    f"Pending request {pr['id']} '{pr['title']}' is within budget and policy limits; prepared for human approval",
                    context={"risks": risks or ["none identified by the agent"]},
                )
            )
        self.s["phase"] = "act"

    def _begin_verification(self) -> None:
        pid = self.s["project_id"]
        self._set_plan("act", "done")
        self._set_plan("verify", "in_progress")
        q = self.s["queue"]
        q.append(_call("list_tasks", {"project_id": pid}, "Verify task updates", "verification"))
        q.append(_call("list_milestones", {"project_id": pid}, "Verify milestone updates", "verification"))
        q.append(_call("list_status_reports", {"project_id": pid}, "Verify the UI-submitted report exists", "verification"))
        for pr_id in self.s["expect"]["payments"]:
            q.append(_call("get_purchase_request", {"pr_id": pr_id}, f"Verify financial state of {pr_id}", "verification"))
        q.append(_call("ui_view_project", {"project_id": pid}, "Capture final state as seen in the UI", "verification"))
        self.s["phase"] = "verify"

    def _conclude(self) -> Finish:
        v, e = self.s["verify_obs"], self.s["expect"]
        checks = []
        tasks = {t["id"]: t for t in v.get("list_tasks") or []}
        for tid, status in e["tasks"].items():
            actual = tasks.get(tid, {}).get("status")
            checks.append({"check": f"task {tid} status", "expected": status, "actual": actual, "ok": actual == status})
        ms = {m["id"]: m for m in v.get("list_milestones") or []}
        for mid, health in e["milestones"].items():
            actual = ms.get(mid, {}).get("health")
            checks.append({"check": f"milestone {mid} health", "expected": health, "actual": actual, "ok": actual == health})
        report_ids = {r["id"] for r in v.get("list_status_reports") or []}
        for rid in e["reports"]:
            checks.append(
                {
                    "check": f"status report {rid} exists (submitted via UI)",
                    "expected": "present",
                    "actual": "present" if rid in report_ids else "missing",
                    "ok": rid in report_ids,
                }
            )
        for pr_id, expected in e["payments"].items():
            pr = v.get(f"get_purchase_request:{pr_id}") or {}
            actual = pr.get("status")
            paid = len(pr.get("payments") or [])
            exp_paid = 1 if expected.startswith("Paid") else 0
            checks.append({"check": f"{pr_id} status", "expected": expected, "actual": actual, "ok": actual == expected})
            checks.append({"check": f"{pr_id} payment records", "expected": exp_paid, "actual": paid, "ok": paid == exp_paid})
        verified = bool(checks) and all(c["ok"] for c in checks)
        self._set_plan("verify", "done" if verified else "failed")
        ok_actions = [a for a in self.s["actions_done"] if a["status"] == "ok"]
        headline = (
            f"Verified: {sum(c['ok'] for c in checks)}/{len(checks)} checks passed; {len(ok_actions)} write(s) executed."
            if verified
            else f"Verification failed: {sum(not c['ok'] for c in checks)} of {len(checks)} checks did not match."
        )
        return Finish(
            verified=verified,
            headline=headline,
            report={
                "project_id": self.s.get("project_id"),
                "findings": self.s["findings"],
                "actions": self.s["actions_done"],
                "approvals": self.s["approvals"],
                "escalations": self.s["escalations"],
                "checks": checks,
                "agent": "Deterministic demo agent (simulation - not Claude)",
            },
        )
