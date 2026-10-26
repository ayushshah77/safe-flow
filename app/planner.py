import json
from pydantic import ValidationError
from app.llm import chat
from app.schemas import Plan
from app.registry import catalog_for_prompt
from app.validator import validate_plan, PlanError

SYSTEM = """You convert user requests into a JSON workflow plan.
Respond with ONLY a single JSON object that matches this JSON Schema:
{schema}

Use ONLY these actions:
{catalog}

Rules:
- Step ids are unique snake_case. Later steps may reference earlier output with
  Jinja: {{{{ steps.fetch.output.text }}}} or workflow inputs: {{{{ inputs.url }}}}.
- Use "when" for conditions, e.g. "steps['count']['output']['words'] > 500".
- Put every external value (URL, filename) in "inputs" and reference it.
- For fragile steps (network), set on_failure to retry (max 2) or fallback.
- If the request needs something not in the action list, return a plan with a
  single step using action "notify" explaining what is unsupported.
"""

def make_plan(request: str, attempts: int = 3) -> Plan:
    system = SYSTEM.format(schema=json.dumps(Plan.model_json_schema()),
                           catalog=catalog_for_prompt())
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": request}]
    last_error = None
    for _ in range(attempts):
        raw = chat(msgs, json_mode=True)
        try:
            plan = Plan.model_validate_json(raw)
            validate_plan(plan)
            return plan
        except (ValidationError, PlanError) as e:
            last_error = e
            msgs += [{"role": "assistant", "content": raw},
                     {"role": "user",
                      "content": f"Invalid plan. Fix these errors and return the full JSON object:\n{e}"}]
    raise RuntimeError(f"Planner could not produce a valid plan: {last_error}")
