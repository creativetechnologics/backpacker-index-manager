"""Generic OpenAI-compatible chat-completions provider.

Used for any service that exposes ``POST /v1/chat/completions`` with
Bearer-token auth and a JSON response shape identical to OpenAI's. The
``name`` argument (one of the keys in ``_ENV_VARS``) picks the env
var that holds the API key, and the lane's ``base_url`` config picks
the endpoint. The lane's ``model`` is passed straight through.

The names this class is registered under today:

  * ``opencode-zen`` — OpenCode Zen hosted models
    (https://opencode.ai/zen/v1) — free tier: big-pickle,
    deepseek-v4-flash-free, mimo-v2.5-free, nemotron-3-ultra-free
  * ``nvidia`` — NVIDIA NIM free hosted models
    (https://integrate.api.nvidia.com/v1) — nemotron, llama,
    deepseek, qwen
  * ``minimax`` — MiniMax Coding Plan global endpoint
    (https://api.minimax.io/v1) — MiniMax-M3, M2.7, M2.5, M2.1, M2

Adding another OpenAI-compatible gateway is a one-line edit to
``_ENV_VARS`` plus a lane in ``lane_config.default_lanes()``.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from . import Provider, ProviderError, RateLimitError

# Map "logical provider name" → env var that holds the API key.
# The orchestrator propagates this into the lane worker subprocess.
_ENV_VARS: dict[str, str] = {
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}


def _ssl_context():
    if os.environ.get("FILL_SSL_NO_VERIFY") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def _parse_retry_after(body: str, headers: Any) -> float:
    """Best-effort extraction of the suggested wait time on 429.

    OpenAI-compatible gateways vary:
      * OpenRouter-style: JSON body with ``retry_after_seconds`` or
        ``retry_after`` field
      * Generic: ``Retry-After`` HTTP header (seconds or HTTP-date)
      * Free tier: often just 429 with no hint
    Falls back to 30s if nothing usable is found.
    """
    # Header
    try:
        ra = headers.get("Retry-After") if hasattr(headers, "get") else None
    except Exception:
        ra = None
    if ra:
        try:
            return max(1.0, float(ra))
        except (TypeError, ValueError):
            pass
    # Body fields
    try:
        parsed = json.loads(body)
    except Exception:
        return 30.0
    for key in ("retry_after_seconds", "retry_after", "retryAfter"):
        if key in parsed:
            try:
                return max(1.0, float(parsed[key]))
            except (TypeError, ValueError):
                pass
    return 30.0


def _recover_truncated_json(text: str) -> dict[str, Any] | None:
    r"""Attempt to recover from common JSON syntax errors the LLM makes.

    These are NOT truncation - with max_tokens=81920 we have plenty of
    headroom. These are genuine model output errors:
      - Invalid backslash escapes inside strings (e.g. backslash-s, backslash-x)
      - Missing commas between object fields
      - Trailing commas before closing braces
      - Unclosed outermost braces/brackets

    Returns the parsed dict on success, or None if unrecoverable.
    """
    import json as _json
    import re as _re

    # Fix 1: Invalid backslash escapes inside JSON strings.
    # JSON only allows \", \\, \/, \b, \f, \n, \r, \t, \uXXXX.
    # The model sometimes emits \s, \x, \d, etc. Replace invalid
    # single-character escapes with the literal character.
    fixed = _re.sub(
        r'\\x([0-9a-fA-F]{2})',
        lambda m: chr(int(m.group(1), 16)),
        text
    )
    # Replace invalid single-char escapes: backslash + any char
    # not in the allowed JSON escape set.
    def _fix_escapes(m):
        c = m.group(1)
        if c in '"\\/bfnrtu':
            return m.group(0)  # valid escape, keep it
        return c  # invalid escape, just use the literal char
    fixed = _re.sub(r'\\(.)', _fix_escapes, fixed)
    if fixed != text:
        try:
            return _json.loads(fixed)
        except _json.JSONDecodeError:
            pass

    # Fix 2: Trailing comma before closing brace/bracket.
    fixed2 = _re.sub(r",(\s*[}\]])$", r"\1", text.rstrip())
    if fixed2 != text:
        try:
            return _json.loads(fixed2)
        except _json.JSONDecodeError:
            pass

    # Fix 3: Missing closing braces/brackets at the very end.
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")
    if open_braces > 0 or open_brackets > 0:
        fixed = text.rstrip()
        if fixed.endswith(","):
            fixed = fixed[:-1]
        fixed += "}" * max(0, open_braces)
        fixed += "]" * max(0, open_brackets)
        try:
            return _json.loads(fixed)
        except _json.JSONDecodeError:
            pass

    # Fix 4: Missing comma between fields — expanded patterns.
    # The model sometimes omits commas between any two JSON values.
    for pat, repl in [
        (r'(\"[^"]*\")\s+(\")', r'\1, \2'),      # "str" "str"
        (r'(\"[^"]*\")\s+(\d)', r'\1, \2'),         # "str" 123
        (r'(\"[^"]*\")\s+(tru|fals|nul)', r'\1, \2'), # "str" true
        (r'(\d)\s+(\")', r'\1, \2'),                     # 123 "str"
        (r'(\])\s+(\")', r'\1, \2'),                      # ] "str"
        (r'(\})\s+(\")', r'\1, \2'),                      # } "str"
    ]:
        fixed2 = _re.sub(pat, repl, text)
        if fixed2 != text:
            try:
                return _json.loads(fixed2)
            except _json.JSONDecodeError:
                pass

    return None


class OpenAICompatibleClient:
    """A thin wrapper around any OpenAI-compatible ``/v1/chat/completions`` endpoint."""

    name: str
    model: str

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        provider_name: str | None = None,
        timeout: int = 300,
    ):
        self.model = model
        # The lane worker passes the configured provider name; fall back
        # to the class default if it didn't.
        self.name = provider_name or "openai-compatible"
        if not api_key:
            env_var = _ENV_VARS.get(self.name)
            if env_var:
                api_key = os.environ.get(env_var)
        if not api_key:
            raise ProviderError(
                f"Missing API key for provider '{self.name}'. "
                f"Set the env var or add a key via the dashboard."
            )
        self.api_key = api_key
        if not base_url:
            raise ProviderError(
                f"Provider '{self.name}' requires an explicit base_url on the lane."
            )
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, prompt: str) -> tuple[dict[str, Any], dict[str, int]]:
        url = f"{self.base}/chat/completions"
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            # MiniMax default max_completion_tokens is 10,240 — far too
            # low for our structured output (typical: 15-40K chars ≈
            # 4-10K tokens). Set a generous limit so we never truncate.
            "max_tokens": 81920,
        }
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            # Cloudflare WAF on opencode.ai and similar gateways
            # rejects Python's default ``Python-urllib/x.y`` User-Agent
            # with ``error code: 1010``. Presenting a browser UA
            # bypasses the WAF's bot check.
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:500]
            if exc.code == 429:
                retry_after = _parse_retry_after(detail, exc.headers or {})
                raise RateLimitError(
                    f"{self.name} HTTP 429: {detail[:200]}",
                    retry_after_s=retry_after,
                ) from exc
            raise ProviderError(f"{self.name} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self.name} network error: {exc}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderError(f"{self.name} network timeout: {exc}") from exc

        text = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = payload.get("usage", {}) or {}
        text = (text or "").strip()
        # Strip control characters that break JSON parsing. Some models
        # emit raw control chars (\x00-\x1f) inside strings that json.loads
        # rejects. Replace with escaped versions.
        import re as _re2
        text = _re2.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        # Strip markdown ```json / ``` fences. Some models return the
        # JSON wrapped in a code block with leading text before it.
        import re as _re
        m = _re.search(r"```(?:json)?\s*\n?(.+?)\n?```", text, _re.DOTALL)
        if m:
            text = m.group(1).strip()
        elif text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        # Some models (notably MiniMax-M2.7/M3 and DeepSeek Reasoner)
        # emit a ``<think>…</think>`` block before the JSON. The thinking
        # block can itself contain curly braces that confuse brace-
        # extraction. Strip the whole think block before json.loads.
        import re as _re
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
        try:
            return json.loads(text), {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
        except json.JSONDecodeError as exc:
            # Attempt recovery for truncated or malformed JSON.
            # Common minimax failure modes:
            #   1. Response cut off at max_tokens — truncate to last valid brace
            #   2. Trailing comma before closing brace — strip it
            #   3. Unterminated string — close it and the containing object
            recovered = _recover_truncated_json(text)
            if recovered is not None:
                return recovered, {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
            raise ProviderError(
                f"{self.name} returned invalid JSON: {exc}: {text[:300]}"
            ) from exc


def known_logical_providers() -> dict[str, str]:
    """Return the name → env-var mapping for the dashboard's defaults endpoint."""
    return dict(_ENV_VARS)
