# Copyright (c) 2026 bachbnt. All rights reserved.
"""Vietnam price-board data (KBS) used for movers, breadth, foreign trading and screening.

Prices in the returned frames are in thousand VND (the same unit as get_vn_stock_price); values are VND.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import pandas as pd

from finhub_mcp.cache import ttl_cache
from finhub_mcp.providers import first_success, quiet, vn_listing, vn_trading

GROUPS = ('HOSE', 'HNX', 'UPCOM', 'VN30', 'VN100', 'ALL')
EXCHANGE_GROUPS = ('HOSE', 'HNX', 'UPCOM')
BOARD_CHUNK = 400

_NUMERIC = (
    'reference_price', 'ceiling_price', 'floor_price', 'open_price', 'high_price', 'low_price',
    'close_price', 'average_price', 'volume_accumulated', 'total_value',
    'foreign_buy_volume', 'foreign_sell_volume',
)


def normalize_group(group: str) -> str:
    value = (group or '').strip().upper()
    if value not in GROUPS:
        raise ValueError(f"Invalid group: {group!r}. Choose from: {', '.join(GROUPS)}")
    return value


@ttl_cache('history')
def fetch_group_symbols(group: str, source: str) -> tuple[str, ...]:
    listing = vn_listing(source)
    names = EXCHANGE_GROUPS if group == 'ALL' else (group,)
    symbols: set[str] = set()
    for name in names:
        symbols.update(str(s).upper() for s in quiet(listing.symbols_by_group, name))
    if not symbols:
        raise ValueError(f"no symbols found for group {group}")
    return tuple(sorted(symbols))


def _fetch_raw(symbols: tuple[str, ...], source: str) -> pd.DataFrame:
    trading = vn_trading(source)
    chunks = [list(symbols[i:i + BOARD_CHUNK]) for i in range(0, len(symbols), BOARD_CHUNK)]
    with ThreadPoolExecutor(max_workers=min(len(chunks), 4)) as pool:
        frames = list(pool.map(lambda chunk: quiet(trading.price_board, chunk), chunks))
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        raise ValueError("price board returned no rows")
    return pd.concat(frames, ignore_index=True)


def normalize_board(raw: pd.DataFrame) -> pd.DataFrame:
    """Raw KBS board -> tidy frame with prices in thousand VND and derived change/foreign columns."""
    num = {
        column: pd.to_numeric(raw[column], errors='coerce').fillna(0.0).to_numpy(dtype=float)
        if column in raw else np.zeros(len(raw))
        for column in _NUMERIC
    }
    ref = num['reference_price'] / 1000
    close = num['close_price']
    traded = (num['volume_accumulated'] > 0) & (close > 0)
    price = np.where(traded, close / 1000, ref)
    change = np.where(ref > 0, price - ref, 0.0)
    change_pct = np.where(ref > 0, change / np.where(ref > 0, ref, 1) * 100, 0.0)
    avg_vnd = np.where(num['average_price'] > 0, num['average_price'], price * 1000)
    net_vol = num['foreign_buy_volume'] - num['foreign_sell_volume']
    at_ceiling = traded & (num['ceiling_price'] > 0) & (close >= num['ceiling_price'])
    at_floor = traded & (num['floor_price'] > 0) & (close <= num['floor_price'])
    status = np.select([at_ceiling, at_floor], ['ceiling', 'floor'], default='')
    return pd.DataFrame({
        'symbol': raw['symbol'].astype(str).str.upper().to_numpy(),
        'exchange': raw['exchange'].to_numpy() if 'exchange' in raw else '',
        'reference': ref,
        'open': num['open_price'] / 1000,
        'high': num['high_price'] / 1000,
        'low': num['low_price'] / 1000,
        'price': price,
        'change': change,
        'change_pct': change_pct,
        'volume': num['volume_accumulated'],
        'value_vnd': num['total_value'],
        'traded': traded,
        'status': status,
        'foreign_buy_volume': num['foreign_buy_volume'],
        'foreign_sell_volume': num['foreign_sell_volume'],
        'foreign_net_volume': net_vol,
        'foreign_net_value_vnd': net_vol * avg_vnd,
    })


@ttl_cache('ticker')
def fetch_board(group: str, source: str) -> pd.DataFrame:
    return normalize_board(_fetch_raw(fetch_group_symbols(group, source), source))


@ttl_cache('ticker')
def fetch_board_for(symbols: tuple[str, ...], source: str) -> pd.DataFrame:
    return normalize_board(_fetch_raw(symbols, source))


def load_board(sources: list[str], group: str) -> tuple[str, pd.DataFrame]:
    return first_success(sources, lambda src: fetch_board(group, src), f"no price board for {group}")


# --------------------------------------------------------------- analysis


def board_row(row: pd.Series) -> dict[str, Any]:
    return {
        'symbol': row['symbol'],
        'exchange': row['exchange'],
        'price': round(float(row['price']), 2),
        'reference': round(float(row['reference']), 2),
        'change': round(float(row['change']), 2),
        'change_pct': round(float(row['change_pct']), 2),
        'volume': int(row['volume']),
        'value_bn_vnd': round(float(row['value_vnd']) / 1e9, 2),
        'foreign_net_volume': int(row['foreign_net_volume']),
        'status': row['status'] or None,
    }


def rows_of(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [board_row(row) for _, row in df.iterrows()]


def foreign_totals(df: pd.DataFrame) -> dict[str, Any]:
    return {
        'buy_volume': int(df['foreign_buy_volume'].sum()),
        'sell_volume': int(df['foreign_sell_volume'].sum()),
        'net_volume': int(df['foreign_net_volume'].sum()),
        'net_value_bn_vnd_est': round(float(df['foreign_net_value_vnd'].sum()) / 1e9, 2),
    }


def breadth(df: pd.DataFrame) -> dict[str, Any]:
    """Advancers/decliners/unchanged, ceiling/floor counts, turnover and foreign totals."""
    traded = df[df['traded']]
    advancers = int((traded['change'] > 0).sum())
    decliners = int((traded['change'] < 0).sum())
    return {
        'symbols': int(len(df)),
        'traded': int(len(traded)),
        'advancers': advancers,
        'decliners': decliners,
        'unchanged': int(len(traded) - advancers - decliners),
        'not_traded': int(len(df) - len(traded)),
        'ceiling': int((traded['status'] == 'ceiling').sum()),
        'floor': int((traded['status'] == 'floor').sum()),
        'advance_decline_ratio': round(advancers / decliners, 2) if decliners else None,
        'volume': int(traded['volume'].sum()),
        'value_bn_vnd': round(float(traded['value_vnd'].sum()) / 1e9, 2),
        'foreign': foreign_totals(df),
    }


def no_trades_warning(df: pd.DataFrame) -> str | None:
    if bool(df['traded'].any()):
        return None
    return 'No trades yet: the market is closed or in the pre-open session, so prices show reference prices.'


def screen(
    df: pd.DataFrame,
    min_price: float | None = None,
    max_price: float | None = None,
    min_change_pct: float | None = None,
    max_change_pct: float | None = None,
    min_volume: float | None = None,
    min_value_bn: float | None = None,
    foreign_net: str = 'any',
    status: str = 'any',
) -> pd.DataFrame:
    """Filter a normalized board. Only symbols that have traded today are considered."""
    mask = df['traded'].copy()  # never modify the (cached, shared) board in place
    if min_price is not None:
        mask &= df['price'] >= min_price
    if max_price is not None:
        mask &= df['price'] <= max_price
    if min_change_pct is not None:
        mask &= df['change_pct'] >= min_change_pct
    if max_change_pct is not None:
        mask &= df['change_pct'] <= max_change_pct
    if min_volume is not None:
        mask &= df['volume'] >= min_volume
    if min_value_bn is not None:
        mask &= df['value_vnd'] >= min_value_bn * 1e9
    if foreign_net == 'buy':
        mask &= df['foreign_net_volume'] > 0
    elif foreign_net == 'sell':
        mask &= df['foreign_net_volume'] < 0
    if status in {'ceiling', 'floor'}:
        mask &= df['status'] == status
    return df[mask]
