import hmac
import json
import os

import streamlit as st

# Streamlit Secrets -> environment variables read by app/llm.py
for _k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
    if _k in st.secrets:
        os.environ[_k] = str(st.secrets[_k])

import app.actions.core  # noqa: F401  (registers the safe actions)
from app.executor import execute
from app.logstore import get_run
from app.planner import make_plan
from app.schemas import Plan
from app.validator import PlanError, risky_steps, validate_plan

st.set_page_config(page_title="Safe Workflow Assistant", layout="wide")

ICONS = {
    "run_started": "▶️", "attempt_start": "⏳", "attempt_ok": "🟢",
    "attempt_failed": "🔴", "fallback_ok": "🟡", "fallback_failed": "🔴",
    "skipped": "⚪", "skipped_after_failure": "🟠", "dry_run": "🧪",
    "blocked_needs_approval": "⛔", "condition_error": "🔴",
    "run_aborted": "🛑", "run_finished": "🏁",
}
MAX_PLANS_PER_SESSION = 10

# ---------- password gate (fails closed) ----------
PASSWORD = str(st.secrets.get("APP_PASSWORD", ""))
ALLOW_REAL_RUN = str(st.secrets.get("ALLOW_REAL_RUN", "false")).lower() == "true"

st.title("Safe Workflow Assistant")
if not PASSWORD:
    st.error("This app is not configured: APP_PASSWORD is missing in Secrets.")
    st.stop()

if not st.session_state.get("authed"):
    entered = st.text_input("Password", type="password")
    if st.button("Enter"):
        if hmac.compare_digest(entered, PASSWORD):
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

st.caption("Describe a task. Review and edit the plan. Validate, dry run"
           + (", then run." if ALLOW_REAL_RUN else ". (Real runs are disabled in this demo.)"))

# ---------- 1. request ----------
request_text = st.text_area(
    "1. What do you want to automate?",
    placeholder="Fetch https://en.wikipedia.org/wiki/Git, and if it is over 500 words "
                "summarize it and save to notes/git.md. Retry the fetch twice.",
    height=100,
)
used = st.session_state.get("plans_used", 0)
if st.button("Generate plan", type="primary",
             disabled=not request_text.strip() or used >= MAX_PLANS_PER_SESSION):
    with st.spinner("Planning..."):
        try:
            plan = make_plan(request_text)
            st.session_state.plans_used = used + 1
            st.session_state.plan_text = json.dumps(plan.model_dump(), indent=2)
            st.session_state.needs_approval = risky_steps(plan)
            st.session_state.pop("run_id", None)
        except Exception as e:
            st.error(f"Planner failed: {e}")
if used >= MAX_PLANS_PER_SESSION:
    st.warning("Plan limit reached for this session. Reload the page to start over.")


def load_plan():
    try:
        plan = Plan.model_validate_json(st.session_state.get("plan_text", ""))
        validate_plan(plan)
        return plan
    except PlanError as e:
        st.error(f"Plan rejected by the validator:\n\n{e}")
    except Exception as e:
        st.error(f"Invalid plan: {e}")
    return None


# ---------- 2. edit / validate / run ----------
if "plan_text" in st.session_state:
    st.subheader("2. Review and edit the plan")
    if st.session_state.get("needs_approval"):
        st.warning("Steps that write files or use the network: "
                   + ", ".join(st.session_state.needs_approval))
    st.text_area("Plan (JSON, editable)", key="plan_text", height=420)

    c1, c2, c3 = st.columns(3)
    if c1.button("Validate"):
        if load_plan() is not None:
            st.success("Plan is valid.")
    if c2.button("Dry run (no writes, no network)"):
        plan = load_plan()
        if plan is not None:
            st.session_state.run_id = execute(plan, approved=False, dry_run=True)
    if ALLOW_REAL_RUN:
        reviewed = st.checkbox("I reviewed the plan, including write and network steps")
        if c3.button("Approve and run", disabled=not reviewed):
            plan = load_plan()
            if plan is not None:
                with st.spinner("Running..."):
                    st.session_state.run_id = execute(plan, approved=True, dry_run=False)

# ---------- 3. run log ----------
if "run_id" in st.session_state:
    rid = st.session_state.run_id
    st.subheader(f"3. Run log ({rid})")
    rows = get_run(rid)
    final = [r for r in rows if r[2] == "run_finished"]
    if final:
        status = json.loads(final[-1][3]).get("status", "?")
        {"success": st.success, "failed": st.error}.get(status, st.warning)(
            f"Run finished: {status}")
    for ts, step, event, data in rows:
        label = f"{ICONS.get(event, '▫️')} **{step or 'run'}** · {event}"
        with st.expander(label, expanded=event in ("attempt_failed", "run_aborted")):
            try:
                st.json(json.loads(data))
            except Exception:
                st.code(data)
