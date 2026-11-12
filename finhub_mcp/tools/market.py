# Copyright (c) 2026 bachbnt. All rights reserved.

from datetime import datetime

from finhub_mcp.cache import ttl_cache
from finhub_mcp.providers import quiet, silent_import
from finhub_mcp.responses import success
from finhub_mcp.services import frame_records
from finhub_mcp.tooling import register_tool


@ttl_cache('market_overview')
def _gold_prices():
    sjc_gold_price = silent_import('vnstock.explorer.misc.gold_price', 'sjc_gold_price')
    return frame_records(quiet(sjc_gold_price))


@ttl_cache('market_overview')
def _forex_rates(day: str):
    vcb_exchange_rate = silent_import('vnstock.explorer.misc.exchange_rate', 'vcb_exchange_rate')
    return frame_records(quiet(vcb_exchange_rate, day))


def register(mcp) -> None:
    tool = register_tool(mcp)

    @tool
    def get_gold_price() -> str:
        """Get the current SJC gold buying and selling prices across branches in Vietnam."""
        return success(_gold_prices())

    @tool
    def get_forex() -> str:
        """Get today's foreign exchange rates from Vietcombank."""
        return success(_forex_rates(datetime.now().strftime('%Y-%m-%d')))
