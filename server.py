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
import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("finance-mcp")

# Path to the shared alerts store used by both server and alert_daemon
ALERTS_FILE = os.path.join(os.path.dirname(__file__), 'alerts.json')


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
    with contextlib.redirect_stdout(buf):
        return fn(*args, **kwargs)


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


def _vn_quote(symbol: str):
    """Return a vnstock Quote object for a Vietnam-listed symbol (VCI source).

    Args:
        symbol: Ticker symbol, e.g. 'VNM', 'TCB'.

    Returns:
        vnstock Quote instance ready for history/intraday queries.
    """
    from vnstock.api.quote import Quote
    return Quote(symbol=symbol.upper(), source='VCI')


def _vn_stock(symbol: str, source: str = 'VCI'):
    """Return a Vnstock stock object for company/financial data.

    Args:
        symbol: Ticker symbol, e.g. 'VNM'.
        source: Data source identifier (default 'VCI').

    Returns:
        Vnstock stock component with .company and .finance sub-objects.
    """
    from vnstock import Vnstock
    return Vnstock().stock(symbol=symbol.upper(), source=source)


_SUPPORTED_EXCHANGES = {'binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc'}


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
    return getattr(ccxt, name.lower())({'enableRateLimit': True, 'options': {'defaultType': 'future'}})


def _ts_to_utc(ts_ms: int) -> str:
    """Convert a millisecond UNIX timestamp to a UTC datetime string.

    Args:
        ts_ms: Timestamp in milliseconds.

    Returns:
        String formatted as 'YYYY-MM-DD HH:MM' in UTC.
    """
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


# ─── VN STOCK ────────────────────────────────────────────────────────────────

@mcp.tool()
def get_vn_stock_price(symbol: str) -> str:
    """Get the latest price snapshot for a Vietnam-listed stock.

    Fetches the most recent daily OHLCV candle and computes the day-over-day change.

    Args:
        symbol: Ticker symbol listed on HOSE or HNX, e.g. VNM, TCB, VIC, HPG, FPT.

    Returns:
        JSON string with fields: symbol, date, open, high, low, close,
        change (absolute), change_pct (%), volume.
        Returns an error string on failure.
    """
    try:
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        df = _quiet(_vn_quote(symbol).history, start=start, end=end, interval='1D')
        if df.empty:
            return f"No data found for {symbol.upper()}"
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
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_vn_stock_history(symbol: str, days: int = 30) -> str:
    """Get historical daily OHLCV data for a Vietnam-listed stock.

    Args:
        symbol: Ticker symbol, e.g. VNM, TCB.
        days: Number of calendar days to look back (1–365, default 30).

    Returns:
        JSON array of objects with fields: date, open, high, low, close, volume.
        Returns an error string on failure.
    """
    try:
        days = min(max(days, 1), 365)
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        df = _quiet(_vn_quote(symbol).history, start=start, end=end, interval='1D')
        if df.empty:
            return f"No data found for {symbol.upper()}"
        df['date'] = df['time'].dt.strftime('%Y-%m-%d')
        result = df[['date', 'open', 'high', 'low', 'close', 'volume']].to_dict(orient='records')
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_vn_market_overview() -> str:
    """Get a snapshot of the main Vietnam market indices: VNINDEX, VN30, and HNXINDEX.

    Returns:
        JSON object keyed by index name, each with: close, change, change_pct (%),
        volume, and date of the latest session.
        Returns an error string on failure.
    """
    results = {}
    for idx in ['VNINDEX', 'VN30', 'HNXINDEX']:
        try:
            end = datetime.now().strftime('%Y-%m-%d')
            start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
            df = _quiet(_vn_quote(idx).history, start=start, end=end, interval='1D')
            if df.empty:
                continue
            row = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else row
            change = float(row['close']) - float(prev['close'])
            results[idx] = {
                'close': float(row['close']),
                'change': round(change, 2),
                'change_pct': round((change / float(prev['close'])) * 100, 2),
                'volume': int(row['volume']),
                'date': row['time'].strftime('%Y-%m-%d'),
            }
        except Exception:
            pass
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
def search_vn_stock(query: str) -> str:
    """Search for Vietnam stock tickers by symbol or company name.

    Args:
        query: Partial symbol or company name to search for, e.g. 'vinamilk' or 'VNM'.

    Returns:
        JSON array of up to 20 matching records with symbol and company name.
        Returns an error string on failure.
    """
    try:
        from vnstock.api.listing import Listing
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            listing = Listing(source='VCI').all_symbols()
        q = query.lower()
        mask = listing['symbol'].str.lower().str.contains(q, na=False)
        if 'organ_name' in listing.columns:
            mask |= listing['organ_name'].str.lower().str.contains(q, na=False)
        matches = listing[mask].head(20)
        return matches.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Error: {e}"


# ─── CRYPTO ──────────────────────────────────────────────────────────────────

@mcp.tool()
def get_crypto_price(symbol: str, exchange: str = 'binance') -> str:
    """Get the current spot price and 24-hour statistics for a cryptocurrency pair.

    Args:
        symbol: Base asset or full pair, e.g. BTC, ETH, SOL, or BTC/USDT.
                Defaults to USDT quote if no quote is specified.
        exchange: Exchange name (binance, okx, bybit, kucoin, gate, mexc). Default binance.

    Returns:
        JSON object with: symbol, exchange, last, bid, ask, high_24h, low_24h,
        volume_24h_base, volume_24h_usdt, change_pct_24h (%), vwap.
        Returns an error string on failure.
    """
    try:
        ex = _get_exchange(exchange)
        symbol = symbol.upper()
        if '/' not in symbol:
            symbol = f"{symbol}/USDT"
        t = ex.fetch_ticker(symbol)
        return json.dumps({
            'symbol': symbol,
            'exchange': exchange,
            'last': t['last'],
            'bid': t['bid'],
            'ask': t['ask'],
            'high_24h': t['high'],
            'low_24h': t['low'],
            'volume_24h_base': t['baseVolume'],
            'volume_24h_usdt': round(t.get('quoteVolume') or 0, 2),
            'change_pct_24h': round(t.get('percentage') or 0, 2),
            'vwap': t.get('vwap'),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_history(symbol: str, timeframe: str = '1d', limit: int = 30, exchange: str = 'binance') -> str:
    """Get historical OHLCV candlestick data for a cryptocurrency pair.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        timeframe: Candle interval — 1m, 5m, 15m, 1h, 4h, 1d, 1w. Default 1d.
        limit: Number of candles to return. Default 30.
        exchange: Exchange name. Default binance.

    Returns:
        JSON array of candle objects with fields: time (UTC), open, high, low, close, volume.
        Returns an error string on failure.
    """
    try:
        ex = _get_exchange(exchange)
        symbol = symbol.upper()
        if '/' not in symbol:
            symbol = f"{symbol}/USDT"
        ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        result = [{
            'time': _ts_to_utc(ts),
            'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
        } for ts, o, h, l, c, v in ohlcv]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_top_crypto(limit: int = 10, exchange: str = 'binance') -> str:
    """List the top cryptocurrencies ranked by 24-hour USDT trading volume.

    Args:
        limit: Number of results to return. Default 10.
        exchange: Exchange name. Default binance.

    Returns:
        JSON array ordered by volume descending, each item with:
        symbol, last price, change_pct_24h (%), volume_usdt_24h.
        Returns an error string on failure.
    """
    try:
        ex = _get_exchange(exchange)
        tickers = ex.fetch_tickers()
        usdt = {k: v for k, v in tickers.items() if k.endswith('/USDT') and v.get('quoteVolume')}
        top = sorted(usdt.values(), key=lambda x: x.get('quoteVolume', 0), reverse=True)[:limit]
        result = [{
            'symbol': t['symbol'],
            'last': t['last'],
            'change_pct_24h': round(t.get('percentage') or 0, 2),
            'volume_usdt_24h': round(t.get('quoteVolume') or 0, 0),
        } for t in top]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_orderbook(symbol: str, depth: int = 10, exchange: str = 'binance') -> str:
    """Get the current order book (bid/ask depth) for a cryptocurrency pair.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        depth: Number of price levels per side (5–50). Default 10.
        exchange: Exchange name. Default binance.

    Returns:
        JSON object with: symbol, exchange, datetime, spread (absolute),
        spread_pct (%), bids (list of [price, size]), asks (list of [price, size]).
        Returns an error string on failure.
    """
    try:
        ex = _get_exchange(exchange)
        symbol = symbol.upper()
        if '/' not in symbol:
            symbol = f"{symbol}/USDT"
        depth = min(max(depth, 5), 50)
        ob = ex.fetch_order_book(symbol, limit=depth)
        spread = ob['asks'][0][0] - ob['bids'][0][0] if ob['bids'] and ob['asks'] else None
        return json.dumps({
            'symbol': symbol,
            'exchange': exchange,
            'datetime': ob.get('datetime'),
            'spread': round(spread, 4) if spread else None,
            'spread_pct': round(spread / ob['asks'][0][0] * 100, 4) if spread else None,
            'bids': ob['bids'][:depth],
            'asks': ob['asks'][:depth],
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_trades(symbol: str, limit: int = 20, exchange: str = 'binance') -> str:
    """Get the most recent public trades (market fills) for a cryptocurrency pair.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        limit: Number of trades to return (1–50). Default 20.
        exchange: Exchange name. Default binance.

    Returns:
        JSON array of trade objects with fields: time (ISO), side (buy/sell),
        price, amount, cost (price × amount).
        Returns an error string on failure.
    """
    try:
        ex = _get_exchange(exchange)
        symbol = symbol.upper()
        if '/' not in symbol:
            symbol = f"{symbol}/USDT"
        limit = min(max(limit, 1), 50)
        trades = ex.fetch_trades(symbol, limit=limit)
        result = [{
            'time': t['datetime'],
            'side': t['side'],
            'price': t['price'],
            'amount': t['amount'],
            'cost': t.get('cost'),
        } for t in trades]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_funding_rate(symbol: str, exchange: str = 'binance', history_limit: int = 8) -> str:
    """Get the current funding rate and recent history for a perpetual futures contract.

    Funding rates are charged every 8 hours on most exchanges.
    Positive rate = longs pay shorts; negative = shorts pay longs.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        exchange: Exchange name. Default binance.
        history_limit: Number of past funding periods to include (default 8 ≈ last 24 h).

    Returns:
        JSON object with: symbol, exchange, funding_rate, funding_rate_pct (%),
        mark_price, index_price, next_funding (datetime), and a history array
        with datetime, rate, rate_pct for each past period.
        Returns an error string on failure.
    """
    try:
        ex = _get_futures_exchange(exchange)
        symbol = symbol.upper()
        if '/' not in symbol:
            symbol = f"{symbol}/USDT"
        fr = ex.fetch_funding_rate(symbol)
        history = ex.fetch_funding_rate_history(symbol, limit=history_limit)
        return json.dumps({
            'symbol': symbol,
            'exchange': exchange,
            'funding_rate': fr.get('fundingRate'),
            'funding_rate_pct': round((fr.get('fundingRate') or 0) * 100, 6),
            'mark_price': fr.get('markPrice'),
            'index_price': fr.get('indexPrice'),
            'next_funding': fr.get('nextFundingDatetime'),
            'history': [
                {'datetime': h['datetime'], 'rate': h['fundingRate'], 'rate_pct': round(h['fundingRate'] * 100, 6)}
                for h in history
            ],
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_crypto_open_interest(symbol: str, exchange: str = 'binance') -> str:
    """Get the current open interest for a perpetual futures contract.

    Open interest is the total number of outstanding contracts not yet settled.
    Rising OI with rising price signals strong trend; divergence may indicate reversal.

    Args:
        symbol: Base asset or full pair, e.g. BTC or BTC/USDT.
        exchange: Exchange name. Default binance.

    Returns:
        JSON object with: symbol, exchange, datetime,
        open_interest_coins (in base asset), open_interest_usdt (notional value).
        Returns an error string on failure.
    """
    try:
        ex = _get_futures_exchange(exchange)
        symbol = symbol.upper()
        if '/' not in symbol:
            symbol = f"{symbol}/USDT"
        oi = ex.fetch_open_interest(symbol)
        return json.dumps({
            'symbol': symbol,
            'exchange': exchange,
            'datetime': oi.get('datetime'),
            'open_interest_coins': oi.get('openInterestAmount'),
            'open_interest_usdt': oi.get('openInterestValue'),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


# ─── COMPANY & FINANCIALS ────────────────────────────────────────────────────

@mcp.tool()
def get_company_overview(symbol: str) -> str:
    """Get company profile information for a Vietnam-listed stock.

    Returns industry sector, market cap, listing exchange, website, and business description.

    Args:
        symbol: Ticker symbol, e.g. VNM, FPT, VIC.

    Returns:
        JSON array of company profile records.
        Returns an error string on failure.
    """
    try:
        stock = _vn_stock(symbol)
        df = _quiet(stock.company.overview)
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_financials(symbol: str, statement: str = 'income_statement', period: str = 'year') -> str:
    """Get financial statements for a Vietnam-listed stock.

    Args:
        symbol: Ticker symbol, e.g. VNM, TCB.
        statement: Report type — one of:
            income_statement, balance_sheet, cash_flow, ratio.
            Default income_statement.
        period: Reporting period — year or quarter. Default year.
                (Ignored for 'ratio' which always returns the latest data.)

    Returns:
        JSON array of financial records for the requested statement.
        Returns an error string on failure.
    """
    try:
        stock = _vn_stock(symbol)
        fn_map = {
            'income_statement': lambda: stock.finance.income_statement(period=period),
            'balance_sheet': lambda: stock.finance.balance_sheet(period=period),
            'cash_flow': lambda: stock.finance.cash_flow(period=period),
            'ratio': lambda: stock.finance.ratio(lang='vi'),
        }
        if statement not in fn_map:
            return f"Invalid statement. Choose from: {', '.join(fn_map)}"
        df = _quiet(fn_map[statement])
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
        from vnstock.explorer.misc.gold_price import sjc_gold_price
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
        from vnstock.explorer.misc.exchange_rate import vcb_exchange_rate
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
