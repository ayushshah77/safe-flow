import hmac
import os
import re

import streamlit as st

# Streamlit Secrets -> environment variables read by app/llm.py
for _k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
    if _k in st.secrets:
        os.environ[_k] = str(st.secrets[_k])

import app.actions.core  # noqa: F401  (registers the safe actions)
from app.actions.extra import end_capture, start_capture  # registers llm_answer and respond
from app.executor import execute
from app.llm import model_name
from app.logstore import get_run
from app.planner import make_plan

st.set_page_config(page_title="Safe Workflow Assistant", page_icon="🛡️", layout="centered")

DEMO_MODE = str(st.secrets.get("DEMO_MODE", "true")).lower() == "true"
APP_PASSWORD = str(st.secrets.get("APP_PASSWORD", ""))
try:
    MAX_PLANS = int(st.secrets.get("MAX_PLANS", 0))  # 0 = unlimited
except (TypeError, ValueError):
    MAX_PLANS = 0
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

EXAMPLES = [
    ("Write code", "Write a Python function that adds two numbers."),
    ("Summarize a page", "Summarize https://en.wikipedia.org/wiki/Git in 3 sentences."),
    ("Count words", "How many words are in this sentence: the quick brown fox jumps over the lazy dog"),
]

PLANNER_HINT = (
    "\n\n(Plan requirements: the last step must use the 'respond' action, and its 'text' must contain "
    "only the final result the user asked for, built from earlier step outputs with templates such as "
    "{{ steps.answer.output.text }}. Use 'llm_answer' for questions, writing and code that need no web "
    "or files. Use http_get or file actions only when the request needs them, and do not save files "
    "unless asked. If the request is unsupported or unsafe, use a single 'respond' step that says so briefly.)"
)

st.title("🛡️ Safe Workflow Assistant")

# ------------------------------------------------------------------ login
if not st.session_state.get("user"):
    if DEMO_MODE:
        st.info("Demo mode: enter any password and a valid email address.")
    elif not APP_PASSWORD:
        st.error("This app is not configured: APP_PASSWORD is missing in Secrets.")
        st.stop()
    with st.form("login"):
        email = st.text_input("Email", placeholder="name@example.com")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        email = email.strip()
        if not EMAIL_RE.match(email):
            st.error("Enter a valid email address, like name@example.com.")
        elif not password:
            st.error("Enter a password.")
        elif not DEMO_MODE and not hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
            st.error("Wrong password.")
        else:
            st.session_state.user = email
            st.rerun()
    st.stop()

with st.sidebar:
    st.caption(f"Signed in as {st.session_state.user}")
    st.caption(f"Model: {model_name()}")
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()


# ------------------------------------------------------------------ pipeline
def last_error(run_id):
    msg = ""
    for _ts, _step, event, data in get_run(run_id):
        if event in ("attempt_failed", "fallback_failed", "run_aborted"):
            try:
                import json
                msg = json.loads(data).get("error", "") or msg
            except Exception:
                msg = data
    return msg


def run_request(text):
    """Plan, validate and execute in the background. Returns (ok, text)."""
    start_capture()
    run_id, plan_error = None, ""
    try:
        plan = make_plan(text.strip() + PLANNER_HINT)
        run_id = execute(plan, approved=True, dry_run=False)
    except Exception as e:
        plan_error = str(e)
    finally:
        results = end_capture()

    if results:
        return True, results[-1]
    if run_id is None:
        if "valid plan" in plan_error:
            return False, "Could not build a safe plan for this request. Try rephrasing it."
        return False, f"The model request failed: {plan_error[:200]}"
    reason = last_error(run_id)
    if reason:
        return False, f"Could not complete the request: {reason[:250]}"
    return False, "No output was produced. Try rephrasing your request."


# ------------------------------------------------------------------ request
def set_example(text):
    st.session_state.request_text = text


st.markdown("### Request")
cols = st.columns(len(EXAMPLES))
for col, (label, text) in zip(cols, EXAMPLES):
    col.button(label, on_click=set_example, args=(text,), use_container_width=True)

request_text = st.text_area("What should the assistant do?", key="request_text", height=110,
                            placeholder="e.g. Summarize https://en.wikipedia.org/wiki/Git in 3 sentences.")

used = st.session_state.get("plans_used", 0)
limit_hit = bool(MAX_PLANS) and used >= MAX_PLANS
if limit_hit:
    st.warning("Request limit reached for this session. Reload the page to start over.")

if st.button("Run", type="primary", disabled=not request_text.strip() or limit_hit):
    st.session_state.plans_used = used + 1
    with st.spinner("Working on it..."):
        st.session_state.output = run_request(request_text)

# ------------------------------------------------------------------ output
st.markdown("### Output")
out = st.session_state.get("output")
with st.container(border=True):
    if out is None:
        st.caption("Your result will appear here.")
    elif out[0]:
        st.markdown(out[1])
    else:
        st.error(out[1])
