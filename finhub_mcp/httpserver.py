# Copyright (c) 2026 bachbnt. All rights reserved.
"""Network transport support: bearer-token auth and safe startup rules for the HTTP/SSE modes."""

import hmac
import ipaddress
import json
import secrets

MIN_TOKEN_LENGTH = 16
HEALTH_PATH = '/healthz'
LOOPBACK_NAMES = {'localhost'}


def is_loopback(host: str) -> bool:
    """True when binding to this host keeps the server reachable only from this machine."""
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def validate_startup(host: str, token: str | None, no_auth: bool) -> None:
    """Refuse unsafe network configurations. Raises ValueError with an actionable message.

    The server exposes tools that write data (alerts), so a network listener must be authenticated.
    Auth may only be disabled explicitly, and only on a loopback address.
    """
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"FINHUB_HTTP_TOKEN must be at least {MIN_TOKEN_LENGTH} characters. "
            "Generate one with: python server.py --generate-token"
        )
    if token:
        return
    if no_auth and is_loopback(host):
        return
    if no_auth:
        raise ValueError(f"--no-auth is only allowed on a loopback address, not {host!r}")
    raise ValueError(
        "HTTP transports require authentication. Set FINHUB_HTTP_TOKEN "
        "(generate one with: python server.py --generate-token), or pass --no-auth on a loopback host."
    )


class BearerTokenMiddleware:
    """Pure-ASGI middleware: every HTTP request needs `Authorization: Bearer <token>`.

    `/healthz` is exempt and returns a static body, so container health checks need no secret.
    Tokens are compared in constant time.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':  # lifespan and anything else pass straight through
            await self.app(scope, receive, send)
            return
        if scope['path'] == HEALTH_PATH and scope['method'] == 'GET':
            await self._respond(send, 200, {'status': 'ok'})
            return
        headers = {key.lower(): value for key, value in scope.get('headers', [])}
        scheme, _, supplied = headers.get(b'authorization', b'').decode('latin-1').partition(' ')
        if scheme.lower() == 'bearer' and hmac.compare_digest(supplied.strip().encode(), self.token):
            await self.app(scope, receive, send)
            return
        await self._respond(send, 401, {'error': 'unauthorized'}, {b'www-authenticate': b'Bearer'})

    @staticmethod
    async def _respond(send, status: int, payload: dict, extra: dict[bytes, bytes] | None = None) -> None:
        body = json.dumps(payload).encode()
        headers = [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]
        headers += list((extra or {}).items())
        await send({'type': 'http.response.start', 'status': status, 'headers': headers})
        await send({'type': 'http.response.body', 'body': body})
