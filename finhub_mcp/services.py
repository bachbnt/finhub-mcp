# Copyright (c) 2026 bachbnt. All rights reserved.
"""Cached data-access helpers shared by the MCP tools and the alert daemon.

Everything returned from a cached helper is shared between callers: do not mutate it.
"""

import json
from datetime import datetime, timedelta
from typing import Any

from finhub_mcp.cache import ttl_cache
from finhub_mcp.config import get_ttl
from finhub_mcp.providers import (
    SUPPORTED_EXCHANGES,
    VN_QUOTE_SOURCES,
    fallback_used,
    first_success,
    get_exchange,
    quiet,
    timeframe_to_ms,
    vn_listing,
    vn_quote,
    vn_stock,
)

# ---------------------------------------------------------------- crypto


@ttl_cache('ticker')
def fetch_crypto_ticker(exchange: str, symbol: str) -> dict[str, Any]:
    return get_exchange(exchange).fetch_ticker(symbol)


@ttl_cache('ticker')
def fetch_crypto_tickers(exchange: str) -> dict[str, Any]:
    return get_exchange(exchange).fetch_tickers()


@ttl_cache('ticker')
def fetch_crypto_tickers_for(exchange: str, symbols: tuple[str, ...]) -> dict[str, Any]:
    """Tickers for the subset of ``symbols`` listed on the exchange, keyed by symbol."""
    ex = get_exchange(exchange)
    ex.load_markets()
    listed = [s for s in symbols if s in ex.markets]
    if not listed:
        raise ValueError(f"none of the requested symbols are listed on {exchange}")
    return ex.fetch_tickers(listed)


def _ohlcv_ttl(exchange: str, symbol: str, timeframe: str, limit: int) -> float:
    """Cache candles for at most a quarter of the candle length, so 1m data is not 5 minutes stale."""
    base = get_ttl('history')
    try:
        candle_seconds = timeframe_to_ms(timeframe) / 1000
    except ValueError:
        return 0
    return min(base, max(5, candle_seconds / 4))


@ttl_cache('history', ttl=_ohlcv_ttl)
def fetch_crypto_ohlcv(exchange: str, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
    return get_exchange(exchange).fetch_ohlcv(symbol, timeframe, limit=limit)


def crypto_last_price(symbol: str, exchanges: list[str] | None = None) -> float:
    """Latest spot price for a normalized pair, falling back across exchanges."""
    exchanges = exchanges or list(SUPPORTED_EXCHANGES)

    def fetch(name: str):
        ticker = fetch_crypto_ticker(name, symbol)
        if ticker.get('last') is None:
            raise ValueError(f"no last price for {symbol}")
        return ticker

    _, ticker = first_success(exchanges, fetch, f"no ticker found for {symbol}")
    return float(ticker['last'])


# ------------------------------------------------------------ vn stocks


def date_range(days: int) -> tuple[str, str]:
    """(start, end) as YYYY-MM-DD strings covering the last ``days`` calendar days."""
    now = datetime.now()
    return (now - timedelta(days=days)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')


def _vn_history(symbol: str, source: str, start: str, end: str):
    return quiet(vn_quote(symbol, source).history, start=start, end=end, interval='1D')


# Same fetch, three freshness levels: daily history, latest quote, index overview.
fetch_vn_history = ttl_cache('history')(_vn_history)
fetch_vn_latest = ttl_cache('ticker')(_vn_history)
fetch_vn_index_history = ttl_cache('market_overview')(_vn_history)


@ttl_cache('history')
def fetch_vn_listing(source: str):
    return quiet(vn_listing(source).all_symbols)


@ttl_cache('history')
def fetch_company_overview(symbol: str, source: str):
    return quiet(vn_stock(symbol, source).company.overview)


@ttl_cache('history')
def fetch_financials(symbol: str, source: str, statement: str, period: str):
    stock = vn_stock(symbol, source)
    fetchers = {
        'income_statement': lambda: stock.finance.income_statement(period=period),
        'balance_sheet': lambda: stock.finance.balance_sheet(period=period),
        'cash_flow': lambda: stock.finance.cash_flow(period=period),
        'ratio': lambda: stock.finance.ratio(lang='vi'),
    }
    return quiet(fetchers[statement])


def frame_records(df) -> list[dict[str, Any]]:
    """DataFrame -> JSON-safe list of records (NaN -> null, unicode kept)."""
    return json.loads(df.to_json(orient='records', force_ascii=False))


def price_snapshot(df) -> dict[str, Any]:
    """Latest OHLCV row plus day-over-day change from a daily history frame."""
    row = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else row
    close = float(row['close'])
    prev_close = float(prev['close'])
    change = close - prev_close
    return {
        'date': row['time'].strftime('%Y-%m-%d'),
        'open': float(row['open']),
        'high': float(row['high']),
        'low': float(row['low']),
        'close': close,
        'change': round(change, 2),
        'change_pct': round(change / prev_close * 100, 2) if prev_close else None,
        'volume': int(row['volume']),
    }


def vn_last_close(symbol: str, sources: list[str] | None = None) -> float:
    """Latest closing price for a Vietnam symbol, falling back across sources."""
    sources = sources or list(VN_QUOTE_SOURCES)
    start, end = date_range(10)
    _, df = first_success(
        sources,
        lambda src: fetch_vn_latest(symbol, src, start, end),
        f"no data found for {symbol}",
    )
    return float(df.iloc[-1]['close'])


def vn_index_snapshot(index: str, sources: tuple[str, ...]) -> dict[str, Any]:
    """Snapshot of a VN index (close, change, volume) with source fallback."""
    start, end = date_range(10)
    used, df = first_success(
        list(sources),
        lambda src: fetch_vn_index_history(index, src, start, end),
        f"no data found for {index}",
    )
    snap = price_snapshot(df)
    return {
        'close': snap['close'],
        'change': snap['change'],
        'change_pct': snap['change_pct'],
        'volume': snap['volume'],
        'date': snap['date'],
        'source': used,
        'fallback_used': fallback_used(used, list(sources)),
    }
