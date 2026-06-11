"""DeepSeek direct (non-OpenAI-compatible) provider.

Thin wrapper around the existing ``DeepSeekDirectClient`` in
``deepseek_importer`` so the new lane system can import the same client.
"""
from __future__ import annotations

import os
import sys
from typing import Any

# Reuse the existing implementation in the parent module.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from deepseek_importer import DeepSeekDirectClient as _LegacyDeepSeekClient  # noqa: E402

from . import Provider, ProviderError  # noqa: E402


class DeepSeekDirectClient:  # implements Provider
    """Wrap ``DeepSeekDirectClient`` so it conforms to the ``Provider`` protocol.

    Renamed-from-original; the legacy class in ``deepseek_importer`` is
    aliased to ``_LegacyDeepSeekClient`` in this module.
    """

    name = "deepseek-direct"
    model: str

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, config: dict[str, Any] | None = None):
        if model:
            os.environ["DEEPSEEK_MODEL"] = model
        if api_key:
            os.environ["DEEPSEEK_API_KEY"] = api_key
        cfg = dict(config or {})
        if base_url:
            cfg["deepseek_api_url"] = base_url
        self._config = cfg
        self._client: _LegacyDeepSeekClient | None = None
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        # Resolved at init so the dashboard can display it.
        try:
            from deepseek_importer import DEFAULT_DEEPSEEK_URL
            self.base: str = cfg.get("deepseek_api_url") or DEFAULT_DEEPSEEK_URL
        except Exception:
            self.base = base_url or ""

    def _ensure(self) -> _LegacyDeepSeekClient:
        if self._client is None:
            self._client = _LegacyDeepSeekClient(self._config)
            self.model = self._client.model
        return self._client

    def call(self, prompt: str) -> tuple[dict[str, Any], dict[str, int]]:
        try:
            return self._ensure().extract(prompt)
        except Exception as exc:  # underlying client raises ImporterError
            raise ProviderError(str(exc)) from exc
