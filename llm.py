"""llm.py — one function to call Claude, Gemini or OpenAI and get JSON back, cached on disk.

    from llm import call_llm
    out = call_llm("...", schema={...})            # -> dict

This module only talks to the models. Prompts and schemas live with the step that uses them:
    discover.py    restaurant extraction (Prompt B)
    adjudicate.py  region adjudicator (Prompt C)

Settings come from the [llm] section of config.toml, with secrets.toml merged on top (paths.get_config).

Providers: anthropic (anthropic_api_key), gemini (gemini_api_key), openai (openai_api_key); keys go in secrets.toml.
Order: providers = ["openai", "anthropic", "gemini"] sets the fallback order;
provider = "<name>" just moves one provider to the front. Providers without a key are skipped.
If a provider errors (out of credits, quota, rate limit), the call falls back to the next one that has a key.

Models: anthropic_model, gemini_model, openai_model. Check these against what your account offers.
OpenAI calls leave temperature at the model's default (newer OpenAI models reject other values), and get a larger
token allowance (openai_max_tokens) because reasoning tokens count against it.
Cache: data/llm_cache/<sha256>.json (paths.LLM_CACHE_DIR; moves with DATA_DIR). Same prompt, schema and model means
no new API call. Delete the folder to bust it.

pip install anthropic google-genai openai
"""
from __future__ import annotations
import json, re, time

import paths

CACHE_DIR = paths.LLM_CACHE_DIR          # module-level so tests (and callers) can point it elsewhere
CONFIG = paths.get_config("llm")         # same here
ANTHROPIC_MODEL = CONFIG["anthropic_model"]
GEMINI_MODEL = CONFIG["gemini_model"]
OPENAI_MODEL = CONFIG["openai_model"]
MAX_TOKENS = CONFIG["max_tokens"]
OPENAI_MAX_TOKENS = CONFIG["openai_max_tokens"]
RETRIES = 3


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- providers
def _api_key(provider: str) -> str | None:
    return CONFIG.get(f"{provider}_api_key") or None     # secrets-example.toml leaves unused keys as ""


def _model(provider: str) -> str:
    return {"anthropic": ANTHROPIC_MODEL, "gemini": GEMINI_MODEL, "openai": OPENAI_MODEL}[provider]


def _providers() -> list[str]:
    order = CONFIG["providers"]
    unknown = [p for p in order if p not in CALLERS]
    if unknown:
        raise LLMError(f"Unknown provider(s) in [llm] providers: {unknown}. Use {sorted(CALLERS)}.")
    forced = CONFIG.get("provider")
    if forced:
        order = [forced] + [p for p in order if p != forced]
    have = [p for p in order if _api_key(p)]
    if forced and forced not in have:                      # forced without a key: try it anyway, it will say why
        have = [forced] + have
    if not have:
        raise LLMError("No API key: set anthropic_api_key, gemini_api_key or openai_api_key in secrets.toml.")
    return have


def _call_anthropic(prompt: str, schema: dict | None, system: str | None, temperature: float):
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key("anthropic"))
    kw = dict(model=ANTHROPIC_MODEL, max_tokens=MAX_TOKENS, temperature=temperature,
              messages=[{"role": "user", "content": prompt}])
    if system:
        kw["system"] = system
    if schema:                                   # tool use forces output that matches the schema
        kw["tools"] = [{"name": "respond", "description": "Return the answer.", "input_schema": schema}]
        kw["tool_choice"] = {"type": "tool", "name": "respond"}
    msg = client.messages.create(**kw)
    if schema:
        for block in msg.content:
            if block.type == "tool_use":
                return block.input
        raise LLMError("Claude returned no tool call")
    return "".join(b.text for b in msg.content if b.type == "text")


def _call_gemini(prompt: str, schema: dict | None, system: str | None, temperature: float):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=_api_key("gemini"))
    cfg = dict(temperature=temperature, max_output_tokens=MAX_TOKENS)
    if system:
        cfg["system_instruction"] = system
    if schema:
        cfg["response_mime_type"] = "application/json"
        try:                                     # newer google-genai: takes plain JSON Schema
            config = types.GenerateContentConfig(**cfg, response_json_schema=schema)
        except (TypeError, ValueError):          # older versions: OpenAPI-subset schema
            config = types.GenerateContentConfig(**cfg, response_schema=_to_openapi(schema))
    else:
        config = types.GenerateContentConfig(**cfg)
    resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=config)
    text = resp.text or ""
    return parse_json(text) if schema else text


def _call_openai(prompt: str, schema: dict | None, system: str | None, temperature: float):
    from openai import OpenAI
    client = OpenAI(api_key=_api_key("openai"))
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    kw = dict(model=OPENAI_MODEL, messages=messages, max_completion_tokens=OPENAI_MAX_TOKENS)
    if schema:                                   # JSON-schema output; not strict, since our schemas have optional fields
        kw["response_format"] = {"type": "json_schema",
                                 "json_schema": {"name": "respond", "schema": schema, "strict": False}}
    resp = client.chat.completions.create(**kw)
    text = resp.choices[0].message.content or ""
    return parse_json(text) if schema else text


def _to_openapi(s):
    """Strip JSON-Schema keys the older Gemini schema format rejects."""
    if isinstance(s, dict):
        out = {k: _to_openapi(v) for k, v in s.items() if k not in ("additionalProperties", "$schema", "default")}
        if isinstance(out.get("type"), list):    # ["string","null"] -> string + nullable
            types_ = [t for t in out["type"] if t != "null"]
            out["type"] = types_[0] if types_ else "string"
            out["nullable"] = True
        return out
    if isinstance(s, list):
        return [_to_openapi(x) for x in s]
    return s


CALLERS = {"anthropic": _call_anthropic, "gemini": _call_gemini, "openai": _call_openai}


def parse_json(text: str):
    """Pull JSON out of model text, even when it's wrapped in ```json fences or surrounded by prose."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for open_, close in (("{", "}"), ("[", "]")):
            i, j = text.find(open_), text.rfind(close)
            if 0 <= i < j:
                try:
                    return json.loads(text[i:j + 1])
                except json.JSONDecodeError:
                    pass
    raise LLMError(f"Could not parse JSON from model output: {text[:200]!r}")


# ---------------------------------------------------------------- main entry
def call_llm(prompt: str, schema: dict | None = None, system: str | None = None,
             temperature: float = 0.0, use_cache: bool = True):
    """Returns a dict/list when `schema` is given, otherwise a string."""
    errors = []
    plan = []
    for provider in _providers():
        model = _model(provider)
        path = paths.cache_path(CACHE_DIR, provider, model, system, prompt, schema, temperature)
        cached = paths.cache_get(path) if use_cache else None
        if cached is not None:                           # any provider's cached answer wins: no API call
            return cached["output"]
        plan.append((provider, model, path))
    for provider, model, path in plan:
        for attempt in range(RETRIES):
            try:
                out = CALLERS[provider](prompt, schema, system, temperature)
                if use_cache:
                    paths.cache_put(path, {"provider": provider, "model": model, "output": out})
                return out
            except Exception as e:                       # noqa: BLE001 — rate limits, credits, bad JSON
                errors.append(f"{provider}#{attempt + 1}: {type(e).__name__}: {e}")
                msg = str(e).lower()
                if any(s in msg for s in ("credit", "billing", "quota", "insufficient", "api key", "permission")):
                    break                                # retrying won't help, so try the other provider
                time.sleep(2 ** attempt)
    raise LLMError("All providers failed:\n" + "\n".join(errors))


if __name__ == "__main__":
    import sys
    print(call_llm(" ".join(sys.argv[1:]) or "Say hi in five words."))
