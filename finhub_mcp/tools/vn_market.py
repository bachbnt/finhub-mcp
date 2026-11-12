# Copyright (c) 2026 bachbnt. All rights reserved.

from finhub_mcp.providers import VN_BOARD_SOURCES, clamp, fallback_used, first_success, provider_order, vn_symbol
from finhub_mcp.responses import invalid, success
from finhub_mcp.tooling import register_tool
from finhub_mcp.tools.world import split_symbols
from finhub_mcp.vnboard import (
    breadth,
    fetch_board_for,
    fetch_group_symbols,
    foreign_totals,
    load_board,
    no_trades_warning,
    normalize_group,
    rows_of,
    screen,
)

MAX_SYMBOLS = 100
PRICE_UNIT = 'thousand VND'
SORT_COLUMNS = {
    'value': 'value_vnd',
    'volume': 'volume',
    'change_pct': 'change_pct',
    'price': 'price',
    'foreign_net': 'foreign_net_value_vnd',
}


def register(mcp) -> None:
    tool = register_tool(mcp)

    @tool
    def get_vn_stock_prices(symbols: list[str] | str, source: str = 'auto') -> str:
        """Get live prices for many Vietnam-listed stocks in one call (max 100).

        symbols: list or comma-separated string such as "FPT,VCB,HPG". Prices are in thousand VND. Before the
        first trade of the day the reference price is shown. Includes foreign net volume.
        """
        wanted = [vn_symbol(s) for s in split_symbols(symbols, MAX_SYMBOLS)]
        sources = provider_order(source, VN_BOARD_SOURCES, 'VN board source')

        def fetch(src: str):
            # The board endpoint errors out on unknown tickers, so only ask for listed ones.
            known = set(fetch_group_symbols('ALL', src))
            listed = tuple(sorted({s for s in wanted if s in known}))
            if not listed:
                raise ValueError(f"none of the requested symbols are listed: {', '.join(wanted)}")
            return fetch_board_for(listed, src)

        used, board = first_success(sources, fetch, 'no price board data')
        by_symbol = {row['symbol']: row for row in rows_of(board)}
        data = [by_symbol[s] for s in wanted if s in by_symbol]
        return success(
            data,
            source=used,
            fallback_used=fallback_used(used, sources),
            price_unit=PRICE_UNIT,
            missing=[s for s in wanted if s not in by_symbol] or None,
            warnings=no_trades_warning(board),
        )

    @tool
    def get_vn_top_movers(group: str = 'HOSE', by: str = 'change_pct', limit: int = 10,
                          min_value_bn: float = 1.0, source: str = 'auto') -> str:
        """Get the biggest movers on a Vietnam market: top gainers and losers, or top volume / traded value.

        group: HOSE, HNX, UPCOM, VN30, VN100 or ALL. by: "change_pct" (returns gainers and losers),
        "volume" or "value" (returns the most traded). min_value_bn drops illiquid stocks (billion VND
        traded) from the gainers/losers lists so thin trading does not dominate. Prices in thousand VND.
        """
        group = normalize_group(group)
        by = (by or '').strip().lower()
        if by not in {'change_pct', 'volume', 'value'}:
            return invalid("Invalid by. Choose from: change_pct, volume, value")
        limit = clamp(limit, 1, 50)
        sources = provider_order(source, VN_BOARD_SOURCES, 'VN board source')
        used, board = load_board(sources, group)
        traded = board[board['traded']]
        if by == 'change_pct':
            liquid = traded[traded['value_vnd'] >= float(min_value_bn) * 1e9]
            data = {
                'gainers': rows_of(liquid[liquid['change_pct'] > 0].nlargest(limit, 'change_pct')),
                'losers': rows_of(liquid[liquid['change_pct'] < 0].nsmallest(limit, 'change_pct')),
            }
        else:
            data = {'top': rows_of(traded.nlargest(limit, SORT_COLUMNS[by]))}
        return success(
            data,
            group=group,
            by=by,
            source=used,
            fallback_used=fallback_used(used, sources),
            price_unit=PRICE_UNIT,
            warnings=no_trades_warning(board),
        )

    @tool
    def get_vn_market_breadth(group: str = 'ALL', source: str = 'auto') -> str:
        """Get market breadth for Vietnam: advancers, decliners, unchanged, ceiling/floor counts, turnover.

        group: HOSE, HNX, UPCOM, VN30, VN100 or ALL (ALL also returns a per-exchange breakdown). Includes
        total foreign buying/selling (net value is an estimate: net volume x average price).
        """
        group = normalize_group(group)
        sources = provider_order(source, VN_BOARD_SOURCES, 'VN board source')
        used, board = load_board(sources, group)
        data = breadth(board)
        if group == 'ALL':
            data['by_exchange'] = {
                name: breadth(part)
                for name, part in board.groupby('exchange') if name in {'HOSE', 'HNX', 'UPCOM'}
            }
        return success(
            data,
            group=group,
            source=used,
            fallback_used=fallback_used(used, sources),
            warnings=no_trades_warning(board),
        )

    @tool
    def get_vn_foreign_trading(group: str = 'HOSE', limit: int = 10, source: str = 'auto') -> str:
        """Get foreign investor (khối ngoại) trading: top net buys and net sells, plus market totals.

        group: HOSE, HNX, UPCOM, VN30, VN100 or ALL. Ranked by estimated net value (net volume x average
        price); the price board only carries foreign volumes, so values are estimates.
        """
        group = normalize_group(group)
        limit = clamp(limit, 1, 50)
        sources = provider_order(source, VN_BOARD_SOURCES, 'VN board source')
        used, board = load_board(sources, group)
        active = board[(board['foreign_buy_volume'] > 0) | (board['foreign_sell_volume'] > 0)]
        data = {
            'totals': foreign_totals(board),
            'top_net_buy': _foreign_rows(active.nlargest(limit, 'foreign_net_value_vnd'), positive=True),
            'top_net_sell': _foreign_rows(active.nsmallest(limit, 'foreign_net_value_vnd'), positive=False),
        }
        return success(
            data,
            group=group,
            source=used,
            fallback_used=fallback_used(used, sources),
            price_unit=PRICE_UNIT,
            warnings=no_trades_warning(board),
        )

    @tool
    def screen_vn_stocks(
        group: str = 'HOSE',
        min_price: float | None = None,
        max_price: float | None = None,
        min_change_pct: float | None = None,
        max_change_pct: float | None = None,
        min_volume: float | None = None,
        min_value_bn: float | None = None,
        foreign_net: str = 'any',
        status: str = 'any',
        sort_by: str = 'value',
        descending: bool = True,
        limit: int = 20,
        source: str = 'auto',
    ) -> str:
        """Screen Vietnam stocks by today's trading data (price, change, volume, traded value, foreign flow).

        group: HOSE, HNX, UPCOM, VN30, VN100 or ALL. Prices in thousand VND; min_value_bn is billion VND traded.
        foreign_net: any, buy or sell (net foreign direction). status: any, ceiling or floor. sort_by: value,
        volume, change_pct, price or foreign_net. Only stocks that have traded today are considered.
        This screens on live trading data, not fundamentals (P/E, market cap).
        """
        group = normalize_group(group)
        foreign_net = (foreign_net or 'any').strip().lower()
        status = (status or 'any').strip().lower()
        sort_by = (sort_by or 'value').strip().lower()
        if foreign_net not in {'any', 'buy', 'sell'}:
            return invalid("Invalid foreign_net. Choose from: any, buy, sell")
        if status not in {'any', 'ceiling', 'floor'}:
            return invalid("Invalid status. Choose from: any, ceiling, floor")
        if sort_by not in SORT_COLUMNS:
            return invalid(f"Invalid sort_by. Choose from: {', '.join(SORT_COLUMNS)}")
        limit = clamp(limit, 1, 100)
        sources = provider_order(source, VN_BOARD_SOURCES, 'VN board source')
        used, board = load_board(sources, group)
        matched = screen(board, min_price, max_price, min_change_pct, max_change_pct,
                         min_volume, min_value_bn, foreign_net, status)
        ranked = matched.sort_values(SORT_COLUMNS[sort_by], ascending=not descending)
        return success(
            rows_of(ranked.head(limit)),
            group=group,
            matched=int(len(matched)),
            sort_by=sort_by,
            source=used,
            fallback_used=fallback_used(used, sources),
            price_unit=PRICE_UNIT,
            warnings=no_trades_warning(board),
        )


def _foreign_rows(df, positive: bool):
    rows = rows_of(df)
    for row, (_, source_row) in zip(rows, df.iterrows(), strict=True):
        row['foreign_buy_volume'] = int(source_row['foreign_buy_volume'])
        row['foreign_sell_volume'] = int(source_row['foreign_sell_volume'])
        row['foreign_net_value_bn_vnd_est'] = round(float(source_row['foreign_net_value_vnd']) / 1e9, 3)
    return [
        r for r in rows
        if r['foreign_net_value_bn_vnd_est'] != 0 and (r['foreign_net_value_bn_vnd_est'] > 0) == positive
    ]
