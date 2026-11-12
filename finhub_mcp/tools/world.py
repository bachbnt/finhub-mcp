# Copyright (c) 2026 bachbnt. All rights reserved.

from concurrent.futures import ThreadPoolExecutor

from finhub_mcp.providers import clamp
from finhub_mcp.responses import error, success
from finhub_mcp.tooling import register_tool
from finhub_mcp.worldmarkets import (
    METALS,
    forex_rate,
    history_days,
    metal_price,
    parse_metal,
    parse_pair,
    usd_per_oz_to_vnd_per_luong,
    yahoo_history,
)

MAX_PAIRS = 20


def split_symbols(value, limit: int) -> list[str]:
    """Accept a list or a comma/space separated string; dedupe, preserve order, cap at limit."""
    if isinstance(value, str):
        value = value.replace(',', ' ').split()
    items = []
    for item in value or []:
        item = str(item).strip()
        if item and item not in items:
            items.append(item)
    if not items:
        raise ValueError("Provide at least one symbol")
    if len(items) > limit:
        raise ValueError(f"Too many symbols ({len(items)}); the maximum is {limit}")
    return items


def register(mcp) -> None:
    tool = register_tool(mcp)

    @tool
    def get_world_forex(pairs: list[str] | str = 'EURUSD,GBPUSD,USDJPY,USDVND') -> str:
        """Get international spot forex rates (e.g. EURUSD, USD/JPY, USDVND) with day-over-day change.

        pairs: list or comma-separated string of 6-letter currency pairs (max 20).
        """
        items = [parse_pair(p) for p in split_symbols(pairs, MAX_PAIRS)]

        def fetch(pair):
            try:
                return forex_rate(''.join(pair)), None
            except Exception as e:
                return None, f"{'/'.join(pair)}: {e}"

        with ThreadPoolExecutor(max_workers=min(len(items), 8)) as pool:
            outcomes = list(pool.map(fetch, items))
        data = [row for row, err in outcomes if err is None]
        warnings = [err for row, err in outcomes if err is not None]
        if not data:
            return error(f"all pairs failed ({'; '.join(warnings)})")
        return success(data, warnings=warnings or None)

    @tool
    def get_world_forex_history(pair: str, days: int = 30, interval: str = '1d') -> str:
        """Get OHLC history for an international forex pair from Yahoo Finance.

        interval: 1m, 5m, 15m, 30m, 1h, 1d, 1wk or 1mo. Intraday intervals only reach back a limited
        number of days (1m: 7, 5m-30m: 59, 1h: 729); days is clamped to that.
        """
        base, quote = parse_pair(pair)
        days = history_days(clamp(days, 1, 3650), interval)
        rows = yahoo_history(f'{base}{quote}=X', days, interval)
        if not rows:
            return error(f"no history returned for {base}/{quote}")
        return success(rows, pair=f'{base}/{quote}', interval=interval, days=days, source='YAHOO')

    @tool
    def get_world_metal_price(metal: str = 'XAU') -> str:
        """Get the international USD price per troy ounce of gold (XAU), silver, platinum or palladium.

        metal: XAU (gold), XAG (silver), XPT (platinum) or XPD (palladium), or the English name.

        For gold, also returns an estimate of the equivalent VND price per lượng (37.5 g) from the
        live USD/VND rate, handy for comparing with SJC. The estimate excludes tax, premium and fees.
        """
        data = metal_price(parse_metal(metal))
        warnings = None
        if data['metal'] == 'XAU':
            try:
                usd_vnd = forex_rate('USDVND')['rate']
                data['usd_vnd'] = usd_vnd
                data['vnd_per_luong_estimate'] = round(usd_per_oz_to_vnd_per_luong(data['price'], usd_vnd))
            except Exception as e:
                warnings = {'vnd_per_luong_estimate': f"unavailable: {e}"}
        return success(data, warnings=warnings)

    @tool
    def get_world_metal_history(metal: str = 'XAU', days: int = 30, interval: str = '1d') -> str:
        """Get OHLC history for gold/silver/platinum/palladium futures (GC=F, SI=F, PL=F, PA=F) from Yahoo Finance.

        Futures prices differ slightly from the spot price returned by get_world_metal_price.
        """
        code = parse_metal(metal)
        days = history_days(clamp(days, 1, 3650), interval)
        symbol = METALS[code][1]
        rows = yahoo_history(symbol, days, interval)
        if not rows:
            return error(f"no history returned for {code}")
        return success(rows, metal=code, yahoo_symbol=symbol, interval=interval, days=days, source='YAHOO')
