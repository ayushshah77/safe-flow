import pytest
from pydantic import BaseModel

import app.actions.core  # noqa: F401
from app.executor import execute
from app.logstore import get_run
from app.registry import REGISTRY, ActionSpec
from app.schemas import Plan
from app.validator import PlanError, validate_plan


class In(BaseModel):
    x: int = 0

class Out(BaseModel):
    y: int = 0

class StrictOut(BaseModel):
    must_exist: int          # required field the fake action will not return

state = {"n": 0}

def _ok(a):
    return Out(y=2)

def _fail(a):
    raise RuntimeError("boom")

def _flaky(a):
    state["n"] += 1
    if state["n"] < 3:
        raise RuntimeError("temporary")
    return Out(y=1)


@pytest.fixture(autouse=True)
def fake_actions(monkeypatch):
    state["n"] = 0
    monkeypatch.setattr("app.executor.time.sleep", lambda s: None)
    specs = {
        "t_ok": (Out, _ok),
        "t_fail": (Out, _fail),
        "t_flaky": (Out, _flaky),
        "t_bad_output": (StrictOut, _ok),
    }
    for name, (out_model, fn) in specs.items():
        REGISTRY[name] = ActionSpec(name, "test", In, out_model, fn, "read")
    yield
    for name in specs:
        REGISTRY.pop(name, None)


def plan(*steps):
    return Plan.model_validate({"goal": "t", "steps": list(steps)})

def events(run_id):
    return [row[2] for row in get_run(run_id)]


def test_unknown_action_rejected():
    with pytest.raises(PlanError):
        validate_plan(plan({"id": "a", "action": "run_shell"}))

def test_forward_reference_rejected():
    with pytest.raises(PlanError):
        validate_plan(plan({"id": "a", "action": "t_ok",
                            "args": {"x": "{{ steps.b.output.y }}"}}))

def test_code_injection_in_condition_rejected():
    with pytest.raises(PlanError):
        validate_plan(plan({"id": "a", "action": "t_ok",
                            "when": "__import__('os').system('ls')"}))

def test_condition_skips_step():
    p = plan({"id": "a", "action": "t_ok"},
             {"id": "b", "action": "t_ok", "when": "steps['a']['output']['y'] > 5"})
    ev = events(execute(p))
    assert "skipped" in ev and ev[-1] == "run_finished"

def test_retry_then_success():
    p = plan({"id": "a", "action": "t_flaky",
              "on_failure": {"strategy": "retry", "max_retries": 2}})
    ev = events(execute(p))
    assert ev.count("attempt_failed") == 2 and "attempt_ok" in ev

def test_fallback_used():
    p = plan({"id": "a", "action": "t_fail",
              "on_failure": {"strategy": "fallback",
                             "fallback": {"action": "t_ok", "args": {}}}})
    assert "fallback_ok" in events(execute(p))

def test_skip_continues_run():
    p = plan({"id": "a", "action": "t_fail", "on_failure": {"strategy": "skip"}},
             {"id": "b", "action": "t_ok"})
    ev = events(execute(p))
    assert "skipped_after_failure" in ev and ev.count("attempt_ok") == 1

def test_abort_stops_run():
    p = plan({"id": "a", "action": "t_fail"}, {"id": "b", "action": "t_ok"})
    ev = events(execute(p))
    assert "run_aborted" in ev and "attempt_ok" not in ev

def test_output_validation_failure_is_a_failed_step():
    ev = events(execute(plan({"id": "a", "action": "t_bad_output"})))
    assert "attempt_failed" in ev and "run_aborted" in ev
