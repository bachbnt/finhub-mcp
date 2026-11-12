# Copyright (c) 2026 bachbnt. All rights reserved.
#
# MCP server configuration (add to ~/.claude/settings.json):
# {
#   "mcpServers": {
#     "finhub": {
#       "command": "/path/to/finhub-mcp/.venv/bin/python",
#       "args": ["/path/to/finhub-mcp/server.py"]
#     }
#   }
# }
#
# That runs the default stdio transport. To serve over HTTP instead, see the README
# ("HTTP transport"): python server.py --transport streamable-http

import argparse
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from finhub_mcp import prompts, resources
from finhub_mcp.httpserver import HEALTH_PATH, BearerTokenMiddleware, generate_token, validate_startup
from finhub_mcp.tools import alerts, crypto, crypto_extra, market, news, vn_market, vn_stock, world

TRANSPORTS = ('stdio', 'streamable-http', 'sse')


def create_mcp(host: str = '127.0.0.1', port: int = 8000, allowed_hosts: list[str] | None = None) -> FastMCP:
    """Create and register the FinHub MCP server (tools, resources and prompts)."""
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f'http://{h}' for h in allowed_hosts] + [f'https://{h}' for h in allowed_hosts],
        )
    mcp = FastMCP("finhub-mcp", host=host, port=port, transport_security=security)
    vn_stock.register(mcp)
    vn_market.register(mcp)
    crypto.register(mcp)
    crypto_extra.register(mcp)
    market.register(mcp)
    world.register(mcp)
    news.register(mcp)
    alerts.register(mcp)
    resources.register(mcp)
    prompts.register(mcp)

    @mcp.custom_route(HEALTH_PATH, methods=['GET'], include_in_schema=False)
    async def health(request):  # served unauthenticated by the middleware too; present for non-auth loopback use
        from starlette.responses import JSONResponse
        return JSONResponse({'status': 'ok'})

    return mcp


mcp = create_mcp()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FinHub MCP server")
    parser.add_argument('--transport', choices=TRANSPORTS, default=os.getenv('FINHUB_TRANSPORT', 'stdio'),
                        help="stdio (default, used by Claude Code), streamable-http, or sse")
    parser.add_argument('--host', default=os.getenv('FINHUB_HOST', '127.0.0.1'),
                        help="bind address for HTTP transports (default 127.0.0.1)")
    parser.add_argument('--port', type=int, default=int(os.getenv('FINHUB_PORT', '8000')))
    parser.add_argument('--allow-host', action='append', default=[],
                        help="extra Host header allowed (e.g. finhub.example.com:*); enables DNS-rebinding protection")
    parser.add_argument('--no-auth', action='store_true',
                        help="disable bearer auth (loopback only; for local testing)")
    parser.add_argument('--generate-token', action='store_true', help="print a random token and exit")
    args = parser.parse_args(argv)
    env_hosts = [h.strip() for h in os.getenv('FINHUB_ALLOWED_HOSTS', '').split(',') if h.strip()]
    args.allowed_hosts = args.allow_host + env_hosts
    return args


def build_http_app(transport: str, host: str, port: int, token: str | None, allowed_hosts: list[str] | None = None):
    """The ASGI app for an HTTP transport, wrapped in bearer auth when a token is given."""
    server = create_mcp(host, port, allowed_hosts or None)
    app = server.streamable_http_app() if transport == 'streamable-http' else server.sse_app()
    if token:
        app.add_middleware(BearerTokenMiddleware, token=token)
    return app


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.generate_token:
        print(generate_token())
        return
    if args.transport == 'stdio':
        mcp.run()
        return
    token = os.getenv('FINHUB_HTTP_TOKEN') or None
    try:
        validate_startup(args.host, token, args.no_auth)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    import uvicorn
    app = build_http_app(args.transport, args.host, args.port, token, args.allowed_hosts)
    path = '/mcp' if args.transport == 'streamable-http' else '/sse'
    print(f"FinHub MCP listening on http://{args.host}:{args.port}{path} "
          f"({'bearer auth' if token else 'NO AUTH, loopback only'})", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level='info')


if __name__ == '__main__':
    main()
