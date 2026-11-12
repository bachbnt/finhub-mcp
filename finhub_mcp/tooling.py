# Copyright (c) 2026 bachbnt. All rights reserved.

import asyncio
import functools

from finhub_mcp.responses import error


def register_tool(mcp):
    """Decorator that registers a blocking function as an MCP tool.

    The function runs in a worker thread, so a slow provider call never blocks the
    event loop or other tool calls, and any exception is converted into the standard
    error envelope. The function itself just returns a response string.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except Exception as e:
                return error(e)

        mcp.tool()(wrapper)
        return fn

    return decorator
