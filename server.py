# Copyright (c) 2026 bachbnt. All rights reserved.
#
# MCP server configuration (add to ~/.claude/settings.json):
# {
#   "mcpServers": {
#     "finance": {
#       "command": "/Users/bachbui/Desktop/source/finance-mcp/.venv/bin/python",
#       "args": ["/Users/bachbui/Desktop/source/finance-mcp/server.py"]
#     }
#   }
# }

import contextlib
import importlib
import io
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("finance-mcp")

# Path to the shared alerts store used by both server and alert_daemon
ALERTS_FILE = os.path.join(os.path.dirname(__file__), 'alerts.json')

_VN_QUOTE_SOURCES = ('VCI', 'KBS', 'MSN')
_VN_INTRADAY_SOURCES = ('KBS', 'VCI', 'MSN')
_VN_LISTING_SOURCES = ('VCI', 'KBS')
_VN_STOCK_SOURCES = ('VCI', 'KBS')
_SUPPORTED_EXCHANGES = ('binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc')
_FUTURES_DEFAULT_TYPES = {
    'binance': 'future',
    'okx': 'swap',
    'bybit': 'swap',
    'kucoin': 'swap',
    'gate': 'swap',
    'mexc': 'swap',
}


# ─── INTERNAL HELPERS ────────────────────────────────────────────────────────

def _quiet(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) while suppressing stdout (vnstock prints banners/ads).

    Args:
        fn: Callable to invoke.
        *args, **kwargs: Forwarded to fn.

    Returns:
        Whatever fn returns.
    """
    buf = io.StringIO()
    previous_disable = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return fn(*args, **kwargs)
    finally:
        logging.disable(previous_disable)


def _silent_import(module: str, name: str):
    """Import a vnstock symbol without leaking banners to MCP stdout."""
    buf = io.StringIO()
    previous_disable = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            module_obj = importlib.import_module(module)
        return getattr(module_obj, name)
    finally:
        logging.disable(previous_disable)


def _load_alerts() -> list:
    """Load the alert list from the JSON store.

    Returns:
        List of alert dicts, or an empty list if the file does not exist.
    """
    if os.path.exists(ALERTS_FILE):
        with open(ALERTS_FILE) as f:
            return json.load(f)
    return []


def _save_alerts(alerts: list) -> None:
    """Persist the alert list to the JSON store.

    Args:
        alerts: List of alert dicts to write.
    """
    with open(ALERTS_FILE, 'w') as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)


def _provider_order(value: str, defaults: tuple[str, ...], label: str) -> list[str]:
    """Build a provider fallback order from 'auto' or a comma-separated list."""
    if value is None or value.strip().lower() in {'auto', 'fallback', 'any'}:
        parts = defaults
    else:
        parts = tuple(part.strip() for part in value.split(',') if part.strip())

    allowed = {item.lower(): item for item in defaults}
    order = []
    for part in parts:
        key = part.lower()
        if key not in allowed:
            raise ValueError(f"Unsupported {label}: {part}. Choose from: auto, {', '.join(defaults)}")
        canonical = allowed[key]
        if canonical not in order:
            order.append(canonical)
    if not order:
        raise ValueError(f"No {label} providers configured")
    return order


def _first_success(providers: list[str], fn, empty_msg: str = 'no data'):
    """Run fn(provider) until one provider returns a non-empty result."""
    errors = []
    for provider in providers:
        try:
            result = fn(provider)
            is_empty = getattr(result, 'empty', None)
            if is_empty is True or (is_empty is None and hasattr(result, '__len__') and len(result) == 0):
                errors.append(f"{provider}: {empty_msg}")
                continue
            return provider, result
        except Exception as e:
            errors.append(f"{provider}: {e}")
    raise RuntimeError(f"All providers failed ({'; '.join(errors)})")


def _fallback_used(provider: str, providers: list[str]) -> bool:
    return len(providers) > 1 and provider != providers[0]


def _vn_quote(symbol: str, source: str = 'VCI'):
    """Return a vnstock Quote object for a Vietnam-listed symbol.

    Args:
        symbol: Ticker symbol, e.g. 'VNM', 'TCB'.
        source: vnstock quote source, e.g. 'VCI', 'KBS', or 'MSN'.

    Returns:
        vnstock Quote instance ready for history/intraday queries.
    """
    Quote = _silent_import('vnstock.api.quote', 'Quote')
    return _quiet(Quote, symbol=symbol.upper(), source=source)


def _vn_stock(symbol: str, source: str = 'VCI'):
    """Return a Vnstock stock object for company/financial data.

    Args:
        symbol: Ticker symbol, e.g. 'VNM'.
        source: Data source identifier (default 'VCI').

    Returns:
        Vnstock stock component with .company and .finance sub-objects.
    """
    buf = io.StringIO()
    previous_disable = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            from vnstock import Vnstock
            return Vnstock().stock(symbol=symbol.upper(), source=source)
    finally:
        logging.disable(previous_disable)


def _vn_listing(source: str):
    Listing = _silent_import('vnstock.api.listing', 'Listing')
    return _quiet(Listing, source=source)


def _get_exchange(name: str):
    """Create a ccxt spot exchange instance with rate-limiting enabled.

    Args:
        name: Exchange name, must be one of _SUPPORTED_EXCHANGES.

    Returns:
        ccxt Exchange instance configured for spot trading.

    Raises:
        ValueError: If the exchange name is not supported.
    """
    import ccxt
    if name.lower() not in _SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange. Choose from: {', '.join(sorted(_SUPPORTED_EXCHANGES))}")
    return getattr(ccxt, name.lower())({'enableRateLimit': True})


def _get_futures_exchange(name: str):
    """Create a ccxt futures/perpetual exchange instance with rate-limiting enabled.

    Args:
        name: Exchange name, must be one of _SUPPORTED_EXCHANGES.

    Returns:
        ccxt Exchange instance configured for futures (defaultType='future').

    Raises:
        ValueError: If the exchange name is not supported.
    """
    import ccxt
    if name.lower() not in _SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange. Choose from: {', '.join(sorted(_SUPPORTED_EXCHANGES))}")
    default_type = _FUTURES_DEFAULT_TYPES.get(name.lower(), 'swap')
    return getattr(ccxt, name.lower())({'enableRateLimit': True, 'options': {'defaultType': default_type}})


def _crypto_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if '/' not in symbol:
        return f"{symbol}/USDT"
    return symbol


def _ts_to_utc(ts_ms: int) -> str:
    """Convert a millisecond UNIX timestamp to a UTC datetime string.

    Args:
        ts_ms: Timestamp in milliseconds.

    Returns:
        String formatted as 'YYYY-MM-DD HH:MM' in UTC.
    """
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def _timeframe_to_ms(timeframe: str) -> int:
    """Convert a ccxt timeframe string like '5m' or '1h' to milliseconds."""
    unit = timeframe[-1]
    try:
        value = int(timeframe[:-1])
    except ValueError as e:
        raise ValueError("Invalid timeframe. Use values like 1m, 5m, 15m, 1h, 4h.") from e
    multipliers = {
        's': 1000,
        'm': 60 * 1000,
        'h': 60 * 60 * 1000,
        'd': 24 * 60 * 60 * 1000,
        'w': 7 * 24 * 60 * 60 * 1000,
    }
    if unit not in multipliers:
        raise ValueError("Invalid timeframe. Use values like 1m, 5m, 15m, 1h, 4h.")
    return value * multipliers[unit]


# ─── VN STOCK ────────────────────────────────────────────────────────────────

@mcp.tool()
def get_vn_stock_price(symbol: str, source: str = 'auto') -> str:
    """Get the latest price snapshot for a Vietnam-listed stock.

    Fetches the most recent daily OHLCV candle and computes the day-over-day change.

    Args:
        symbol: Ticker symbol listed on HOSE or HNX, e.g. VNM, TCB, VIC, HPG, FPT.
        source: Data source: auto (VCI -> KBS -> MSN) or one/comma-separated list.

    Returns:
        JSON string with fields: symbol, date, open, high, low, close,
        change (absolute), change_pct (%), volume, source, fallback_used.
        Returns an error string on failure.
    """
    try:
        sources = _provider_order(source, _VN_QUOTE_SOURCES, 'VN quote source')
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        used_source, df = _first_success(
            sources,
            lambda src: _quiet(_vn_quote(symbol, src).history, start=start, end=end, interval='1D'),
            f"no data found for {symbol.upper()}",
        )
        row = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else row
        change = float(row['close']) - float(prev['close'])
        return json.dumps({
            'symbol': symbol.upper(),
            'date': row['time'].strftime('%Y-%m-%d'),
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'change': round(change, 2),
            'change_pct': round((change / float(prev['close'])) * 100, 2),
            'volume': int(row['volume']),
            'source': used_source,
            'fallback_used': _fallback_used(used_source, sources),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_vn_stock_history(symbol: str, days: int = 30, source: str = 'auto') -> str:
    """Get historical daily OHLCV data for a Vietnam-listed stock.

    Args:
        symbol: Ticker symbol, e.g. VNM, TCB.
        days: Number of calendar days to look back (1–365, default 30).
        source: Data source: auto (VCI -> KBS -> MSN) or one/comma-separated list.

    Returns:
        JSON array of objects with fields: date, open, high, low, close, volume, source.
        Returns an error string on failure.
    """
    try:
        sources = _provider_order(source, _VN_QUOTE_SOURCES, 'VN quote source')
        days = min(max(days, 1), 365)
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        used_source, df = _first_success(
            sources,
            lambda src: _quiet(_vn_quote(symbol, src).history, start=start, end=end, interval='1D'),
            f"no data found for {symbol.upper()}",
        )
        df['date'] = df['time'].dt.strftime('%Y-%m-%d')
        result = df[['date', 'open', 'high', 'low', 'close', 'volume']].to_dict(orient='records')
        for item in result:
            item['source'] = used_source
            item['fallback_used'] = _fallback_used(used_source, sources)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_vn_stock_intraday(
    symbol: str,
    period: str = 'latest',
    limit: int = 100,
    page_size: int = 100,
    pages: int = 5,
    source: str = 'auto',
) -> str:
    """Get intraday matched trades for a Vietnam-listed stock.

    Args:
        symbol: Ticker symbol, e.g. FPT, VNM, TCB.
        period: latest (most recent trading session) or today (strict calendar day).
        limit: Maximum rows to return (1-1000). Default 100.
        page_size: Records per provider page (10-1000). Default 100.
        pages: Number of provider pages to scan (1-20). Default 5.
        source: Data source: auto (KBS -> VCI -> MSN) or one/comma-separated list.

    Returns:
        JSON array of intraday trades with time, price, volume, match_type, source.
        Returns an error string on failure.
    """
    try:
        sources = _provider_order(source, _VN_INTRADAY_SOURCES, 'VN intraday source')
        period = period.lower()
        if period not in {'latest', 'today'}:
            return "Invalid period. Choose from: latest, today"
        limit = min(max(limit, 1), 1000)
        page_size = min(max(page_size, 10), 1000)
        pages = min(max(pages, 1), 20)

        def fetch(src: str):
            frames = []
            quote = _vn_quote(symbol, src)
            for page in range(1, pages + 1):
                df = _quiet(quote.intraday, page_size=page_size, page=page)
                if getattr(df, 'empty', True):
                    break
                frames.append(df)
            if not frames:
                return []
            records = []
            for df in frames:
                for row in df.to_dict(orient='records'):
                    records.append(row)
            return records

        used_source, records = _first_success(
            sources,
            fetch,
            f"no intraday data found for {symbol.upper()}",
        )
        today = datetime.now().date()
        normalized = []
        for row in records:
            ts = row.get('time')
            if hasattr(ts, 'to_pydatetime'):
                dt = ts.to_pydatetime()
            elif isinstance(ts, datetime):
                dt = ts
            else:
                dt = datetime.fromisoformat(str(ts))
            if period == 'today' and dt.date() != today:
                continue
            normalized.append({
                'symbol': symbol.upper(),
                'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
                'price': float(row['price']) if row.get('price') is not None else None,
                'volume': int(row['volume']) if row.get('volume') is not None else None,
                'match_type': row.get('match_type'),
                'id': row.get('id'),
                'source': used_source,
                'fallback_used': _fallback_used(used_source, sources),
            })

        normalized.sort(key=lambda item: item['time'], reverse=True)
        return json.dumps(normalized[:limit], ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_vn_market_overview(source: str = 'auto') -> str:
    """Get a snapshot of the main Vietnam market indices: VNINDEX, VN30, and HNXINDEX.

    Args:
        source: Data source: auto (VCI -> KBS -> MSN) or one/comma-separated list.

    Returns:
        JSON object keyed by index name, each with: close, change, change_pct (%),
        volume, date, source, and fallback_used.
        Returns an error string on failure.
    """
    results = {}
    sources = _provider_order(source, _VN_QUOTE_SOURCES, 'VN quote source')
    for idx in ['VNINDEX', 'VN30', 'HNXINDEX']:
        try:
            end = datetime.now().strftime('%Y-%m-%d')
            start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
            used_source, df = _first_success(
                sources,
                lambda src: _quiet(_vn_quote(idx, src).history, start=start, end=end, interval='1D'),
                f"no data found for {idx}",
            )
            row = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else row
            change = float(row['close']) - float(prev['close'])
            results[idx] = {
                'close': float(row['close']),
                'change': round(change, 2),
                'change_pct': round((change / float(prev['close'])) * 100, 2),
                'volume': int(row['volume']),
                'date': row['time'].strftime('%Y-%m-%d'),
                'source': used_source,
                'fallback_used': _fallback_used(used_source, sources),
            }
        except Exception:
            pass
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
def search_vn_stock(query: str, source: str = 'auto') -> str:
    """Search for Vietnam stock tickers by symbol or company name.

    Args:
        query: Partial symbol or company name to search for, e.g. 'vinamilk' or 'VNM'.
        source: Data source: auto (VCI -> KBS) or one/comma-separated list.

    Returns:
        JSON array of up to 20 matching records with symbol, company name, and source.
        Returns an error string on failure.
    """
    try:
        sources = _provider_order(source, _VN_LISTING_SOURCES, 'VN listing source')
        used_source, listing = _first_success(
            sources,
            lambda src: _quiet(_vn_listing(src).all_symbols),
            'no symbols found',
        )
        q = query.lower()
        mask = listing['symbol'].str.lower().str.contains(q, na=False)
        if 'organ_name' in listing.columns:
            mask |= listing['organ_name'].str.lower().str.contains(q, na=False)
        matches = listing[mask].head(20)
        matches = matches.copy()
        matches['source'] = used_source
        matches['fallback_used'] = _fallback_used(used_source, sources)
        return matches.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Error: {e}"


# ─── CRYPTO ──────────────────────────────────────────────────────────────────

@mcp.tool()
def get_crypto_price(symbol: str, exchange: str = 'auto') -> str:
    """Get the current spot price and 24-hour statistics for a cryptocurrency pair.

    Args:
        symbol: Base asset or full pair, e.g. BTC, ETH, SOL, or BTC/USDT.
                Defaults to USDT quote if no quote is specified.
        exchange: auto, one exchange, or comma-separated fallback list.

    Returns:
        JSON object with: symbol, exchange, last, bid, ask, high_24h, low_24h,
        volume_24h_base, volume_24h_usdt, change_pct_24h (%), vwap, fallback_used.
        Returns an error string on failure.
    """
    try:
        exchanges = _provider_order(exchange, _SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = _crypto_symbol(symbol)
        used_exchange, t = _first_success(
            exchanges,
            lambda ex_name: _get_exchange(ex_name).fetch_ticker(symbol),
            f"no ticker found for {symbol}",
        )
        return json.dumps({
            'symbol': symbol,
            'exchange': used_exchange,
            'last': t['last'],
            'bid': t['bid'],
            'ask': t['ask'],
            'high_24h': t['high'],
            'low_24h': t['low'],
            'volume_24h_base': t['baseVolume'],
            'volume_24h_usdt': round(t.get('quoteVolume') or 0, 2),
            'change_pct_24h': round(t.get('percentage') or 0, 2),
            'vwap': t.get('vwap'),
            'fallback_used': _fallback_used(used_exchange, exchanges),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_history(symbol: str, timeframe: str = '1d', limit: int = 30, exchange: str = 'auto') -> str:
    """Get historical OHLCV candlestick data for a cryptocurrency pair.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        timeframe: Candle interval — 1m, 5m, 15m, 1h, 4h, 1d, 1w. Default 1d.
        limit: Number of candles to return. Default 30.
        exchange: auto, one exchange, or comma-separated fallback list.

    Returns:
        JSON array with time (UTC), open, high, low, close, volume, exchange, fallback_used.
        Returns an error string on failure.
    """
    try:
        exchanges = _provider_order(exchange, _SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = _crypto_symbol(symbol)
        used_exchange, ohlcv = _first_success(
            exchanges,
            lambda ex_name: _get_exchange(ex_name).fetch_ohlcv(symbol, timeframe, limit=limit),
            f"no candles found for {symbol}",
        )
        result = [{
            'time': _ts_to_utc(ts),
            'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
            'exchange': used_exchange,
            'fallback_used': _fallback_used(used_exchange, exchanges),
        } for ts, o, h, l, c, v in ohlcv]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_intraday(
    symbol: str,
    timeframe: str = '5m',
    hours: int = 24,
    max_candles: int = 1000,
    exchange: str = 'auto',
) -> str:
    """Get intraday OHLCV candles for a cryptocurrency over the last N hours.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        timeframe: Candle interval, e.g. 1m, 5m, 15m, 1h. Default 5m.
        hours: Lookback window in hours (1-24). Default 24.
        max_candles: Maximum candles to return (1-2000). Default 1000.
        exchange: auto, one exchange, or comma-separated fallback list.

    Returns:
        JSON array with time (UTC), open, high, low, close, volume, exchange.
        Returns an error string on failure.
    """
    try:
        exchanges = _provider_order(exchange, _SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = _crypto_symbol(symbol)
        hours = min(max(hours, 1), 24)
        max_candles = min(max(max_candles, 1), 2000)
        timeframe_ms = _timeframe_to_ms(timeframe)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        since_ms = now_ms - hours * 60 * 60 * 1000

        def fetch(exchange_name: str):
            ex = _get_exchange(exchange_name)
            candles = []
            seen = set()
            cursor = since_ms
            while cursor < now_ms and len(candles) < max_candles:
                batch_limit = min(1000, max_candles - len(candles))
                batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=batch_limit)
                if not batch:
                    break
                for candle in batch:
                    ts = candle[0]
                    if since_ms <= ts <= now_ms and ts not in seen:
                        candles.append(candle)
                        seen.add(ts)
                next_cursor = batch[-1][0] + timeframe_ms
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
                if len(batch) < batch_limit and batch[-1][0] >= now_ms - timeframe_ms * 2:
                    break
            candles.sort(key=lambda item: item[0])
            return candles

        used_exchange, ohlcv = _first_success(
            exchanges,
            fetch,
            f"no intraday candles found for {symbol}",
        )
        result = [{
            'time': _ts_to_utc(ts),
            'open': o,
            'high': h,
            'low': l,
            'close': c,
            'volume': v,
            'symbol': symbol,
            'timeframe': timeframe,
            'hours': hours,
            'exchange': used_exchange,
            'fallback_used': _fallback_used(used_exchange, exchanges),
        } for ts, o, h, l, c, v in ohlcv]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_top_crypto(limit: int = 10, exchange: str = 'auto') -> str:
    """List the top cryptocurrencies ranked by 24-hour USDT trading volume.

    Args:
        limit: Number of results to return. Default 10.
        exchange: auto, one exchange, or comma-separated fallback list.

    Returns:
        JSON array ordered by volume descending, each item with:
        symbol, last price, change_pct_24h (%), volume_usdt_24h, exchange.
        Returns an error string on failure.
    """
    try:
        exchanges = _provider_order(exchange, _SUPPORTED_EXCHANGES, 'crypto exchange')
        used_exchange, tickers = _first_success(
            exchanges,
            lambda ex_name: _get_exchange(ex_name).fetch_tickers(),
            'no tickers found',
        )
        usdt = {k: v for k, v in tickers.items() if k.endswith('/USDT') and v.get('quoteVolume')}
        top = sorted(usdt.values(), key=lambda x: x.get('quoteVolume', 0), reverse=True)[:limit]
        result = [{
            'symbol': t['symbol'],
            'last': t['last'],
            'change_pct_24h': round(t.get('percentage') or 0, 2),
            'volume_usdt_24h': round(t.get('quoteVolume') or 0, 0),
            'exchange': used_exchange,
            'fallback_used': _fallback_used(used_exchange, exchanges),
        } for t in top]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_orderbook(symbol: str, depth: int = 10, exchange: str = 'auto') -> str:
    """Get the current order book (bid/ask depth) for a cryptocurrency pair.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        depth: Number of price levels per side (5–50). Default 10.
        exchange: auto, one exchange, or comma-separated fallback list.

    Returns:
        JSON object with: symbol, exchange, datetime, spread (absolute),
        spread_pct (%), bids (list of [price, size]), asks (list of [price, size]).
        Returns an error string on failure.
    """
    try:
        exchanges = _provider_order(exchange, _SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = _crypto_symbol(symbol)
        depth = min(max(depth, 5), 50)
        used_exchange, ob = _first_success(
            exchanges,
            lambda ex_name: _get_exchange(ex_name).fetch_order_book(symbol, limit=depth),
            f"no order book found for {symbol}",
        )
        spread = ob['asks'][0][0] - ob['bids'][0][0] if ob['bids'] and ob['asks'] else None
        return json.dumps({
            'symbol': symbol,
            'exchange': used_exchange,
            'datetime': ob.get('datetime'),
            'spread': round(spread, 4) if spread else None,
            'spread_pct': round(spread / ob['asks'][0][0] * 100, 4) if spread else None,
            'bids': ob['bids'][:depth],
            'asks': ob['asks'][:depth],
            'fallback_used': _fallback_used(used_exchange, exchanges),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_trades(symbol: str, limit: int = 20, exchange: str = 'auto') -> str:
    """Get the most recent public trades (market fills) for a cryptocurrency pair.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        limit: Number of trades to return (1–50). Default 20.
        exchange: auto, one exchange, or comma-separated fallback list.

    Returns:
        JSON array with time, side, price, amount, cost, exchange, fallback_used.
        Returns an error string on failure.
    """
    try:
        exchanges = _provider_order(exchange, _SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = _crypto_symbol(symbol)
        limit = min(max(limit, 1), 50)
        used_exchange, trades = _first_success(
            exchanges,
            lambda ex_name: _get_exchange(ex_name).fetch_trades(symbol, limit=limit),
            f"no trades found for {symbol}",
        )
        result = [{
            'time': t['datetime'],
            'side': t['side'],
            'price': t['price'],
            'amount': t['amount'],
            'cost': t.get('cost'),
            'exchange': used_exchange,
            'fallback_used': _fallback_used(used_exchange, exchanges),
        } for t in trades]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_funding_rate(symbol: str, exchange: str = 'auto', history_limit: int = 8) -> str:
    """Get the current funding rate and recent history for a perpetual futures contract.

    Funding rates are charged every 8 hours on most exchanges.
    Positive rate = longs pay shorts; negative = shorts pay longs.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        exchange: auto, one exchange, or comma-separated fallback list.
        history_limit: Number of past funding periods to include (default 8 ≈ last 24 h).

    Returns:
        JSON object with: symbol, exchange, funding_rate, funding_rate_pct (%),
        mark_price, index_price, next_funding (datetime), and a history array
        with datetime, rate, rate_pct for each past period.
        Returns an error string on failure.
    """
    try:
        exchanges = _provider_order(exchange, _SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = _crypto_symbol(symbol)

        def fetch(exchange_name: str):
            ex = _get_futures_exchange(exchange_name)
            fr = ex.fetch_funding_rate(symbol)
            history = ex.fetch_funding_rate_history(symbol, limit=history_limit)
            return fr, history

        used_exchange, payload = _first_success(exchanges, fetch, f"no funding data found for {symbol}")
        fr, history = payload
        return json.dumps({
            'symbol': symbol,
            'exchange': used_exchange,
            'funding_rate': fr.get('fundingRate'),
            'funding_rate_pct': round((fr.get('fundingRate') or 0) * 100, 6),
            'mark_price': fr.get('markPrice'),
            'index_price': fr.get('indexPrice'),
            'next_funding': fr.get('nextFundingDatetime'),
            'fallback_used': _fallback_used(used_exchange, exchanges),
            'history': [
                {'datetime': h['datetime'], 'rate': h['fundingRate'], 'rate_pct': round(h['fundingRate'] * 100, 6)}
                for h in history
            ],
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_open_interest(symbol: str, exchange: str = 'auto') -> str:
    """Get the current open interest for a perpetual futures contract.

    Open interest is the total number of outstanding contracts not yet settled.
    Rising OI with rising price signals strong trend; divergence may indicate reversal.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        exchange: auto, one exchange, or comma-separated fallback list.

    Returns:
        JSON object with: symbol, exchange, datetime,
        open_interest_coins (in base asset), open_interest_usdt (notional value).
        Returns an error string on failure.
    """
    try:
        exchanges = _provider_order(exchange, _SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = _crypto_symbol(symbol)
        used_exchange, oi = _first_success(
            exchanges,
            lambda ex_name: _get_futures_exchange(ex_name).fetch_open_interest(symbol),
            f"no open interest found for {symbol}",
        )
        return json.dumps({
            'symbol': symbol,
            'exchange': used_exchange,
            'datetime': oi.get('datetime'),
            'open_interest_coins': oi.get('openInterestAmount'),
            'open_interest_usdt': oi.get('openInterestValue'),
            'fallback_used': _fallback_used(used_exchange, exchanges),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


# ─── COMPANY & FINANCIALS ────────────────────────────────────────────────────

@mcp.tool()
def get_company_overview(symbol: str, source: str = 'auto') -> str:
    """Get company profile information for a Vietnam-listed stock.

    Returns industry sector, market cap, listing exchange, website, and business description.

    Args:
        symbol: Ticker symbol, e.g. VNM, FPT, VIC.
        source: Data source: auto (VCI -> KBS) or one/comma-separated list.

    Returns:
        JSON array of company profile records with source metadata.
        Returns an error string on failure.
    """
    try:
        sources = _provider_order(source, _VN_STOCK_SOURCES, 'VN stock source')
        used_source, df = _first_success(
            sources,
            lambda src: _quiet(_vn_stock(symbol, src).company.overview),
            f"no company overview found for {symbol.upper()}",
        )
        df = df.copy()
        df['source'] = used_source
        df['fallback_used'] = _fallback_used(used_source, sources)
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_financials(symbol: str, statement: str = 'income_statement', period: str = 'year', source: str = 'auto') -> str:
    """Get financial statements for a Vietnam-listed stock.

    Args:
        symbol: Ticker symbol, e.g. VNM, TCB.
        statement: Report type — one of:
            income_statement, balance_sheet, cash_flow, ratio.
            Default income_statement.
        period: Reporting period — year or quarter. Default year.
                (Ignored for 'ratio' which always returns the latest data.)
        source: Data source: auto (VCI -> KBS) or one/comma-separated list.

    Returns:
        JSON array of financial records for the requested statement with source metadata.
        Returns an error string on failure.
    """
    try:
        valid_statements = {'income_statement', 'balance_sheet', 'cash_flow', 'ratio'}
        if statement not in valid_statements:
            return f"Invalid statement. Choose from: {', '.join(sorted(valid_statements))}"
        sources = _provider_order(source, _VN_STOCK_SOURCES, 'VN stock source')

        def fetch(src: str):
            stock = _vn_stock(symbol, src)
            fn_map = {
                'income_statement': lambda: stock.finance.income_statement(period=period),
                'balance_sheet': lambda: stock.finance.balance_sheet(period=period),
                'cash_flow': lambda: stock.finance.cash_flow(period=period),
                'ratio': lambda: stock.finance.ratio(lang='vi'),
            }
            return _quiet(fn_map[statement])

        used_source, df = _first_success(sources, fetch, f"no financials found for {symbol.upper()}")
        df = df.copy()
        df['source'] = used_source
        df['fallback_used'] = _fallback_used(used_source, sources)
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


# ─── MARKET EXTRAS ───────────────────────────────────────────────────────────

@mcp.tool()
def get_gold_price() -> str:
    """Get the current SJC gold buying and selling prices across branches in Vietnam.

    Returns:
        JSON array with fields: name, branch, buy_price, sell_price, date.
        Returns an error string on failure.
    """
    try:
        sjc_gold_price = _silent_import('vnstock.explorer.misc.gold_price', 'sjc_gold_price')
        df = _quiet(sjc_gold_price)
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_forex() -> str:
    """Get today's foreign exchange rates from Vietcombank (VCB).

    Covers major currencies: USD, EUR, JPY, CNY, GBP, AUD, and more.

    Returns:
        JSON array with fields: currency_code, currency_name,
        buy_cash, buy_transfer, sell, date.
        Returns an error string on failure.
    """
    try:
        vcb_exchange_rate = _silent_import('vnstock.explorer.misc.exchange_rate', 'vcb_exchange_rate')
        today = datetime.now().strftime('%Y-%m-%d')
        df = _quiet(vcb_exchange_rate, today)
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


# ─── ALERTS ──────────────────────────────────────────────────────────────────

@mcp.tool()
def add_alert(
    symbol: str,
    condition: str,
    price: float,
    asset_type: str = 'crypto',
    telegram_chat_id: str = '',
) -> str:
    """Create a price alert that will be monitored by alert_daemon.py.

    The daemon checks alerts every CHECK_INTERVAL seconds and sends a Telegram
    notification when the condition is met.

    Args:
        symbol: Asset symbol, e.g. BTC for crypto or VNM for VN stocks.
        condition: Trigger direction — 'above' (price rises to target)
                   or 'below' (price falls to target).
        price: Target price level that triggers the alert.
        asset_type: Asset class — 'crypto' or 'vn_stock'. Default 'crypto'.
        telegram_chat_id: Telegram chat ID to notify. Get it by sending /start
                          to the configured bot and reading the chat_id from the response.
                          Leave empty to suppress Telegram delivery (logs only).

    Returns:
        Confirmation string with the generated alert ID.
    """
    alerts = _load_alerts()
    alert = {
        'id': str(uuid.uuid4())[:8],
        'symbol': symbol.upper(),
        'asset_type': asset_type,
        'condition': condition,
        'price': price,
        'telegram_chat_id': telegram_chat_id,
        'created_at': datetime.now().isoformat(),
        'triggered': False,
    }
    alerts.append(alert)
    _save_alerts(alerts)
    return f"Alert created [{alert['id']}]: {alert['symbol']} {condition} {price}"


@mcp.tool()
def list_alerts() -> str:
    """List all price alerts currently stored in the alert store.

    Returns:
        JSON array of all alert objects, or a message if the store is empty.
        Each alert includes: id, symbol, asset_type, condition, price,
        telegram_chat_id, created_at, triggered (bool), and triggered_at / triggered_price
        when applicable.
    """
    alerts = _load_alerts()
    if not alerts:
        return "No alerts configured."
    return json.dumps(alerts, ensure_ascii=False, indent=2)


@mcp.tool()
def remove_alert(alert_id: str) -> str:
    """Delete a price alert by its ID.

    Args:
        alert_id: The 8-character ID returned when the alert was created.

    Returns:
        Confirmation string on success, or an error message if the ID was not found.
    """
    alerts = _load_alerts()
    filtered = [a for a in alerts if a['id'] != alert_id]
    if len(filtered) == len(alerts):
        return f"Alert ID not found: {alert_id}"
    _save_alerts(filtered)
    return f"Alert {alert_id} removed."


if __name__ == '__main__':
    mcp.run()
