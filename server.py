# Them vao ~/.claude/settings.json:
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
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("finance-mcp")

ALERTS_FILE = os.path.join(os.path.dirname(__file__), 'alerts.json')


def _quiet(fn, *args, **kwargs):
    """Goi fn(*args, **kwargs) nhung suppress stdout cua vnstock (banner/ads)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*args, **kwargs)


def _load_alerts():
    if os.path.exists(ALERTS_FILE):
        with open(ALERTS_FILE) as f:
            return json.load(f)
    return []


def _save_alerts(alerts):
    with open(ALERTS_FILE, 'w') as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)


def _vn_quote(symbol: str):
    from vnstock.api.quote import Quote
    return Quote(symbol=symbol.upper(), source='VCI')


def _vn_stock(symbol: str, source: str = 'VCI'):
    from vnstock import Vnstock
    return Vnstock().stock(symbol=symbol.upper(), source=source)


_SUPPORTED_EXCHANGES = {'binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc'}


def _get_exchange(name: str):
    import ccxt
    if name.lower() not in _SUPPORTED_EXCHANGES:
        raise ValueError(f"Exchange khong ho tro. Chon: {', '.join(sorted(_SUPPORTED_EXCHANGES))}")
    return getattr(ccxt, name.lower())({'enableRateLimit': True})


# ─── VN STOCK ────────────────────────────────────────────────────────────────

@mcp.tool()
def get_vn_stock_price(symbol: str) -> str:
    """Gia co phieu Viet Nam hien tai. Vi du: VNM, TCB, VIC, HPG, FPT"""
    try:
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        df = _quiet(_vn_quote(symbol).history, start=start, end=end, interval='1D')
        if df.empty:
            return f"Khong tim thay du lieu cho {symbol.upper()}"
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
        return f"Loi: {e}"


@mcp.tool()
def get_vn_stock_history(symbol: str, days: int = 30) -> str:
    """Lich su gia OHLCV co phieu Viet Nam. days: so ngay lay lui (mac dinh 30, toi da 365)"""
    try:
        days = min(max(days, 1), 365)
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        df = _quiet(_vn_quote(symbol).history, start=start, end=end, interval='1D')
        if df.empty:
            return f"Khong co du lieu cho {symbol.upper()}"
        df['date'] = df['time'].dt.strftime('%Y-%m-%d')
        result = df[['date', 'open', 'high', 'low', 'close', 'volume']].to_dict(orient='records')
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Loi: {e}"


@mcp.tool()
def get_vn_market_overview() -> str:
    """Tong quan thi truong: VNINDEX, VN30, HNX-INDEX"""
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
    """Tim kiem ma co phieu Viet Nam theo ten cong ty hoac ma chung khoan"""
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
        return f"Loi: {e}"


# ─── CRYPTO ──────────────────────────────────────────────────────────────────

@mcp.tool()
def get_crypto_price(symbol: str, exchange: str = 'binance') -> str:
    """Gia crypto hien tai. symbol: BTC, ETH, SOL hoac BTC/USDT"""
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
            'volume_24h': t['baseVolume'],
            'change_pct_24h': round(t.get('percentage') or 0, 2),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Loi: {e}"


@mcp.tool()
def get_crypto_history(symbol: str, timeframe: str = '1d', limit: int = 30, exchange: str = 'binance') -> str:
    """Lich su OHLCV crypto. timeframe: 1m, 5m, 15m, 1h, 4h, 1d, 1w. limit: so nen"""
    try:
        ex = _get_exchange(exchange)
        symbol = symbol.upper()
        if '/' not in symbol:
            symbol = f"{symbol}/USDT"
        ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        result = [{
            'time': datetime.utcfromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M'),
            'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
        } for ts, o, h, l, c, v in ohlcv]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Loi: {e}"


@mcp.tool()
def get_top_crypto(limit: int = 10, exchange: str = 'binance') -> str:
    """Top crypto theo volume USDT 24h"""
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
        return f"Loi: {e}"


# ─── COMPANY & FINANCIALS ────────────────────────────────────────────────────

@mcp.tool()
def get_company_overview(symbol: str) -> str:
    """Thong tin cong ty: nganh nghe, von hoa, san niem yet, website, mo ta"""
    try:
        stock = _vn_stock(symbol)
        df = _quiet(stock.company.overview)
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Loi: {e}"


@mcp.tool()
def get_financials(symbol: str, statement: str = 'income_statement', period: str = 'year') -> str:
    """
    Bao cao tai chinh.
    - statement: income_statement | balance_sheet | cash_flow | ratio
    - period: year | quarter
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
            return f"statement phai la: {', '.join(fn_map)}"
        df = _quiet(fn_map[statement])
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Loi: {e}"


# ─── MARKET EXTRAS ───────────────────────────────────────────────────────────

@mcp.tool()
def get_gold_price() -> str:
    """Gia vang SJC hien tai (mua vao / ban ra)"""
    try:
        from vnstock.explorer.misc.gold_price import sjc_gold_price
        df = _quiet(sjc_gold_price)
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Loi: {e}"


@mcp.tool()
def get_forex() -> str:
    """Ty gia ngoai te (USD, EUR, JPY, CNY, ...) tu Vietcombank"""
    try:
        from vnstock.explorer.misc.exchange_rate import vcb_exchange_rate
        today = datetime.now().strftime('%Y-%m-%d')
        df = _quiet(vcb_exchange_rate, today)
        return df.to_json(orient='records', force_ascii=False, indent=2)
    except Exception as e:
        return f"Loi: {e}"


# ─── ALERTS ──────────────────────────────────────────────────────────────────

@mcp.tool()
def add_alert(
    symbol: str,
    condition: str,
    price: float,
    asset_type: str = 'crypto',
    telegram_chat_id: str = '',
) -> str:
    """
    Them price alert.
    - asset_type: 'crypto' hoac 'vn_stock'
    - condition: 'above' (gia tang den muc) hoac 'below' (gia giam xuong muc)
    - telegram_chat_id: lay bang cach gui /start cho Telegram bot
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
    return f"Da them alert [{alert['id']}]: {alert['symbol']} {condition} {price}"


@mcp.tool()
def list_alerts() -> str:
    """Xem danh sach tat ca price alerts"""
    alerts = _load_alerts()
    if not alerts:
        return "Chua co alert nao."
    return json.dumps(alerts, ensure_ascii=False, indent=2)


@mcp.tool()
def remove_alert(alert_id: str) -> str:
    """Xoa alert theo ID"""
    alerts = _load_alerts()
    filtered = [a for a in alerts if a['id'] != alert_id]
    if len(filtered) == len(alerts):
        return f"Khong tim thay alert ID: {alert_id}"
    _save_alerts(filtered)
    return f"Da xoa alert {alert_id}"


if __name__ == '__main__':
    mcp.run()
