import json
import os

import requests
import streamlit as st

API = os.environ.get("API_URL", "http://127.0.0.1:8000")
ICONS = {
    "run_started": "▶️", "attempt_start": "⏳", "attempt_ok": "🟢",
    "attempt_failed": "🔴", "fallback_ok": "🟡", "fallback_failed": "🔴",
    "skipped": "⚪", "skipped_after_failure": "🟠", "dry_run": "🧪",
    "blocked_needs_approval": "⛔", "condition_error": "🔴",
    "run_aborted": "🛑", "run_finished": "🏁",
}

st.set_page_config(page_title="Safe Workflow Assistant", layout="wide")
st.title("Safe Workflow Assistant")
st.caption("Describe a task. Review and edit the plan. Validate, dry run, then run.")


def call(method, path, **kwargs):
    try:
        return requests.request(method, API + path, timeout=180, **kwargs)
    except requests.RequestException as e:
        st.error(f"Cannot reach the API at {API}. Is uvicorn running? ({e})")
        return None


def parse_plan():
    try:
        return json.loads(st.session_state.get("plan_text", ""))
    except json.JSONDecodeError as e:
        st.error(f"The plan is not valid JSON: {e}")
        return None


def show_error(r):
    try:
        st.error(r.json().get("detail", r.text))
    except Exception:
        st.error(r.text)


# ---------- 1. Request ----------
request_text = st.text_area(
    "1. What do you want to automate?",
    placeholder="Fetch https://en.wikipedia.org/wiki/Git, and if it is over 500 words "
                "summarize it and save to notes/git.md. Retry the fetch twice.",
    height=100,
)
if st.button("Generate plan", type="primary", disabled=not request_text.strip()):
    with st.spinner("Planning..."):
        r = call("POST", "/plan", json={"request": request_text})
    if r is not None and r.ok:
        data = r.json()
        st.session_state.plan_text = json.dumps(data["plan"], indent=2)
        st.session_state.needs_approval = data["needs_approval"]
        st.session_state.pop("run_id", None)
    elif r is not None:
        show_error(r)

# ---------- 2. Edit / validate / run ----------
if "plan_text" in st.session_state:
    st.subheader("2. Review and edit the plan")
    if st.session_state.get("needs_approval"):
        st.warning("Steps that write files or use the network: "
                   + ", ".join(st.session_state.needs_approval))

    st.text_area("Plan (JSON, editable)", key="plan_text", height=420)
    reviewed = st.checkbox("I reviewed the plan, including write and network steps")

    c1, c2, c3 = st.columns(3)
    if c1.button("Validate"):
        plan = parse_plan()
        if plan is not None:
            r = call("POST", "/validate", json=plan)
            if r is not None and r.ok:
                st.success("Plan is valid.")
            elif r is not None:
                show_error(r)

    if c2.button("Dry run (no writes, no network)"):
        plan = parse_plan()
        if plan is not None:
            r = call("POST", "/run", params={"dry_run": "true"}, json=plan)
            if r is not None and r.ok:
                st.session_state.run_id = r.json()["run_id"]
            elif r is not None:
                show_error(r)

    if c3.button("Approve and run", disabled=not reviewed):
        plan = parse_plan()
        if plan is not None:
            with st.spinner("Running..."):
                r = call("POST", "/run", params={"approved": "true"}, json=plan)
            if r is not None and r.ok:
                st.session_state.run_id = r.json()["run_id"]
            elif r is not None:
                show_error(r)

# ---------- 3. Run log ----------
if "run_id" in st.session_state:
    rid = st.session_state.run_id
    st.subheader(f"3. Run log ({rid})")
    r = call("GET", f"/runs/{rid}")
    if r is not None and r.ok:
        rows = r.json()
        final = [row for row in rows if row[2] == "run_finished"]
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
