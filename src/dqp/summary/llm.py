"""Minimal LLM clients: Anthropic and any OpenAI-compatible API.

Standard library only (urllib), so the pipeline has no extra dependency and the
provider is swappable by config. API keys come from environment variables
(GitHub Actions secrets in CI), never from the config file or the repo.

What is sent: the fact pack (aggregated numbers, check names, cleaning
reasons). Raw data rows are never sent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
DEFAULT_MODEL = {"anthropic": "claude-sonnet-5-5"}


class LLMError(RuntimeError):
    pass


class LLMClient:
    provider = "base"

    def __init__(self, model: str):
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 2000) -> str:  # pragma: no cover
        raise NotImplementedError


def _post(url: str, headers: dict, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise LLMError(f"HTTP {exc.code} from {url}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMError(f"could not reach {url}: {exc}") from None


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        super().__init__(model)
        self.api_key, self.url = api_key, (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"

    def complete(self, system: str, user: str, max_tokens: int = 2000) -> str:
        data = _post(self.url, {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                     {"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                      "system": system, "messages": [{"role": "user", "content": user}]})
        try:
            return "".join(b["text"] for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError):
            raise LLMError(f"unexpected response: {str(data)[:300]}") from None


class OpenAICompatibleClient(LLMClient):
    provider = "openai"

    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        super().__init__(model)
        self.api_key, self.url = api_key, (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

    def complete(self, system: str, user: str, max_tokens: int = 2000) -> str:
        data = _post(self.url, {"authorization": f"Bearer {self.api_key}"},
                     {"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected response: {str(data)[:300]}") from None


def make_client(cfg) -> tuple[LLMClient | None, str]:
    """Return (client, why). client is None when the template should be used."""
    if cfg.provider == "none":
        return None, "summary.provider is 'none'"
    candidates = [cfg.provider] if cfg.provider != "auto" else ["anthropic", "openai"]
    for provider in candidates:
        env = cfg.api_key_env or DEFAULT_KEY_ENV[provider]
        key = os.environ.get(env)
        if not key:
            continue
        model = cfg.model or DEFAULT_MODEL.get(provider)
        if not model:
            return None, f"summary.model must be set for provider {provider!r}"
        cls = AnthropicClient if provider == "anthropic" else OpenAICompatibleClient
        return cls(key, model, cfg.base_url), f"{provider} ({model}), key from ${env}"
    envs = ", ".join("$" + (cfg.api_key_env or DEFAULT_KEY_ENV[p]) for p in candidates)
    return None, f"no API key found ({envs} not set)"
