"""Local provider.

Talks to a locally-hosted OpenAI-compatible chat completions endpoint.
The default base URL points at oMLX (``http://localhost:8000/v1``),
but any local server that accepts OpenAI-style ``/v1/chat/completions``
will work — point ``base_url`` at whatever you have running.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

from . import Provider, ProviderError

# oMLX's default; works for any OpenAI-compatible local server.
DEFAULT_BASE = "http://localhost:8000/v1"


def _ssl_context():
    if os.environ.get("FILL_SSL_NO_VERIFY") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


class LocalClient:
    name = "local"
    model: str

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, timeout: int = 600):
        # ``api_key`` is accepted for interface uniformity; most local
        # servers accept any non-empty value (oMLX uses "not-needed" by
        # convention) or no key at all.
        self.model = model
        self.api_key = api_key or os.environ.get("LOCAL_PROVIDER_API_KEY") or "not-needed"
        self.base = (base_url or os.environ.get("LOCAL_PROVIDER_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    def call(self, prompt: str) -> tuple[dict[str, Any], dict[str, int]]:
        url = f"{self.base}/chat/completions"
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:500]
            raise ProviderError(f"local HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"local network error: {exc}") from exc

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
            raise ProviderError(f"local returned invalid JSON: {exc}: {text[:300]}") from exc

    def health_check(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/models", timeout=5, context=_ssl_context()) as resp:
                return resp.status == 200
        except Exception:
            return False
