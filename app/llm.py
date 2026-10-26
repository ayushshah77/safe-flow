import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_client = None

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("No API key set (check your .env file)")
        _client = OpenAI(
            api_key=key,
            base_url=os.environ.get("LLM_BASE_URL") or None,
            timeout=60,
            max_retries=2,
        )
    return _client

def model_name() -> str:
    return os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"

def chat(messages: list[dict], json_mode: bool = False, max_tokens: int = 1500) -> str:
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    r = _get_client().chat.completions.create(
        model=model_name(),
        messages=messages,
        temperature=0,
        max_tokens=max_tokens,
        **kwargs,
    )
    return r.choices[0].message.content or ""
