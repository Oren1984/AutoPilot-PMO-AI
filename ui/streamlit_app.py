"""AutoPilot PMO AI - operator console (Streamlit).

Developed and Built by Oren Salami — V1.0
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import httpx
import pandas as pd
import streamlit as st

BACKEND = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
ENTERPRISE_INTERNAL = os.environ.get("ENTERPRISE_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
ENTERPRISE_PUBLIC = os.environ.get("ENTERPRISE_PUBLIC_URL", "http://localhost:8100").rstrip("/")
APPROVER_KEY = os.environ.get("APPROVER_API_KEY", "")
ADMIN_TOKEN = os.environ.get("ENTERPRISE_ADMIN_TOKEN", "")
CREDIT = "Developed and Built by Oren Salami — V1.0"

DEFAULT_ASSIGNMENT = (
    "Review the active project, identify overdue tasks and delivery risks, update permitted task statuses, "
    "prepare the next project actions, review the pending financial request, and complete all authorized work. "
    "Stop for human approval before any financial commitment or payment."
)
FLOW = ["CREATED", "PLANNING", "RUNNING", "WAITING_APPROVAL", "RESUMING", "VERIFYING", "COMPLETED"]
TERMINAL = {"COMPLETED", "REJECTED", "BLOCKED", "FAILED", "CANCELLED"}
STATE_STYLE = {
    "CREATED": ("#5b6578", "Created"),
    "PLANNING": ("#0b5cad", "Planning"),
    "RUNNING": ("#0b5cad", "Running"),
    "PAUSED": ("#7a5af8", "Paused"),
    "WAITING_APPROVAL": ("#b54708", "Awaiting Approval"),
    "RESUMING": ("#0b5cad", "Resuming"),
    "VERIFYING": ("#0e7490", "Verifying"),
    "COMPLETED": ("#1f7a3a", "Completed"),
    "REJECTED": ("#a15c00", "Completed — Approval Rejected"),
    "BLOCKED": ("#b42318", "Blocked"),
    "FAILED": ("#b42318", "Failed"),
    "CANCELLED": ("#5b6578", "Cancelled"),
}

st.set_page_config(page_title="AutoPilot PMO AI", page_icon="🧭", layout="wide")
st.markdown(
    """
<style>
.block-container { padding-top: 1.6rem; }
.ap-banner { border-radius: 10px; padding: 14px 18px; color: #fff; font-weight: 600; font-size: 1.15rem; margin-bottom: 8px; }
.ap-chip { display:inline-block; padding:3px 10px; margin:2px 4px 2px 0; border-radius:12px; font-size:0.78rem; border:1px solid #d0d5dd; color:#475467; }
.ap-chip.on { background:#0b5cad; color:#fff; border-color:#0b5cad; }
.ap-chip.done { background:#e6f4ea; color:#1f7a3a; border-color:#9ccfab; }
.ap-credit { color:#667085; font-size:0.8rem; text-align:center; margin-top:24px; }
.ap-sim { background:#fff4d6; color:#6b4a00; border-radius:6px; padding:4px 10px; font-size:0.8rem; display:inline-block; }
</style>
""",
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------- backend helpers
def api(method: str, path: str, **kw):
    try:
        r = httpx.request(method, BACKEND + path, timeout=30, **kw)
    except httpx.HTTPError as exc:
        return None, f"Backend unreachable ({type(exc).__name__}). Is the backend container running?"
    if r.status_code >= 400:
        try:
            return None, r.json().get("detail", r.text)
        except ValueError:
            return None, r.text
    if r.headers.get("content-type", "").startswith("image/"):
        return r.content, None
    return r.json(), None


@st.cache_data(ttl=10, show_spinner=False)
def get_config():
    return api("GET", "/api/config")


@st.cache_data(ttl=10, show_spinner=False)
def get_connection():
    return api("GET", "/api/connection")


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ts


# ----------------------------------------------------------------------------- header + sidebar
cfg, cfg_err = get_config()
conn, _ = get_connection()

head_l, head_r = st.columns([3, 2])
with head_l:
    st.title("🧭 AutoPilot PMO AI")
    st.caption(
        "Autonomous PMO operations inside an enterprise application — API + browser UI, governed by policy and human approval."
    )
with head_r:
    if cfg:
        mode = cfg["mode"].upper()
        color = "#1f7a3a" if mode == "DEMO" else "#0b5cad"
        st.markdown(
            f"<div style='text-align:right;margin-top:18px'><span class='ap-chip on' style='background:{color};border-color:{color}'>"
            f"{mode} MODE</span><span class='ap-chip'>{cfg['agent_label']}</span></div>",
            unsafe_allow_html=True,
        )

with st.sidebar:
    st.subheader("Environment")
    if cfg_err:
        st.error(cfg_err)
    elif cfg:
        st.write(f"**Mode:** {cfg['mode'].title()}")
        st.write(f"**Agent:** {cfg['agent_label']}")
        if cfg["mode"] == "demo":
            st.markdown(
                "<span class='ap-sim'>Simulation: deterministic agent + local demo enterprise app. Payments are simulated.</span>",
                unsafe_allow_html=True,
            )
    if conn:
        ent = conn.get("enterprise", {})
        api_ok = ent.get("api", {}).get("ok")
        detail = ent.get("api", {}).get("detail", "")
        detail = "connected" if api_ok else str(detail)[:80]
        st.write(("🟢" if api_ok else "🔴") + f" Enterprise API — {detail}")
        if "ui" in ent:
            st.write(("🟢" if ent["ui"].get("ok") else "🔴") + " Browser automation — " + ent["ui"].get("detail", "")[:60])
        cl = conn.get("claude", {})
        st.write(("⚪" if cl.get("ok") is None else "🟢" if cl.get("ok") else "🔴") + " Claude — " + cl.get("detail", ""))
    st.divider()
    st.subheader("Approver")
    st.text_input("Your name (recorded in the audit trail)", value="Oren Salami", key="approver")
    if not APPROVER_KEY:
        st.warning("APPROVER_API_KEY is not set for this console; approvals will be refused.")
    st.divider()
    if cfg and cfg["mode"] == "demo":
        st.link_button("Open demo enterprise app ↗", ENTERPRISE_PUBLIC + "/ui/")
        if st.button("Reset demo enterprise data", help="Restores the seeded demo records so the scenario can be repeated."):
            try:
                r = httpx.post(
                    ENTERPRISE_INTERNAL + "/api/admin/reset", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=10
                )
                st.success("Demo data reset.") if r.status_code == 200 else st.error(f"Reset failed: HTTP {r.status_code}")
            except httpx.HTTPError as exc:
                st.error(f"Reset failed: {type(exc).__name__}")
    st.markdown(f"<div class='ap-credit'>{CREDIT}</div>", unsafe_allow_html=True)


tab_ops, tab_records, tab_diag, tab_help = st.tabs(
    ["🧭 Agent Operations", "🗂️ Enterprise Records", "🩺 Diagnostics", "❓ Help Center"]
)

# ----------------------------------------------------------------------------- Agent Operations
with tab_ops:
    with st.container(border=True):
        assignment = st.text_area("Assignment for the agent", value=DEFAULT_ASSIGNMENT, height=90)
        c1, c2 = st.columns([1, 5])
        if c1.button("▶ Start Task", type="primary", width="stretch"):
            data, err = api("POST", "/api/runs", json={"assignment": assignment})
            if err:
                st.error(f"Could not start the task: {err}")
            else:
                st.session_state["run_id"] = data["id"]
                st.session_state["selected_event"] = None
                st.rerun()
        c2.caption(
            "The agent plans and executes on its own. It pauses automatically at any financial or high-risk action and waits for your decision."
        )

    runs, _ = api("GET", "/api/runs?limit=20")
    runs = runs or []
    if not runs:
        st.info("No runs yet. Press **Start Task** to launch the agent.")
    else:
        ids = [r["id"] for r in runs]
        current = st.session_state.get("run_id") or ids[0]
        if current not in ids:
            current = ids[0]
        labels = {
            r["id"]: f"{r['id']} · {STATE_STYLE.get(r['state'], ('', r['state']))[1]} · {fmt_ts(r['created_at'])}" for r in runs
        }
        sel = st.selectbox("Run", ids, index=ids.index(current), format_func=lambda i: labels[i])
        st.session_state["run_id"] = sel

        @st.fragment(run_every=2)
        def live_run(run_id: str):
            run, err = api("GET", f"/api/runs/{run_id}")
            if err:
                st.error(err)
                return
            events, _ = api("GET", f"/api/runs/{run_id}/events")
            events = events or []
            state = run["state"]
            color, label = STATE_STYLE.get(state, ("#5b6578", state))

            # --- state banner + flow
            if state == "WAITING_APPROVAL":
                st.markdown(
                    f"<div class='ap-banner' style='background:{color}'>⏸ WAITING FOR HUMAN APPROVAL — execution is paused</div>",
                    unsafe_allow_html=True,
                )
            else:
                icon = {
                    "COMPLETED": "✅",
                    "FAILED": "❌",
                    "BLOCKED": "⛔",
                    "CANCELLED": "⏹",
                    "REJECTED": "🛑",
                    "PAUSED": "⏸",
                }.get(state, "⚙️")
                st.markdown(
                    f"<div class='ap-banner' style='background:{color}'>{icon} {label}"
                    + (f" — {run['current_step']}" if run.get("current_step") and state not in TERMINAL else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )
            seen = {e["state"] for e in events if e["type"] == "STATE"} | {"CREATED"}
            chips = ""
            for s in FLOW:
                cls = "on" if s == state else ("done" if s in seen else "")
                chips += f"<span class='ap-chip {cls}'>{s.replace('_', ' ')}</span>→"
            if state in TERMINAL - {"COMPLETED"}:
                chips += f"<span class='ap-chip on' style='background:{color};border-color:{color}'>{state}</span>"
            st.markdown(chips.rstrip("→"), unsafe_allow_html=True)
            if run.get("error"):
                st.warning(run["error"])

            # --- usage metrics
            u = run.get("usage") or {}
            m = st.columns(7)
            m[0].metric("Steps", run.get("step_count", 0))
            m[1].metric("Tool calls", u.get("tool_calls", 0))
            m[2].metric("API calls", u.get("api_calls", 0))
            m[3].metric("UI operations", u.get("ui_operations", 0))
            m[4].metric("Model calls", u.get("model_calls", 0))
            m[5].metric("Est. model cost", f"${u.get('estimated_model_cost_usd', 0):.4f}")
            m[6].metric("Approvals", u.get("approvals_requested", 0))
            st.caption(f"Cost basis: {u.get('cost_basis', 'n/a')}")

            # --- approval card
            appr = run.get("pending_approval")
            if state == "WAITING_APPROVAL" and appr:
                ctx = appr.get("context") or {}
                with st.container(border=True):
                    st.subheader("🛑 Approval Required")
                    a1, a2 = st.columns(2)
                    a1.markdown(f"**Requested action:** `{appr['action']}` via {appr['method'].upper()}")
                    a1.markdown(f"**Amount:** {ctx.get('amount', 0):,.2f} {ctx.get('currency', '')}")
                    a1.markdown(f"**Recipient:** {ctx.get('vendor', '')} ({ctx.get('recipient_account_masked', '')})")
                    a1.markdown(f"**Project:** {ctx.get('project_id', '')} — {ctx.get('project_name', '')}")
                    a1.markdown(f"**Request:** {ctx.get('title', '')} (requested by {ctx.get('requested_by', '')})")
                    a2.markdown(f"**Business justification:** {ctx.get('justification', '')}")
                    a2.markdown(f"**Agent rationale:** {ctx.get('rationale', '')}")
                    a2.markdown("**Risks:** " + "; ".join(ctx.get("risks") or ["none identified"]))
                    a2.markdown(f"**Policy decision:** {ctx.get('policy_level', '')} — {ctx.get('policy_reason', '')}")
                    a2.markdown(
                        f"**Budget remaining:** {ctx.get('budget_remaining', 0):,.0f} · **Expires:** {fmt_ts(appr['expires_at'])}"
                    )
                    if ctx.get("simulated"):
                        st.info(
                            "SIMULATED financial processing: approving records a simulated payment in the demo app. No real funds move."
                        )
                    comment = st.text_input("Decision comment (optional)", key=f"cmt-{appr['id']}")
                    b1, b2, _ = st.columns([1, 1, 4])
                    for btn, decision, kind in ((b1, "approve", "primary"), (b2, "reject", "secondary")):
                        if btn.button(
                            "✔ Approve" if decision == "approve" else "✖ Reject",
                            key=f"{decision}-{appr['id']}",
                            type=kind,
                            width="stretch",
                        ):
                            _, derr = api(
                                "POST",
                                f"/api/approvals/{appr['id']}/decision",
                                json={"decision": decision, "approver": st.session_state.get("approver", ""), "comment": comment},
                                headers={"X-Approver-Key": APPROVER_KEY},
                            )
                            if derr:
                                st.error(f"Decision refused: {derr}")
                            else:
                                st.success(f"Decision recorded: {decision.upper()}")

            # --- controls
            k1, k2, k3, _ = st.columns([1, 1, 1, 5])
            if state in {"PLANNING", "RUNNING", "VERIFYING", "RESUMING"} and k1.button("⏸ Pause", width="stretch"):
                api("POST", f"/api/runs/{run_id}/pause")
            if state == "PAUSED" and k2.button("▶ Resume", width="stretch"):
                api("POST", f"/api/runs/{run_id}/resume")
            if state not in TERMINAL and k3.button("⏹ Cancel", width="stretch"):
                api("POST", f"/api/runs/{run_id}/cancel")

            # --- final result
            res = run.get("result")
            if res and state in TERMINAL:
                with st.container(border=True):
                    st.subheader("Final result")
                    (st.success if res.get("verified") else st.error)(res.get("headline", ""))
                    rep = res.get("report") or {}
                    if rep.get("checks"):
                        st.markdown("**Verification checks**")
                        st.dataframe(
                            pd.DataFrame(rep["checks"]).reindex(columns=["check", "expected", "actual", "ok"]).astype(str),
                            hide_index=True,
                            width="stretch",
                        )
                    r1, r2 = st.columns(2)
                    with r1:
                        st.markdown("**Approval decisions**")
                        for a in rep.get("approvals") or []:
                            st.write(f"- {a.get('pr_id')}: {a.get('outcome')}")
                        for a in res.get("approvals") or []:
                            st.caption(f"{a['id']} · {a['status']} · {a.get('decided_by') or ''}")
                        st.markdown("**Completed actions**")
                        for a in rep.get("actions") or []:
                            if a.get("status") == "ok":
                                st.write(
                                    f"- `{a['action']}` ({(a.get('method') or '').upper()}) {json.dumps({k: v for k, v in a['params'].items() if k != 'body' and k != 'summary'})}"
                                )
                        for a in rep.get("completed_actions") or []:
                            st.write(f"- {a}")
                    with r2:
                        st.markdown("**Findings & risks**")
                        for f in rep.get("findings") or []:
                            st.write(f"- {f['text'] if isinstance(f, dict) else f}")
                        warn = [a for a in rep.get("actions") or [] if a.get("status") not in ("ok",)]
                        if warn:
                            st.markdown("**Warnings / not executed**")
                            for a in warn:
                                st.write(f"- `{a['action']}`: {a.get('status')} — {a.get('message')}")
                    st.caption(f"Duration {res.get('duration_seconds')}s · Agent: {rep.get('agent', '')}")

            # --- plan + timeline + step details
            left, right = st.columns([3, 2])
            with left:
                plan = run.get("plan") or []
                if plan:
                    st.markdown("**Execution plan** (built and updated by the agent)")
                    for p in plan:
                        mark = {"done": "✅", "in_progress": "🔄", "failed": "❌"}.get(p["status"], "▫️")
                        st.write(f"{mark} {p['title']}")
                st.markdown("**Execution timeline** (live events)")
                rows = [
                    {
                        "#": e["id"],
                        "time": fmt_ts(e["ts"]),
                        "step": e["step"] or "",
                        "event": e["type"],
                        "action": e["action"] or "",
                        "method": e["method"] or "",
                        "result": e["result"] or "",
                        "policy": e["policy"] or "",
                        "ms": e["duration_ms"] if e["duration_ms"] is not None else "",
                        "cost $": "" if e["cost_usd"] in (None, 0, 0.0) else f"{e['cost_usd']:.4f}",
                        "message": (e["message"] or "")[:140],
                    }
                    for e in events
                ]
                st.dataframe(pd.DataFrame(rows).astype(str), hide_index=True, width="stretch", height=420)
            with right:
                st.markdown("**Step details**")
                if events:
                    with_shot = [e for e in events if e.get("screenshot")]
                    options = [e["id"] for e in events]
                    follow = st.toggle("Follow latest step", value=True, key=f"follow-{run_id}")
                    if follow:
                        eid = options[-1]
                    else:
                        eid = st.selectbox(
                            "Event",
                            options[::-1],
                            format_func=lambda i: next(
                                f"#{e['id']} {e['type']} {e['action'] or ''} {e['result'] or ''}" for e in events if e["id"] == i
                            ),
                            key=f"evsel-{run_id}",
                        )
                    ev = next(e for e in events if e["id"] == eid)
                    st.write(f"**{ev['type']}** · {ev['action'] or ''} · {ev['method'] or ''} · {ev['result'] or ''}")
                    if ev.get("message"):
                        st.write(ev["message"])
                    d = ev.get("detail") or {}
                    if d.get("rationale"):
                        st.caption(f"Why: {d['rationale']}")
                    if d.get("policy"):
                        st.caption(f"Policy: {d['policy'].get('decision')} · {d['policy'].get('reason')}")
                    if d.get("browser_steps"):
                        st.markdown("Browser actions:")
                        for b in d["browser_steps"]:
                            st.write(f"• {b}")
                    shot = ev.get("screenshot") or (
                        with_shot[-1]["screenshot"]
                        if with_shot and not ev.get("screenshot") and ev["type"] != "TOOL_CALL"
                        else None
                    )
                    if shot:
                        img, ierr = api("GET", f"/api/screenshots/{shot}")
                        if img:
                            st.image(img, caption=f"Browser snapshot (sanitized): {shot.split('/')[-1]}", width="stretch")
                    with st.expander("Raw event detail"):
                        st.json(d)

        live_run(sel)

# ----------------------------------------------------------------------------- Enterprise records
with tab_records:
    st.caption("Read-only view of the records in the enterprise application (fetched live; not part of any agent run).")
    snap, serr = api("GET", "/api/enterprise/snapshot?project_id=PRJ-1001")
    if serr:
        st.warning(f"Enterprise snapshot unavailable: {serr}")
    elif snap:
        p = snap["project"]
        st.subheader(f"{p['id']} — {p['name']}")
        k = st.columns(4)
        k[0].metric("Budget", f"{p['budget_total']:,.0f} {p['currency']}")
        k[1].metric("Committed", f"{p['budget_committed']:,.0f}")
        k[2].metric("Remaining", f"{p['budget_remaining']:,.0f}")
        k[3].metric("Health", p["health"])
        for title, key, cols in (
            ("Tasks", "tasks", ["id", "title", "owner", "due_date", "status", "priority", "version"]),
            ("Milestones", "milestones", ["id", "name", "due_date", "status", "health", "version"]),
            ("Purchase requests", "purchase_requests", ["id", "title", "vendor", "amount", "currency", "status", "version"]),
            ("Status reports", "status_reports", ["id", "rag", "author", "channel", "created_at"]),
        ):
            st.markdown(f"**{title}**")
            df = pd.DataFrame(snap[key])
            if df.empty:
                st.caption("none")
            else:
                st.dataframe(df[[c for c in cols if c in df.columns]].astype(str), hide_index=True, width="stretch")

# ----------------------------------------------------------------------------- Diagnostics
with tab_diag:
    diag, derr = api("GET", "/api/diagnostics?limit=200")
    rid = st.session_state.get("run_id")
    if derr:
        st.error(derr)
    elif diag:
        evs = diag["recent_events"]
        with st.expander("Runtime limits", expanded=True):
            st.json(diag["limits"])
        if rid:
            run, _ = api("GET", f"/api/runs/{rid}")
            if run:
                with st.expander(f"Usage and estimated costs — {rid}", expanded=True):
                    st.json(run.get("usage") or {})
                    st.caption(
                        "Model costs are estimates from provider-reported tokens × configured prices; 'unavailable' when no pricing is configured."
                    )
                audit, _ = api("GET", f"/api/runs/{rid}/audit")
                if audit:
                    st.download_button(
                        "⬇ Export audit trail (JSON)",
                        json.dumps(audit, indent=2, default=str),
                        file_name=f"audit-{rid}.json",
                        mime="application/json",
                    )
        with st.expander("Tool calls"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {k: e[k] for k in ("id", "run_id", "ts", "action", "method", "result", "policy", "duration_ms")}
                        for e in evs
                        if e["type"] == "TOOL_CALL"
                    ]
                ).astype(str),
                hide_index=True,
                width="stretch",
            )
        with st.expander("Policy decisions"):
            st.dataframe(
                pd.DataFrame(
                    [{k: e[k] for k in ("id", "run_id", "type", "action", "policy", "message")} for e in evs if e["policy"]]
                ).astype(str),
                hide_index=True,
                width="stretch",
            )
        with st.expander("Errors, blocks and warnings"):
            bad = [
                e
                for e in evs
                if (e["result"] or "") in ("ERROR", "BLOCKED", "FAILED", "UNCERTAIN", "INVALIDATED", "EXPIRED")
                or e["type"] in ("ERROR", "WARNING", "SECURITY")
            ]
            st.dataframe(
                pd.DataFrame([{k: e[k] for k in ("id", "run_id", "type", "action", "result", "message")} for e in bad]).astype(
                    str
                )
                if bad
                else pd.DataFrame(),
                hide_index=True,
                width="stretch",
            )
        with st.expander("Recent events (all runs)"):
            st.dataframe(
                pd.DataFrame(
                    [{k: e[k] for k in ("id", "run_id", "ts", "type", "state", "action", "result", "message")} for e in evs]
                ).astype(str),
                hide_index=True,
                width="stretch",
            )
        pol, _ = api("GET", "/api/policy")
        with st.expander("Active governance policy (read-only)"):
            st.json(pol or {})

# ----------------------------------------------------------------------------- Help Center
with tab_help:
    st.subheader("Help Center")
    helps = [
        (
            "1. What does this application do?",
            "AutoPilot runs an autonomous PMO agent inside an enterprise application. It reads project data, finds overdue work and risks, makes the updates it is allowed to make (via API and via the web UI), prepares financial actions and stops for your approval before any payment. Everything it does appears in the timeline.",
        ),
        (
            "2. How do I start a task?",
            "On **Agent Operations**, keep or edit the assignment text and press **Start Task**. The agent plans and executes by itself; you only step in when it asks for approval.",
        ),
        (
            "3. Demo Mode vs Connected Mode",
            "**Demo Mode** (default) needs no credentials: a deterministic simulated agent (not Claude) works against a local simulated enterprise app; payments are simulated. **Connected Mode** uses a real Claude agent (needs `ANTHROPIC_API_KEY`) and/or Salesforce (needs a connected app). The mode badge at the top shows which one is active.",
        ),
        (
            "4. How does the autonomous workflow operate?",
            "The agent discovers capabilities, reads the project, analyzes deadlines/milestones/budget, then acts. Structured operations go through the **API**; things only available on screen (the weekly status report) go through the **browser UI**. Every action passes the same policy check. Finally it re-reads the records to **verify** the result.",
        ),
        (
            "5. Why does the agent pause for approval?",
            "Payments, financial commitments and high-impact changes are **Level 2** actions: they always require an explicit human decision. Some actions (e.g. payments above the hard limit, permission changes) are **Level 3** and are never executed by the agent. The agent cannot change these rules.",
        ),
        (
            "6. How do I approve or reject?",
            "When the banner shows **WAITING FOR HUMAN APPROVAL**, review the card (amount, recipient, justification, risks, policy) and press **Approve** or **Reject**. Your name from the sidebar is recorded. Approval applies only to that exact action and amount; if the record changes afterwards, the approval is invalidated and nothing is paid.",
        ),
        (
            "7. Where are logs and costs?",
            "The **Execution timeline** shows every step with method, result, policy decision and duration. **Diagnostics** shows tool calls, policy decisions, errors, limits, usage/cost estimates, and lets you export the audit trail as JSON.",
        ),
        (
            "8. What if a task fails?",
            "Read the red message on the run banner and the ERROR rows in Diagnostics. Common causes: enterprise app not reachable (check containers), missing Claude/Salesforce credentials (switch to Demo Mode), an execution limit reached, or a record changed by someone else. Reset the demo data from the sidebar and start a new task. Uncertain financial actions are never retried automatically.",
        ),
    ]
    for q, a in helps:
        with st.expander(q):
            st.markdown(a)

st.markdown(f"<div class='ap-credit'>{CREDIT}</div>", unsafe_allow_html=True)
