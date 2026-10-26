import time, uuid
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from simpleeval import SimpleEval
from app.registry import REGISTRY
from app.logstore import log_event

jenv = SandboxedEnvironment(undefined=StrictUndefined)

def render(value, ctx):
    if isinstance(value, str):
        return jenv.from_string(value).render(**ctx)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value

def eval_condition(expr, ctx) -> bool:
    return bool(SimpleEval(names=ctx).eval(expr))

def run_action(name, raw_args, ctx):
    spec = REGISTRY[name]
    args = spec.input_model.model_validate(render(raw_args, ctx))
    out = spec.fn(args)
    return spec.output_model.model_validate(out.model_dump())

def execute(plan, approved: bool = False, dry_run: bool = False) -> str:
    run_id = uuid.uuid4().hex[:8]
    ctx = {"inputs": plan.inputs, "steps": {}}
    log_event(run_id, None, "run_started", {"goal": plan.goal, "dry_run": dry_run})

    for step in plan.steps:
        if step.when:
            try:
                ok = eval_condition(step.when, ctx)
            except Exception as e:
                log_event(run_id, step.id, "condition_error", {"error": str(e)})
                ok = False
            if not ok:
                ctx["steps"][step.id] = {"status": "skipped", "output": {}}
                log_event(run_id, step.id, "skipped", {"when": step.when})
                continue

        spec = REGISTRY[step.action]
        if dry_run and spec.risk in ("write", "network"):
            ctx["steps"][step.id] = {"status": "dry_run", "output": {}}
            log_event(run_id, step.id, "dry_run", {"action": step.action})
            continue
        if spec.risk == "write" and not approved:
            log_event(run_id, step.id, "blocked_needs_approval", {})
            log_event(run_id, None, "run_finished", {"status": "blocked"})
            return run_id

        policy = step.on_failure
        attempts = 1 + (policy.max_retries if policy.strategy == "retry" else 0)
        result, last_err = None, None
        for n in range(1, attempts + 1):
            t0 = time.time()
            try:
                log_event(run_id, step.id, "attempt_start", {"attempt": n, "args": step.args})
                result = run_action(step.action, step.args, ctx).copy() if False else \
                         run_action(step.action, step.args, ctx).model_dump()
                log_event(run_id, step.id, "attempt_ok",
                          {"attempt": n, "ms": int((time.time() - t0) * 1000),
                           "output_preview": str(result)[:300]})
                break
            except Exception as e:
                last_err = e
                log_event(run_id, step.id, "attempt_failed",
                          {"attempt": n, "error": f"{type(e).__name__}: {e}"[:500]})
                if n < attempts:
                    time.sleep(min(2 ** n, 8))

        if result is None:
            if policy.strategy == "fallback" and policy.fallback:
                try:
                    result = run_action(policy.fallback.action,
                                        policy.fallback.args, ctx).model_dump()
                    log_event(run_id, step.id, "fallback_ok",
                              {"action": policy.fallback.action})
                except Exception as e:
                    log_event(run_id, step.id, "fallback_failed", {"error": str(e)[:300]})
            if result is None and policy.strategy == "skip":
                ctx["steps"][step.id] = {"status": "skipped_after_failure", "output": {}}
                log_event(run_id, step.id, "skipped_after_failure", {})
                continue
            if result is None:
                log_event(run_id, step.id, "run_aborted", {"error": str(last_err)[:300]})
                log_event(run_id, None, "run_finished", {"status": "failed"})
                return run_id

        ctx["steps"][step.id] = {"status": "ok", "output": result}

    log_event(run_id, None, "run_finished", {"status": "success"})
    return run_id
