import re, ast
from app.registry import REGISTRY

class PlanError(Exception):
    pass

REF = re.compile(r"steps\.([a-z][a-z0-9_]*)|steps\[\\?['\"]([a-z][a-z0-9_]*)\\?['\"]\]")

ALLOWED_AST = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name,
    ast.Load, ast.Constant, ast.Subscript, ast.And, ast.Or, ast.Not, ast.Eq,
    ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Add, ast.Sub, ast.Mult,
)

def validate_plan(plan):
    errors, seen = [], set()
    for s in plan.steps:
        if s.id in seen:
            errors.append(f"duplicate id {s.id}")
        spec = REGISTRY.get(s.action)
        if not spec:
            errors.append(f"{s.id}: unknown action '{s.action}'")
            seen.add(s.id)
            continue

        unknown = set(s.args) - set(spec.input_model.model_fields)
        if unknown:
            errors.append(f"{s.id}: unknown args {unknown}")

        blob = repr(s.args) + (s.when or "")
        for m in REF.finditer(blob):
            ref = m.group(1) or m.group(2)
            if ref not in seen:
                errors.append(f"{s.id}: references '{ref}' which is not an earlier step")

        if s.when:
            try:
                tree = ast.parse(s.when, mode="eval")
                bad = sorted({type(n).__name__ for n in ast.walk(tree)
                              if not isinstance(n, ALLOWED_AST)})
                if bad:
                    errors.append(f"{s.id}: disallowed syntax in 'when': {bad}")
            except SyntaxError as e:
                errors.append(f"{s.id}: bad condition ({e})")

        fb = s.on_failure
        if fb.strategy == "fallback":
            if not fb.fallback or fb.fallback.action not in REGISTRY:
                errors.append(f"{s.id}: fallback missing or unknown action")
        if fb.strategy == "retry" and fb.max_retries < 1:
            errors.append(f"{s.id}: retry needs max_retries >= 1")
        seen.add(s.id)

    if errors:
        raise PlanError("\n".join(errors))

def risky_steps(plan):
    return [s.id for s in plan.steps
            if s.action in REGISTRY and REGISTRY[s.action].risk in ("write", "network")]
