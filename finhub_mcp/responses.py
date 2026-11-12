# Copyright (c) 2026 bachbnt. All rights reserved.

import json
from typing import Any

from finhub_mcp.config import ConfigError
from finhub_mcp.exceptions import ProviderFallbackError


def _dumps(payload: dict[str, Any]) -> str:
    # Compact separators: these responses are read by an LLM, so whitespace is wasted tokens.
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


def success(data: Any, **meta: Any) -> str:
    """Return a successful MCP response as a JSON string.

    Metadata with a value of None is dropped. For list payloads put per-response facts
    (source, exchange, fallback_used) here instead of repeating them in every row.
    """
    payload = {'ok': True, 'data': data}
    meta = {key: value for key, value in meta.items() if value is not None}
    if meta:
        payload['meta'] = meta
    return _dumps(payload)


def error(message: Any, **meta: Any) -> str:
    """Return an error MCP response as a JSON string."""
    if isinstance(message, ProviderFallbackError):
        meta.setdefault('error_type', 'provider_failure')
        meta.setdefault('retryable', message.retryable)
        meta.setdefault('provider_errors', message.provider_errors)
    elif isinstance(message, ConfigError):
        meta.setdefault('error_type', 'config')
        meta.setdefault('retryable', False)
    elif isinstance(message, ValueError):
        meta.setdefault('error_type', 'validation')
        meta.setdefault('retryable', False)
    elif isinstance(message, BaseException):
        meta.setdefault('error_type', 'internal')

    payload = {'ok': False, 'error': str(message) or type(message).__name__}
    if meta:
        payload['meta'] = meta
    return _dumps(payload)


def invalid(message: str) -> str:
    """Return a validation error response."""
    return error(message, error_type='validation', retryable=False)
