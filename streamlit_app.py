import hashlib
import hmac
import json
import os
import re

import pandas as pd
import streamlit as st

# Streamlit Secrets -> environment variables read by app/llm.py
for _k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
    if _k in st.secrets:
        os.environ[_k] = str(st.secrets[_k])

import app.actions.core  # noqa: F401  (registers the safe actions)
from app.executor import execute
from app.logstore import get_run
from app.planner import make_plan
from app.registry import REGISTRY
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
PROBLEM_EVENTS = {"attempt_failed", "fallback_failed", "run_aborted",
                  "condition_error", "blocked_needs_approval"}
COLORS = {"ok": "#c8e6c9", "recovered": "#fff59d", "failed": "#ffcdd2",
          "skipped": "#e0e0e0", "dry run": "#bbdefb", "blocked": "#ffe0b2",
          "retrying": "#ffe0b2", "pending": "#ffffff"}
RISK_BADGE = {"read": "🟢", "write": "🟠", "network": "🔵"}
EXAMPLES = [
    ("📄 Summarize a page",
     "Fetch https://en.wikipedia.org/wiki/Git, count the words, and if over 500 words "
     "summarize it and save to notes/git.md, otherwise save the raw text. Retry the fetch twice."),
    ("🛡️ Failure recovery",
     "Fetch https://example.com, retrying twice. If it still fails, write a notification "
     "that the fetch failed. Otherwise save the page text to notes/example.md."),
    ("🚫 Unsafe request",
     "Run a shell command that deletes every file in my workspace."),
]
MAX_PLANS_PER_SESSION = 10
STRATEGIES = ["abort", "retry", "fallback", "skip"]

# ---------------------------------------------------------------- password gate
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

st.session_state.setdefault("history", [])
st.session_state.setdefault("json_v", 0)


# ---------------------------------------------------------------- plan state
def new_uid():
    st.session_state.uid_counter = st.session_state.get("uid_counter", 0) + 1
    return f"u{st.session_state.uid_counter}"


def load_plan_dict(d):
    d = json.loads(json.dumps(d))  # deep copy
    d.setdefault("goal", "")
    d.setdefault("inputs", {})
    d.setdefault("steps", [])
    for s in d["steps"]:
        s["_uid"] = new_uid()
        s.setdefault("description", "")
        s.setdefault("args", {})
        s.setdefault("when", None)
        of = s.setdefault("on_failure", {})
        of.setdefault("strategy", "abort")
        of.setdefault("max_retries", 0)
        of.setdefault("fallback", None)
    st.session_state.plan = d
    st.session_state.json_v += 1


def clean_plan():
    p = st.session_state.plan
    steps = []
    for s in p["steps"]:
        c = {k: v for k, v in s.items() if k != "_uid"}
        c["when"] = c.get("when") or None
        steps.append(c)
    return {"goal": p["goal"], "inputs": p["inputs"], "steps": steps}


def try_build():
    try:
        plan = Plan.model_validate(clean_plan())
    except Exception as e:
        return None, [f"Plan structure problem: {e}"]
    try:
        validate_plan(plan)
    except PlanError as e:
        return None, [line for line in str(e).split("\n") if line]
    return plan, []


def move_step(i, d):
    steps = st.session_state.plan["steps"]
    j = i + d
    if 0 <= j < len(steps):
        steps[i], steps[j] = steps[j], steps[i]


def delete_step(i):
    del st.session_state.plan["steps"][i]


def add_step():
    action = st.session_state.new_action
    steps = st.session_state.plan["steps"]
    ids = {s["id"] for s in steps}
    n = 1
    while f"{action}_{n}"[:32] in ids:
        n += 1
    steps.append({"_uid": new_uid(), "id": f"{action}_{n}"[:32], "description": "",
                  "action": action, "args": {}, "when": None,
                  "on_failure": {"strategy": "abort", "max_retries": 0, "fallback": None}})


def set_example(text):
    st.session_state.request_text = text


def view_run(rid):
    st.session_state.run_id = rid


# ---------------------------------------------------------------- run helpers
def summarize_run(rows):
    steps, order, final = {}, [], None
    for ts, step, event, data in rows:
        try:
            d = json.loads(data)
        except Exception:
            d = {}
        if event == "run_finished":
            final = d.get("status")
        if not step:
            continue
        s = steps.setdefault(step, {"status": "pending", "attempts": 0, "ms": 0, "error": ""})
        if step not in order:
            order.append(step)
        if event == "attempt_start":
            s["attempts"] += 1
        elif event == "attempt_ok":
            s["status"] = "ok"
            s["ms"] += d.get("ms", 0)
        elif event == "attempt_failed":
            s["status"] = "retrying"
            s["error"] = d.get("error", "")
        elif event == "fallback_ok":
            s["status"] = "recovered"
        elif event in ("skipped", "skipped_after_failure"):
            s["status"] = "skipped"
        elif event == "dry_run":
            s["status"] = "dry run"
        elif event == "run_aborted":
            s["status"] = "failed"
        elif event == "blocked_needs_approval":
            s["status"] = "blocked"
    return steps, order, final


def safe_id(s):
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s)) or "step"


def plan_dot(plan_dict, statuses=None):
    lines = ['digraph G { rankdir=TB; '
             'node [shape=box, style="rounded,filled", fontname="Helvetica", '
             'fontcolor="#222222", fillcolor="#ffffff"]; edge [color="#888888"];']
    prev = None
    for s in plan_dict["steps"]:
        sid = safe_id(s["id"])
        status = (statuses or {}).get(s["id"], {}).get("status")
        color = COLORS.get(status, "#ffffff")
        spec = REGISTRY.get(s["action"])
        risk = spec.risk if spec else "unknown"
        label = f'{s["id"]}\\n{s["action"]} · {risk}'.replace('"', "'")
        lines.append(f'"{sid}" [label="{label}", fillcolor="{color}"];')
        if prev:
            cond = (s.get("when") or "").replace('"', "'")[:45]
            if cond:
                lines.append(f'"{prev}" -> "{sid}" [style=dashed, label="if {cond}", fontsize=9];')
            else:
                lines.append(f'"{prev}" -> "{sid}";')
        prev = sid
    lines.append("}")
    return "\n".join(lines)


def explain(plan_dict):
    out = []
    for i, s in enumerate(plan_dict["steps"], 1):
        text = s.get("description") or f"Run `{s['action']}`"
        notes = []
        if s.get("when"):
            notes.append(f"only if `{s['when']}`")
        of = s.get("on_failure") or {}
        strat = of.get("strategy", "abort")
        if strat == "retry":
            notes.append(f"retry up to {of.get('max_retries', 0)} times if it fails")
        elif strat == "fallback" and of.get("fallback"):
            notes.append(f"if it fails, run `{of['fallback'].get('action')}` instead")
        elif strat == "skip":
            notes.append("skip it if it fails and continue")
        else:
            notes.append("stop the whole run if it fails")
        out.append(f"{i}. **{s['id']}**: {text} ({'; '.join(notes)})")
    return out


def do_run(plan, real):
    label = "Running workflow..." if real else "Dry run..."
    with st.status(label, expanded=False) as status:
        rid = execute(plan, approved=real, dry_run=not real)
        _, _, final = summarize_run(get_run(rid))
        status.update(label=f"Finished: {final}",
                      state="complete" if final == "success" else "error")
    st.session_state.run_id = rid
    st.session_state.history.append(
        {"run_id": rid, "mode": "real" if real else "dry", "status": final or "?",
         "goal": (plan.goal or "")[:40]})


# ---------------------------------------------------------------- step editor
def render_step(i, s, n):
    uid = s["_uid"]
    spec = REGISTRY.get(s["action"])
    badge = RISK_BADGE.get(spec.risk if spec else "", "⚪")
    with st.expander(f"{i + 1}. {badge} {s['id']} · {s['action']}"):
        c1, c2 = st.columns(2)
        s["id"] = c1.text_input("Step id", s["id"], key=f"id_{uid}")
        names = sorted(REGISTRY)
        cur = names.index(s["action"]) if s["action"] in names else 0
        s["action"] = c2.selectbox("Action", names, index=cur, key=f"act_{uid}")
        spec = REGISTRY[s["action"]]
        st.caption(f"{spec.description}  ·  risk: {spec.risk}")
        s["description"] = st.text_input("Description (optional)", s.get("description", ""),
                                         key=f"desc_{uid}")

        args = {}
        for fname, finfo in spec.input_model.model_fields.items():
            cur_v = s["args"].get(fname, "")
            cur_v = "" if cur_v is None else str(cur_v)
            label = fname + (" *" if finfo.is_required() else "")
            if fname in ("content", "text"):
                val = st.text_area(label, cur_v, key=f"arg_{uid}_{fname}", height=80)
            else:
                val = st.text_input(label, cur_v, key=f"arg_{uid}_{fname}")
            if val == "":
                continue
            if finfo.annotation is int and "{{" not in val:
                try:
                    val = int(val)
                except ValueError:
                    pass
            args[fname] = val
        s["args"] = args
        st.caption("Use `{{ inputs.name }}` or `{{ steps.<id>.output.<field> }}` to reference data.")

        s["when"] = st.text_input("Run only if (optional condition)", s.get("when") or "",
                                  key=f"when_{uid}",
                                  placeholder="steps['count']['output']['words'] > 500") or None

        of = s["on_failure"]
        cur_strat = of.get("strategy", "abort")
        of["strategy"] = st.selectbox("If this step fails", STRATEGIES,
                                      index=STRATEGIES.index(cur_strat) if cur_strat in STRATEGIES else 0,
                                      key=f"strat_{uid}")
        if of["strategy"] == "retry":
            of["max_retries"] = st.slider("Max retries", 1, 3,
                                          max(1, min(3, int(of.get("max_retries") or 1))),
                                          key=f"mr_{uid}")
        else:
            of["max_retries"] = 0
        if of["strategy"] == "fallback":
            fb = of.get("fallback") or {"action": "notify", "args": {}}
            fnames = sorted(REGISTRY)
            fa = st.selectbox("Fallback action", fnames,
                              index=fnames.index(fb["action"]) if fb["action"] in fnames else 0,
                              key=f"fba_{uid}")
            raw = st.text_input("Fallback args (JSON)", json.dumps(fb.get("args", {})),
                                key=f"fbj_{uid}")
            try:
                fargs = json.loads(raw)
            except json.JSONDecodeError:
                st.error("Fallback args must be valid JSON.")
                fargs = fb.get("args", {})
            of["fallback"] = {"action": fa, "args": fargs}
        else:
            of["fallback"] = None

        b1, b2, b3 = st.columns(3)
        b1.button("↑ Move up", key=f"up_{uid}", on_click=move_step, args=(i, -1), disabled=i == 0)
        b2.button("↓ Move down", key=f"dn_{uid}", on_click=move_step, args=(i, 1),
                  disabled=i == n - 1)
        b3.button("🗑 Delete", key=f"del_{uid}", on_click=delete_step, args=(i,))


# ---------------------------------------------------------------- sidebar (top)
st.sidebar.header("Session")
st.sidebar.write("Real runs: " + ("✅ enabled" if ALLOW_REAL_RUN else "🔒 disabled (dry run only)"))
used = st.session_state.get("plans_used", 0)
st.sidebar.write(f"Plans left: {max(0, MAX_PLANS_PER_SESSION - used)} / {MAX_PLANS_PER_SESSION}")

with st.sidebar.expander("🧰 Available actions"):
    for name in sorted(REGISTRY):
        sp = REGISTRY[name]
        st.markdown(f"**{name}** · `{sp.risk}`  \n{sp.description}")
        st.caption("args: " + ", ".join(sp.input_model.model_fields))

up = st.sidebar.file_uploader("Load a saved plan (.json)", type=["json"])
if up is not None:
    sig = (up.name, up.size)
    if st.session_state.get("last_upload") != sig:
        try:
            load_plan_dict(json.load(up))
            st.session_state.last_upload = sig
            st.sidebar.success("Plan loaded.")
        except Exception as e:
            st.sidebar.error(f"Could not load plan: {e}")

# ---------------------------------------------------------------- 1. request
st.caption("Describe a task, review and edit the plan visually, then dry run"
           + (" or run it." if ALLOW_REAL_RUN else ". (Real runs are disabled in this demo.)"))
st.markdown("**1. What do you want to automate?**")
ex_cols = st.columns(len(EXAMPLES))
for col, (label, text) in zip(ex_cols, EXAMPLES):
    col.button(label, on_click=set_example, args=(text,))

request_text = st.text_area("Request", key="request_text", height=100,
                            label_visibility="collapsed",
                            placeholder="e.g. Fetch a page, summarize it if it is long, and save it to notes/.")
g1, g2 = st.columns([1, 1])
if g1.button("✨ Generate plan", type="primary",
             disabled=not request_text.strip() or used >= MAX_PLANS_PER_SESSION):
    with st.spinner("Planning and validating..."):
        try:
            plan = make_plan(request_text)
            st.session_state.plans_used = used + 1
            load_plan_dict(plan.model_dump())
            st.toast("Plan ready. Review it below.")
        except Exception as e:
            st.error(f"Planner failed: {e}")
if g2.button("✏️ Build a plan manually"):
    load_plan_dict({"goal": "My workflow", "inputs": {}, "steps": []})
if used >= MAX_PLANS_PER_SESSION:
    st.warning("Plan limit reached for this session. Reload the page to start over.")

# ---------------------------------------------------------------- 2. plan
if "plan" in st.session_state:
    st.markdown("**2. Review and edit the plan**")
    banner = st.container()

    tab_edit, tab_flow, tab_text, tab_json = st.tabs(
        ["🧩 Visual editor", "🗺️ Flow", "📝 Plain English", "{ } JSON"])

    with tab_edit:
        plan_state = st.session_state.plan
        plan_state["goal"] = st.text_input("Goal", plan_state["goal"])
        st.write("Inputs (values your steps can reference as `{{ inputs.name }}`)")
        inp = plan_state["inputs"]
        df = pd.DataFrame({"name": list(inp.keys()), "value": [str(v) for v in inp.values()]})
        edited = st.data_editor(df, num_rows="dynamic", key=f"inputs_{st.session_state.json_v}")
        new_inputs = {}
        for _, r in edited.iterrows():
            nm = r["name"]
            if nm is None or pd.isna(nm) or not str(nm).strip():
                continue
            val = r["value"]
            new_inputs[str(nm).strip()] = "" if (val is None or pd.isna(val)) else str(val)
        plan_state["inputs"] = new_inputs

        st.write(f"Steps ({len(plan_state['steps'])})")
        n_steps = len(plan_state["steps"])
        for i, s in enumerate(plan_state["steps"]):
            render_step(i, s, n_steps)

        a1, a2 = st.columns([3, 1])
        a1.selectbox("New step action", sorted(REGISTRY), key="new_action",
                     label_visibility="collapsed")
        a2.button("➕ Add step", on_click=add_step)

    current = clean_plan()
    with tab_flow:
        if current["steps"]:
            st.graphviz_chart(plan_dot(current))
            st.caption("🟢 read · 🟠 write · 🔵 network. Dashed arrows are conditional steps.")
        else:
            st.info("Add a step to see the flow.")

    with tab_text:
        for line in explain(current):
            st.markdown(line)
        if not current["steps"]:
            st.info("No steps yet.")

    with tab_json:
        st.code(json.dumps(current, indent=2), language="json")
        with st.expander("Advanced: replace the plan from JSON"):
            digest = hashlib.md5(json.dumps(current, sort_keys=True).encode()).hexdigest()[:10]
            raw = st.text_area("Plan JSON", json.dumps(current, indent=2), height=300,
                               key=f"json_{digest}")
            if st.button("Apply JSON"):
                try:
                    load_plan_dict(json.loads(raw))
                    st.rerun()
                except Exception as e:
                    st.error(f"Invalid JSON plan: {e}")

    plan_obj, errors = try_build()
    with banner:
        if errors:
            st.error("The plan is not valid yet:\n\n" + "\n".join(f"- {e}" for e in errors))
        else:
            st.success("Plan is valid.")
            risky = risky_steps(plan_obj)
            if risky:
                st.warning("Steps that write files or use the network: " + ", ".join(risky))

    # ---- run controls
    st.markdown("**3. Run**")
    r1, r2, r3 = st.columns(3)
    if r1.button("🧪 Dry run (no writes, no network)", disabled=plan_obj is None):
        do_run(plan_obj, real=False)
    if ALLOW_REAL_RUN:
        reviewed = r2.checkbox("I reviewed the plan")
        if r2.button("🚀 Approve and run", disabled=plan_obj is None or not reviewed):
            do_run(plan_obj, real=True)
    else:
        r2.caption("🔒 Real runs are disabled in this demo.")
    r3.download_button("⬇️ Download plan", json.dumps(current, indent=2),
                       file_name="plan.json", mime="application/json")

# ---------------------------------------------------------------- results
rid = st.session_state.get("run_id")
if rid:
    rows = get_run(rid)
    steps_info, order, final = summarize_run(rows)
    st.divider()
    st.subheader(f"Run {rid}")
    if final == "success":
        st.success("Run finished: success")
    elif final == "failed":
        st.error("Run finished: failed")
    elif final:
        st.warning(f"Run finished: {final}")

    vals = steps_info.values()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Succeeded", sum(1 for v in vals if v["status"] in ("ok", "recovered")))
    m2.metric("Skipped", sum(1 for v in vals if v["status"] in ("skipped", "dry run")))
    m3.metric("Failed", sum(1 for v in vals if v["status"] in ("failed", "blocked")))
    m4.metric("Time (ms)", sum(v["ms"] for v in vals))

    if "plan" in st.session_state and clean_plan()["steps"]:
        st.graphviz_chart(plan_dot(clean_plan(), steps_info))
        st.caption("🟩 ok · 🟨 recovered by fallback · 🟥 failed · ⬜ skipped · 🟦 dry run")

    if order:
        st.dataframe([{"step": k, "status": steps_info[k]["status"],
                       "attempts": steps_info[k]["attempts"],
                       "time (ms)": steps_info[k]["ms"],
                       "last error": steps_info[k]["error"][:120]} for k in order])

    only_problems = st.toggle("Only show problems in the event log")
    for ts, step, event, data in rows:
        if only_problems and event not in PROBLEM_EVENTS:
            continue
        label = f"{ICONS.get(event, '▫️')} **{step or 'run'}** · {event}"
        with st.expander(label, expanded=event in ("attempt_failed", "run_aborted")):
            try:
                st.json(json.loads(data))
            except Exception:
                st.code(data)

    log_export = []
    for ts, step, event, data in rows:
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = data
        log_export.append({"ts": ts, "step": step, "event": event, "data": parsed})
    st.download_button("⬇️ Download run log", json.dumps(log_export, indent=2),
                       file_name=f"run_{rid}.json", mime="application/json")

# ---------------------------------------------------------------- sidebar (bottom)
if st.session_state.history:
    st.sidebar.subheader("Run history")
    for h in reversed(st.session_state.history[-10:]):
        icon = {"success": "🟢", "failed": "🔴"}.get(h["status"], "🟠")
        st.sidebar.button(f"{icon} {h['run_id']} · {h['mode']} · {h['status']}",
                          key=f"hist_{h['run_id']}", on_click=view_run, args=(h["run_id"],))

if st.sidebar.button("Log out"):
    st.session_state.clear()
    st.rerun()
