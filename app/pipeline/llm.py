"""Provider-agnostic structured-output LLM client (spec 2).

Tries providers/models in order, enforces structured output via json_schema
mode (falling back to json_object), and validates every response against a
Pydantic model — feeding validation errors back to the model on retry.

Hard limits prevent runaway waits: a request timeout per call (60s), a cap on
total attempts, and a wall-clock deadline.

Providers (OpenAI-compatible chat completions):
  - DeepSeek   (https://api.deepseek.com/v1)              -- DEEPSEEK_API_KEY
  - NVIDIA NIM (https://integrate.api.nvidia.com/v1)      -- NIM_API_KEY
  - OpenRouter (https://openrouter.ai/api/v1)             -- OPENROUTER_API_KEY
"""

import json
import os
import time
from typing import Any, Callable

from dotenv import load_dotenv
from openai import APITimeoutError, OpenAI
from pydantic import BaseModel, ValidationError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))

REQUEST_TIMEOUT = 600
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_DEADLINE = 1800  # seconds

NIM_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b",
    "meta/llama-3.3-70b-instruct",
    "openai/gpt-oss-120b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "openai/gpt-oss-20b",
]
OPENROUTER_MODELS = [
    "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]

_PROVIDER_DEFS = [
    {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat"],
        "prefer_schema": False,
    },
    {
        "name": "nim",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_env": "NIM_API_KEY",
        "models": NIM_MODELS,
        "prefer_schema": True,
    },
    {
        "name": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "models": OPENROUTER_MODELS,
        "prefer_schema": True,
    },
]


def _providers() -> list[dict]:
    providers = []
    for p in _PROVIDER_DEFS:
        if not os.environ.get(p["key_env"]):
            continue
        if p["name"] == "nim" and os.environ.get("NIM_MODELS"):
            p["models"] = [m.strip() for m in os.environ["NIM_MODELS"].split(",") if m.strip()]
        providers.append(p)
    return providers


class StructuredOutputError(RuntimeError):
    """Raised when every provider/model/retry attempt fails."""


def generate_structured(
    model_cls: type[BaseModel],
    system_prompt: str,
    user_prompt: str,
    *,
    max_retries: int = 2,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    deadline: float = DEFAULT_DEADLINE,
    temperature: float = 0.2,
    validate: Callable[[BaseModel], str | None] | None = None,
) -> BaseModel:
    """Ask an LLM for JSON matching `model_cls`, with validation + retry.

    Retry strategy: on ValidationError, the collected error messages are fed
    back into the prompt and the same model retries (up to `max_retries`),
    then the next model, then the next provider. Total attempts are capped by
    `max_attempts` and the whole search stops at `deadline` seconds.

    `validate` is an optional stage-level check: it receives the validated
    model and returns an error message (triggering retry) or None.
    """
    providers = _providers()
    if not providers:
        raise StructuredOutputError(
            "No LLM provider configured: set DEEPSEEK_API_KEY, NIM_API_KEY "
            "and/or OPENROUTER_API_KEY in .env"
        )

    schema = model_cls.model_json_schema()
    last_error = ""
    attempts = 0
    start = time.monotonic()

    for provider in providers:
        name = provider["name"]
        client = OpenAI(
            base_url=provider["base_url"],
            api_key=os.environ[provider["key_env"]],
            timeout=REQUEST_TIMEOUT,
            max_retries=0,
        )
        for model in provider["models"]:
            for retry in range(max_retries + 1):
                attempts += 1
                if attempts > max_attempts:
                    raise StructuredOutputError(
                        f"hit attempt cap ({max_attempts}); last error: {last_error[:400]}"
                    )
                if time.monotonic() - start > deadline:
                    raise StructuredOutputError(
                        f"hit deadline ({deadline}s); last error: {last_error[:400]}"
                    )
                elapsed = int(time.monotonic() - start)
                print(
                    f"[llm] {name}/{model} attempt {retry + 1}... "
                    f"(elapsed {elapsed}s, remaining {int(deadline - elapsed)}s)",
                    flush=True,
                )
                feedback = (
                    f"\n\nYour previous response failed validation. Errors:\n{last_error}\n"
                    "Respond again with corrected JSON only."
                    if last_error
                    else ""
                )

                def make_call(rf: dict | None) -> str:
                    return client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt + feedback},
                        ],
                        temperature=temperature,
                        response_format=rf,
                    ).choices[0].message.content or ""

                formats: list[dict | None]
                if provider["prefer_schema"]:
                    formats = [
                        {
                            "type": "json_schema",
                            "json_schema": {
                                "name": model_cls.__name__,
                                "strict": True,
                                "schema": schema,
                            },
                        },
                        {"type": "json_object"},
                    ]
                else:
                    formats = [{"type": "json_object"}]

                content = ""
                last_exc: Exception | None = None
                for rf in formats:
                    try:
                        content = make_call(rf)
                        break
                    except Exception as exc:  # provider/model-level failure
                        last_exc = exc
                        print(
                            f"[llm] {name}/{model} ({rf or 'none'}) failed: {exc}",
                            flush=True,
                        )
                        if isinstance(exc, (APITimeoutError, TimeoutError)):
                            break  # a timed-out generation will also time out again: skip
                if not content:
                    last_error = f"{name}/{model} request failed: {last_exc}"
                    break  # try next model/provider

                if not content.strip():
                    last_error = "empty response"
                    continue

                try:
                    data: Any = json.loads(content)
                except json.JSONDecodeError as exc:
                    last_error = f"invalid JSON: {exc}"
                    continue

                try:
                    validated = model_cls.model_validate(data)
                except ValidationError as exc:
                    last_error = _validation_feedback(exc)
                    print(
                        f"[llm] validation failed ({len(exc.errors())} error(s)); retrying",
                        flush=True,
                    )
                    continue

                if validate is not None:
                    stage_error = validate(validated)
                    if stage_error:
                        last_error = stage_error
                        print(f"[llm] stage check failed: {stage_error}", flush=True)
                        continue

                return validated

    raise StructuredOutputError(
        f"All {attempts} attempts failed to produce valid {model_cls.__name__}. "
        f"Last error: {last_error[:500]}"
    )


def _validation_feedback(exc: ValidationError, limit: int = 5) -> str:
    lines = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"- {loc or '<root>'}: {err['msg']}")
    total = len(exc.errors())
    if total > limit:
        lines.append(f"- ... and {total - limit} more errors")
    return "\n".join(lines)
