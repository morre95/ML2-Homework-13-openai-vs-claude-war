"""Provider-neutral chat client (stdlib only).

Model ids starting with "claude" go to the Anthropic Messages API, everything
else to the OpenAI Chat Completions API. Base URLs and keys come from the usual
env vars (ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY, OPENAI_BASE_URL / OPENAI_API_KEY),
which inside the experiment point at the metering proxy.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


class BudgetExhausted(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


def _post(url: str, headers: dict, body: dict, timeout: int = 600, retries: int = 6) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if "budget_exhausted" in text:
                raise BudgetExhausted(text) from None
            if e.code in (408, 409, 429) or e.code >= 500:
                time.sleep(min(60, 2 ** attempt))
                continue
            raise LLMError(f"HTTP {e.code}: {text[:2000]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(min(60, 2 ** attempt))
    raise LLMError(f"giving up on {url} after {retries} attempts")


def chat(model: str, system: str, messages: list[dict], max_tokens: int = 8192) -> dict:
    """messages: [{"role": "user"|"assistant", "content": str}]. Returns {"text", "usage"}."""
    if model.startswith("claude"):
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        resp = _post(
            f"{base}/v1/messages",
            {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"},
            {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens},
        )
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        u = resp.get("usage", {})
        usage = {"input": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                 + u.get("cache_creation_input_tokens", 0), "output": u.get("output_tokens", 0)}
    else:
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        resp = _post(
            f"{base}/chat/completions",
            {"authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
            {"model": model, "messages": [{"role": "system", "content": system}, *messages],
             "max_completion_tokens": max_tokens},
        )
        text = resp["choices"][0]["message"].get("content") or ""
        u = resp.get("usage", {})
        usage = {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0)}
    return {"text": text, "usage": usage}
