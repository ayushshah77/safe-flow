from fastapi import FastAPI, HTTPException
from app.schemas import Plan
from app.planner import make_plan
from app.validator import validate_plan, risky_steps, PlanError
from app.executor import execute
from app.logstore import get_run
import app.actions.core  # noqa: F401  (registers the actions)

api = FastAPI(title="Safe Workflow Assistant")

@api.get("/health")
def health():
    return {"ok": True}

@api.post("/plan")
def plan(req: dict):
    try:
        p = make_plan(req["request"])
    except Exception as e:
        raise HTTPException(502, f"Planner failed: {e}")
    return {"plan": p.model_dump(), "needs_approval": risky_steps(p)}

@api.post("/validate")
def validate(p: Plan):
    try:
        validate_plan(p)
        return {"ok": True}
    except PlanError as e:
        raise HTTPException(422, str(e))

@api.post("/run")
def run(p: Plan, approved: bool = False, dry_run: bool = False):
    try:
        validate_plan(p)
    except PlanError as e:
        raise HTTPException(422, str(e))
    return {"run_id": execute(p, approved, dry_run)}

@api.get("/runs/{run_id}")
def run_log(run_id: str):
    return get_run(run_id)
