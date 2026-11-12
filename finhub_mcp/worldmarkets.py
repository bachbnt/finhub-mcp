# Copyright (c) 2026 bachbnt. All rights reserved.
"""International forex and precious-metal prices from free public endpoints.

Sources (all unauthenticated):
- Yahoo Finance chart API: forex pairs and metal futures, with history. Unofficial, may change.
- gold-api.com: spot XAU/XAG/XPT/XPD price.
- open.er-api.com: daily forex rates (free tier, attribution required).
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from finhub_mcp.cache import ttl_cache
from finhub_mcp.http import get_json
from finhub_mcp.providers import fallback_used, first_success

YAHOO_CHART_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/'
GOLD_API_URL = 'https://api.gold-api.com/price/'
ER_API_URL = 'https://open.er-api.com/v6/latest/'

FOREX_SOURCES = ['YAHOO', 'ER-API']
METAL_SOURCES = ['GOLD-API', 'YAHOO']

# metal code -> (name, Yahoo futures symbol)
METALS = {
    'XAU': ('Gold', 'GC=F'),
    'XAG': ('Silver', 'SI=F'),
    'XPT': ('Platinum', 'PL=F'),
    'XPD': ('Palladium', 'PA=F'),
}

HISTORY_INTERVALS = {'1m', '5m', '15m', '30m', '1h', '1d', '1wk', '1mo'}
# Yahoo only serves limited look-backs for intraday bars.
_MAX_DAYS = {'1m': 7, '5m': 59, '15m': 59, '30m': 59, '1h': 729}

TROY_OUNCE_GRAMS = 31.1034768
LUONG_GRAMS = 37.5  # 1 lượng (Vietnamese tael)

_PAIR_RE = re.compile(r'^([A-Z]{3})[/_\- ]?([A-Z]{3})$')


def parse_pair(pair: str) -> tuple[str, str]:
    """'eurusd', 'EUR/USD', 'eur-usd' -> ('EUR', 'USD')."""
    match = _PAIR_RE.match((pair or '').strip().upper())
    if not match or match.group(1) == match.group(2):
        raise ValueError(f"Invalid currency pair: {pair!r}. Use a pair such as EURUSD, USD/JPY or USDVND.")
    return match.group(1), match.group(2)


def parse_metal(metal: str) -> str:
    code = (metal or '').strip().upper()
    aliases = {'GOLD': 'XAU', 'SILVER': 'XAG', 'PLATINUM': 'XPT', 'PALLADIUM': 'XPD'}
    code = aliases.get(code, code)
    if code not in METALS:
        raise ValueError(f"Unsupported metal: {metal!r}. Choose from: {', '.join(METALS)}")
    return code


def _iso(ts: float | int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ------------------------------------------------------------------ Yahoo


def _chart_result(symbol: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = get_json(YAHOO_CHART_URL + symbol, params)
    chart = payload.get('chart') or {}
    if chart.get('error'):
        raise ValueError(f"Yahoo: {chart['error'].get('description') or chart['error']}")
    results = chart.get('result') or []
    if not results:
        raise ValueError(f"Yahoo returned no data for {symbol}")
    return results[0]


@ttl_cache('ticker')
def yahoo_quote(symbol: str) -> dict[str, Any]:
    """Latest price plus change versus the previous daily close."""
    result = _chart_result(symbol, {'range': '5d', 'interval': '1d'})
    meta = result.get('meta', {})
    price = meta.get('regularMarketPrice')
    if price is None:
        raise ValueError(f"Yahoo returned no price for {symbol}")
    closes = [c for c in (result.get('indicators', {}).get('quote') or [{}])[0].get('close', []) if c is not None]
    previous = closes[-2] if len(closes) >= 2 else None
    return {
        'price': price,
        'previous_close': previous,
        'as_of': _iso(meta.get('regularMarketTime')),
    }


@ttl_cache('history')
def yahoo_history(symbol: str, days: int, interval: str) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    result = _chart_result(symbol, {
        'period1': int(start.timestamp()),
        'period2': int(end.timestamp()),
        'interval': interval,
    })
    quote = (result.get('indicators', {}).get('quote') or [{}])[0]
    fmt = '%Y-%m-%d' if interval in {'1d', '1wk', '1mo'} else '%Y-%m-%d %H:%M'
    rows = []
    for i, ts in enumerate(result.get('timestamp') or []):
        close = (quote.get('close') or [None] * (i + 1))[i]
        if close is None:
            continue
        rows.append({
            'time': datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt),
            'open': quote['open'][i],
            'high': quote['high'][i],
            'low': quote['low'][i],
            'close': close,
            'volume': (quote.get('volume') or [None] * (i + 1))[i],
        })
    return rows


def history_days(days: int, interval: str) -> int:
    """Clamp a look-back to what Yahoo serves for the interval; raises on an unknown interval."""
    if interval not in HISTORY_INTERVALS:
        raise ValueError(f"Invalid interval. Choose from: {', '.join(sorted(HISTORY_INTERVALS))}")
    limit = _MAX_DAYS.get(interval, 3650)
    return max(1, min(int(days), limit))


# --------------------------------------------------------- other sources


@ttl_cache('market_overview')
def er_api_rates(base: str) -> dict[str, Any]:
    payload = get_json(ER_API_URL + base)
    if payload.get('result') != 'success':
        raise ValueError(f"ER-API error: {payload.get('error-type', 'unknown')}")
    return payload


@ttl_cache('ticker')
def gold_api_price(metal: str) -> dict[str, Any]:
    payload = get_json(GOLD_API_URL + metal)
    if payload.get('price') is None:
        raise ValueError(f"gold-api.com returned no price for {metal}")
    return payload


# ------------------------------------------------------------- composed


def _change(price: float, previous: float | None) -> dict[str, float | None]:
    if not previous:
        return {'change': None, 'change_pct': None}
    return {'change': price - previous, 'change_pct': (price / previous - 1) * 100}


def forex_rate(pair: str) -> dict[str, Any]:
    """Spot rate for a currency pair with source fallback (Yahoo, then ER-API)."""
    base, quote = parse_pair(pair)

    def fetch(source: str) -> dict[str, Any]:
        if source == 'YAHOO':
            q = yahoo_quote(f'{base}{quote}=X')
            return {'rate': q['price'], 'previous_close': q['previous_close'], 'as_of': q['as_of']}
        payload = er_api_rates(base)
        rate = payload['rates'].get(quote)
        if rate is None:
            raise ValueError(f"ER-API has no rate for {base}/{quote}")
        return {'rate': rate, 'previous_close': None, 'as_of': _iso(payload.get('time_last_update_unix'))}

    used, data = first_success(FOREX_SOURCES, fetch)
    return {
        'pair': f'{base}/{quote}',
        'rate': data['rate'],
        **_change(data['rate'], data['previous_close']),
        'as_of': data['as_of'],
        'source': used,
        'fallback_used': fallback_used(used, FOREX_SOURCES),
    }


def metal_price(metal: str) -> dict[str, Any]:
    """USD price per troy ounce, with source fallback (gold-api.com spot, then Yahoo futures)."""
    code = parse_metal(metal)
    name, futures_symbol = METALS[code]

    def fetch(source: str) -> dict[str, Any]:
        if source == 'GOLD-API':
            payload = gold_api_price(code)
            return {'price': float(payload['price']), 'previous_close': None, 'as_of': payload.get('updatedAt'),
                    'basis': 'spot'}
        q = yahoo_quote(futures_symbol)
        return {'price': q['price'], 'previous_close': q['previous_close'], 'as_of': q['as_of'],
                'basis': f'futures ({futures_symbol})'}

    used, data = first_success(METAL_SOURCES, fetch)
    return {
        'metal': code,
        'name': name,
        'currency': 'USD',
        'unit': 'troy ounce',
        'price': data['price'],
        **_change(data['price'], data['previous_close']),
        'basis': data['basis'],
        'as_of': data['as_of'],
        'source': used,
        'fallback_used': fallback_used(used, METAL_SOURCES),
    }


def usd_per_oz_to_vnd_per_luong(usd_per_oz: float, usd_vnd: float) -> float:
    """Convert a USD/oz gold price to VND per lượng (37.5 g), before tax, premium and fees."""
    return usd_per_oz * usd_vnd * LUONG_GRAMS / TROY_OUNCE_GRAMS
