# Copyright (c) 2026 bachbnt. All rights reserved.
"""Small shared HTTP client for the public JSON/RSS endpoints (news, gold, forex, sentiment)."""

from typing import Any

import requests

from finhub_mcp.config import get_number

MAX_BYTES = 5_000_000

_session = requests.Session()
_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (compatible; finhub-mcp)',
    'Accept': 'application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.8',
})


def get_bytes(url: str, params: dict[str, Any] | None = None) -> bytes:
    """GET a URL and return the body, enforcing a timeout and a size cap."""
    timeout = get_number('http_timeout_seconds', 1)
    with _session.get(url, params=params, timeout=timeout, stream=True) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise ValueError(f"Response from {url} is larger than {MAX_BYTES} bytes")
        return bytes(body)


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    import json
    return json.loads(get_bytes(url, params))
