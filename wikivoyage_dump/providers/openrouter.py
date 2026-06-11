"""OpenRouter provider.

OpenRouter exposes an OpenAI-compatible ``/api/v1/chat/completions`` endpoint.
For the ``openrouter/free`` model the user does not need to set a key (or
can set a placeholder), and the router picks a free upstream automatically.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

from . import Provider, ProviderError, RateLimitError

BASE_URL = "https://openrouter.ai/api/v1"


def _ssl_context():
    """Return a urllib SSL context. Honours FILL_SSL_NO_VERIFY=1 to skip
    cert validation (for corporate proxies that intercept the chain)."""
    if os.environ.get("FILL_SSL_NO_VERIFY") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def _parse_retry_after(body: str, default: float) -> float:
    """Try to pull ``retry_after_seconds`` out of an OpenRouter error body."""
    if not body:
        return default
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return default
    err = data.get("error", {}) if isinstance(data, dict) else {}
    if not isinstance(err, dict):
        return default
    meta = err.get("metadata", {}) if isinstance(err, dict) else {}
    if not isinstance(meta, dict):
        return default
    val = meta.get("retry_after_seconds") or meta.get("retry_after")
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class OpenRouterClient:
    name = "openrouter"
    model: str

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, timeout: int = 300):
        if api_key is None:
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.base = (base_url or os.environ.get("OPENROUTER_BASE_URL") or BASE_URL).rstrip("/")

    def call(self, prompt: str) -> tuple[dict[str, Any], dict[str, int]]:
        url = f"{self.base}/chat/completions"
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "Bearer free",
            "HTTP-Referer": "https://backpacker-index.local",
            "X-Title": "Backpacker Index Initial Fill",
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:1000]
            if exc.code == 429:
                # OpenRouter embeds retry_after_seconds in the error
                # body when the upstream provider sends it. Parse if
                # possible; otherwise default to 30s.
                retry_after = _parse_retry_after(detail, default=30.0)
                raise RateLimitError(
                    f"OpenRouter rate limited (HTTP 429). "
                    f"Retry after {retry_after:.0f}s. Detail: {detail[:200]}",
                    retry_after_s=retry_after,
                ) from exc
            raise ProviderError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"OpenRouter network error: {exc}") from exc

        text = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = payload.get("usage", {}) or {}
        text = (text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            return json.loads(text), {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
        except json.JSONDecodeError as exc:
            raise ProviderError(f"OpenRouter returned invalid JSON: {exc}: {text[:300]}") from exc
