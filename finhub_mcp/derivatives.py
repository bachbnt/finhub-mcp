# Copyright (c) 2026 bachbnt. All rights reserved.
"""Crypto derivatives and sentiment data: long/short ratio, liquidations, Fear & Greed, spot-futures basis."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from finhub_mcp.cache import ttl_cache
from finhub_mcp.http import get_json
from finhub_mcp.providers import SUPPORTED_EXCHANGES, get_futures_exchange
from finhub_mcp.services import fetch_crypto_ticker

# Exchanges (in preference order) whose public API exposes each dataset.
LONG_SHORT_EXCHANGES = tuple(e for e in ('binance', 'okx', 'bybit') if e in SUPPORTED_EXCHANGES)
LIQUIDATION_EXCHANGES = tuple(e for e in ('okx', 'gate') if e in SUPPORTED_EXCHANGES)

FEAR_GREED_URL = 'https://api.alternative.me/fng/'
OKX_LIQUIDATIONS_URL = 'https://www.okx.com/api/v5/public/liquidation-orders'
MAX_DATED_FUTURES = 4


def split_pair(symbol: str) -> tuple[str, str]:
    """'BTC/USDT' -> ('BTC', 'USDT')."""
    base, _, quote = symbol.partition('/')
    return base, quote


def perp_symbol(symbol: str) -> str:
    """Linear perpetual swap symbol for a spot pair: BTC/USDT -> BTC/USDT:USDT."""
    _, quote = split_pair(symbol)
    return f'{symbol}:{quote}'


def _utc(ts_ms: float | int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


# -------------------------------------------------------- long/short ratio


@ttl_cache('history')
def fetch_long_short_ratio(exchange: str, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
    """Account long/short ratio history, oldest first: [{time, ratio, long_pct, short_pct}]."""
    ex = get_futures_exchange(exchange)
    raw = ex.fetch_long_short_ratio_history(perp_symbol(symbol), timeframe, None, limit)
    rows = []
    for item in raw:
        ratio = item.get('longShortRatio')
        if ratio is None:
            continue
        ratio = float(ratio)
        long_share = ratio / (1 + ratio)
        rows.append({
            'time': _utc(item.get('timestamp')),
            'ratio': round(ratio, 4),
            'long_pct': round(long_share * 100, 2),
            'short_pct': round((1 - long_share) * 100, 2),
        })
    rows.sort(key=lambda row: row['time'] or '')
    return rows


def summarize_long_short(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latest, first = rows[-1], rows[0]
    return {
        'latest_ratio': latest['ratio'],
        'latest_long_pct': latest['long_pct'],
        'average_ratio': round(sum(r['ratio'] for r in rows) / len(rows), 4),
        'change_in_ratio': round(latest['ratio'] - first['ratio'], 4),
        'crowding': (
            'long-heavy' if latest['ratio'] >= 1.5 else 'short-heavy' if latest['ratio'] <= 0.67 else 'balanced'
        ),
    }


# ------------------------------------------------------------ liquidations


def _gate_liquidations(symbol: str, limit: int) -> list[dict[str, Any]]:
    ex = get_futures_exchange('gate')
    rows = []
    for item in ex.fetch_liquidations(perp_symbol(symbol), limit=limit):
        order_size = float((item.get('info') or {}).get('order_size') or 0)
        rows.append({
            'time': _utc(item.get('timestamp')),
            # A forced sell (negative order size) closes a long position.
            'liquidated_side': 'long' if order_size < 0 else 'short',
            'price': item.get('price'),
            'amount_base': item.get('baseValue'),
            'value_usdt': round(item.get('quoteValue') or 0, 2),
        })
    return rows


def _okx_liquidations(symbol: str, limit: int) -> list[dict[str, Any]]:
    base, quote = split_pair(symbol)
    payload = get_json(OKX_LIQUIDATIONS_URL, {'instType': 'SWAP', 'uly': f'{base}-{quote}', 'state': 'filled'})
    if payload.get('code') != '0':
        raise ValueError(f"OKX error: {payload.get('msg') or payload.get('code')}")
    futures = get_futures_exchange('okx')
    futures.load_markets()
    contract_size = float(futures.market(perp_symbol(symbol)).get('contractSize') or 1)
    rows = []
    for entry in payload.get('data', []):
        for detail in entry.get('details', []):
            side = detail.get('posSide')
            if side not in {'long', 'short'}:  # net mode: a forced sell closes a long
                side = 'long' if detail.get('side') == 'sell' else 'short'
            amount = float(detail['sz']) * contract_size
            price = float(detail['bkPx'])
            rows.append({
                'time': _utc(int(detail['ts'])),
                'liquidated_side': side,
                'price': price,
                'amount_base': amount,
                'value_usdt': round(amount * price, 2),
            })
    rows.sort(key=lambda row: row['time'] or '', reverse=True)
    return rows[:limit]


@ttl_cache('ticker')
def fetch_liquidations(exchange: str, symbol: str, limit: int) -> list[dict[str, Any]]:
    """Most recent forced liquidations on a perpetual swap, newest first."""
    if exchange == 'gate':
        return _gate_liquidations(symbol, limit)
    if exchange == 'okx':
        return _okx_liquidations(symbol, limit)
    raise ValueError(f"{exchange} does not expose public liquidation data over REST")


def summarize_liquidations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    long_value = sum(r['value_usdt'] for r in rows if r['liquidated_side'] == 'long')
    short_value = sum(r['value_usdt'] for r in rows if r['liquidated_side'] == 'short')
    return {
        'count': len(rows),
        'long_liquidated_usdt': round(long_value, 2),
        'short_liquidated_usdt': round(short_value, 2),
        'dominant_side': 'long' if long_value > short_value else 'short' if short_value > long_value else None,
        'window_from': rows[-1]['time'],
        'window_to': rows[0]['time'],
    }


# ------------------------------------------------------------- fear & greed


@ttl_cache('history')
def fetch_fear_greed(days: int) -> list[dict[str, Any]]:
    """Crypto Fear & Greed Index (alternative.me), newest first."""
    payload = get_json(FEAR_GREED_URL, {'limit': days})
    rows = []
    for item in payload.get('data', []):
        rows.append({
            'date': datetime.fromtimestamp(int(item['timestamp']), tz=timezone.utc).strftime('%Y-%m-%d'),
            'value': int(item['value']),
            'classification': item.get('value_classification'),
        })
    if not rows:
        raise ValueError("alternative.me returned no Fear & Greed data")
    return rows


# -------------------------------------------------------------------- basis


@ttl_cache('ticker')
def fetch_futures_ticker(exchange: str, symbol: str) -> dict[str, Any]:
    return get_futures_exchange(exchange).fetch_ticker(symbol)


def select_contracts(markets: list[dict[str, Any]], base: str, quote: str) -> dict[str, Any]:
    """Pick the perpetual and the nearest dated futures for base/quote from ccxt market dicts.

    Linear (stablecoin-margined) contracts are preferred; coin-margined ones are used only when
    no linear equivalent exists (e.g. OKX dated futures).
    """
    stable = {'USDT', 'USDC', 'USD'}
    candidates = [
        m for m in markets
        if m.get('contract') and m.get('active') is not False and m.get('base') == base
        and (m.get('quote') == quote or (m.get('quote') == 'USD' and quote in stable))
    ]
    perps = [m for m in candidates if m.get('swap')]
    linear_perps = [m for m in perps if m.get('linear')]
    dated = [m for m in candidates if m.get('future') and m.get('expiry')]
    linear_dated = [m for m in dated if m.get('linear')]
    chosen_dated = sorted(linear_dated or dated, key=lambda m: m['expiry'])[:MAX_DATED_FUTURES]
    perp = (linear_perps or perps or [None])[0]
    return {'perpetual': perp, 'dated': chosen_dated}


def reference_price(ticker: dict[str, Any]) -> tuple[float | None, str, float | None]:
    """(price, which, spread_pct): the bid/ask midpoint when quoted, else the last trade.

    Illiquid dated futures often carry a stale last price, so the midpoint is the safer reference.
    """
    bid, ask, last = ticker.get('bid'), ticker.get('ask'), ticker.get('last')
    if bid and ask and ask >= bid:
        mid = (bid + ask) / 2
        return mid, 'mid', round((ask - bid) / mid * 100, 3)
    return last, 'last', None


def compute_basis(spot: float, price: float, expiry_ms: int | None, now_ms: int) -> dict[str, float | None]:
    basis = price - spot
    pct = basis / spot * 100
    annualized = None
    if expiry_ms:
        days = (expiry_ms - now_ms) / 86_400_000
        if days > 0:
            annualized = pct * 365 / days
    return {
        'basis': round(basis, 4),
        'basis_pct': round(pct, 4),
        'annualized_pct': round(annualized, 2) if annualized is not None else None,
    }


@ttl_cache('ticker')
def fetch_basis(exchange: str, symbol: str) -> dict[str, Any]:
    """Spot price versus the perpetual and nearest dated futures on one exchange."""
    base, quote = split_pair(symbol)
    futures = get_futures_exchange(exchange)
    futures.load_markets()
    chosen = select_contracts(list(futures.markets.values()), base, quote)
    contracts = ([chosen['perpetual']] if chosen['perpetual'] else []) + chosen['dated']
    if not contracts:
        raise ValueError(f"{exchange} lists no {base} futures against {quote}")

    spot = fetch_crypto_ticker(exchange, symbol).get('last')
    if not spot:
        raise ValueError(f"no spot price for {symbol} on {exchange}")

    def load(market: dict[str, Any]):
        try:
            return market, reference_price(fetch_futures_ticker(exchange, market['symbol'])), None
        except Exception as e:
            return market, (None, 'last', None), str(e)

    with ThreadPoolExecutor(max_workers=len(contracts)) as pool:
        loaded = list(pool.map(load, contracts))

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows, warnings = [], {}
    for market, (price, price_kind, spread_pct), err in loaded:
        if not price:
            warnings[market['symbol']] = err or 'no price'
            continue
        expiry = market.get('expiry')
        rows.append({
            'contract': market['symbol'],
            'type': 'dated' if market.get('future') else 'perpetual',
            'margin': 'linear' if market.get('linear') else 'inverse',
            'expiry': _utc(expiry)[:10] if expiry else None,
            'days_to_expiry': round((expiry - now_ms) / 86_400_000, 1) if expiry else None,
            'price': round(price, 6),
            'price_kind': price_kind,
            'spread_pct': spread_pct,
            **compute_basis(spot, price, expiry, now_ms),
        })
    if not rows:
        raise ValueError(f"no futures prices available ({warnings})")
    return {'symbol': symbol, 'exchange': exchange, 'spot_price': spot, 'contracts': rows, 'warnings': warnings}
