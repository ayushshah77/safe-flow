from dataclasses import dataclass
from typing import Callable, Type
from pydantic import BaseModel

@dataclass
class ActionSpec:
    name: str
    description: str
    input_model: Type[BaseModel]
    output_model: Type[BaseModel]
    fn: Callable
    risk: str = "read"          # read | write | network

REGISTRY: dict[str, ActionSpec] = {}

def action(name, description, inp, out, risk="read"):
    def deco(fn):
        REGISTRY[name] = ActionSpec(name, description, inp, out, fn, risk)
        return fn
    return deco

def catalog_for_prompt() -> str:
    """What the planner LLM is allowed to see."""
    lines = []
    for a in REGISTRY.values():
        lines.append(f"- {a.name}: {a.description}\n  args schema: {a.input_model.model_json_schema()['properties']}\n"
                     f"  output fields: {list(a.output_model.model_fields)}")
    return "\n".join(lines)