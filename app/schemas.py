from typing import Any, Literal
from pydantic import BaseModel, Field

class Fallback(BaseModel):
    action: str
    args: dict[str, Any] = {}

class OnFailure(BaseModel):
    strategy: Literal["abort", "skip", "retry", "fallback"] = "abort"
    max_retries: int = Field(0, ge=0, le=3)
    fallback: Fallback | None = None      # used if strategy == "fallback"

class Step(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    description: str = ""
    action: str
    args: dict[str, Any] = {}             # may contain {{ templates }}
    when: str | None = None               # conditional expression
    on_failure: OnFailure = OnFailure()

class Plan(BaseModel):
    goal: str
    inputs: dict[str, Any] = {}           # e.g. {"url": "https://..."}
    steps: list[Step] = Field(min_length=1, max_length=15)