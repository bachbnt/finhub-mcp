# Copyright (c) 2026 bachbnt. All rights reserved.

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from finhub_mcp.indicators import calculate_indicators, parse_indicators
from finhub_mcp.providers import (
    VN_INTRADAY_SOURCES,
    VN_LISTING_SOURCES,
    VN_QUOTE_SOURCES,
    VN_STOCK_SOURCES,
    clamp,
    fallback_used,
    first_success,
    provider_order,
    quiet,
    vn_quote,
    vn_symbol,
)
from finhub_mcp.responses import error, invalid, success
from finhub_mcp.services import (
    date_range,
    fetch_company_overview,
    fetch_financials,
    fetch_vn_history,
    fetch_vn_latest,
    fetch_vn_listing,
    frame_records,
    price_snapshot,
    vn_index_snapshot,
)
from finhub_mcp.tooling import register_tool

MARKET_INDICES = ('VNINDEX', 'VN30', 'HNXINDEX')
FINANCIAL_STATEMENTS = {'income_statement', 'balance_sheet', 'cash_flow', 'ratio'}


def register(mcp) -> None:
    tool = register_tool(mcp)

    @tool
    def get_vn_stock_price(symbol: str, source: str = 'auto') -> str:
        """Get the latest price snapshot for a Vietnam-listed stock."""
        symbol = vn_symbol(symbol)
        sources = provider_order(source, VN_QUOTE_SOURCES, 'VN quote source')
        start, end = date_range(10)
        used_source, df = first_success(
            sources,
            lambda src: fetch_vn_latest(symbol, src, start, end),
            f"no data found for {symbol}",
        )
        return success({
            'symbol': symbol,
            **price_snapshot(df),
            'source': used_source,
            'fallback_used': fallback_used(used_source, sources),
        })

    @tool
    def get_vn_stock_history(symbol: str, days: int = 30, source: str = 'auto') -> str:
        """Get historical daily OHLCV data for a Vietnam-listed stock."""
        symbol = vn_symbol(symbol)
        sources = provider_order(source, VN_QUOTE_SOURCES, 'VN quote source')
        days = clamp(days, 1, 365)
        start, end = date_range(days)
        used_source, df = first_success(
            sources,
            lambda src: fetch_vn_history(symbol, src, start, end),
            f"no data found for {symbol}",
        )
        df = df.assign(date=df['time'].dt.strftime('%Y-%m-%d'))
        data = df[['date', 'open', 'high', 'low', 'close', 'volume']].to_dict(orient='records')
        return success(data, symbol=symbol, source=used_source, fallback_used=fallback_used(used_source, sources))

    @tool
    def get_vn_stock_intraday(
        symbol: str,
        period: str = 'latest',
        limit: int = 100,
        page_size: int = 100,
        pages: int = 5,
        source: str = 'auto',
    ) -> str:
        """Get intraday matched trades for a Vietnam-listed stock."""
        symbol = vn_symbol(symbol)
        sources = provider_order(source, VN_INTRADAY_SOURCES, 'VN intraday source')
        period = period.lower()
        if period not in {'latest', 'today'}:
            return invalid("Invalid period. Choose from: latest, today")
        limit = clamp(limit, 1, 1000)
        page_size = clamp(page_size, 10, 1000)
        pages = clamp(pages, 1, 20)

        def fetch(src: str):
            records = []
            quote = vn_quote(symbol, src)
            for page in range(1, pages + 1):
                df = quiet(quote.intraday, page_size=page_size, page=page)
                if getattr(df, 'empty', True):
                    break
                records.extend(df.to_dict(orient='records'))
            return records

        used_source, records = first_success(
            sources,
            fetch,
            f"no intraday data found for {symbol}",
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
                'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
                'price': float(row['price']) if row.get('price') is not None else None,
                'volume': int(row['volume']) if row.get('volume') is not None else None,
                'match_type': row.get('match_type'),
                'id': row.get('id'),
            })

        normalized.sort(key=lambda item: item['time'], reverse=True)
        return success(
            normalized[:limit],
            symbol=symbol,
            source=used_source,
            fallback_used=fallback_used(used_source, sources),
        )

    @tool
    def get_vn_market_overview(source: str = 'auto') -> str:
        """Get a snapshot of the main Vietnam market indices."""
        sources = tuple(provider_order(source, VN_QUOTE_SOURCES, 'VN quote source'))

        def snapshot(idx: str):
            try:
                return idx, vn_index_snapshot(idx, sources), None
            except Exception as e:
                return idx, None, str(e)

        with ThreadPoolExecutor(max_workers=len(MARKET_INDICES)) as pool:
            outcomes = list(pool.map(snapshot, MARKET_INDICES))
        results = {idx: data for idx, data, err in outcomes if err is None}
        errors = {idx: err for idx, data, err in outcomes if err is not None}
        if not results:
            details = '; '.join(f'{idx}: {err}' for idx, err in errors.items())
            return error(f"all market indices failed ({details})")
        return success(results, warnings=errors or None)

    @tool
    def search_vn_stock(query: str, source: str = 'auto') -> str:
        """Search for Vietnam stock tickers by symbol or company name."""
        q = (query or '').strip().lower()
        if not q:
            return invalid("Query must not be empty")
        sources = provider_order(source, VN_LISTING_SOURCES, 'VN listing source')
        used_source, listing = first_success(sources, fetch_vn_listing, 'no symbols found')
        mask = listing['symbol'].str.lower().str.contains(q, regex=False, na=False)
        if 'organ_name' in listing.columns:
            mask |= listing['organ_name'].str.lower().str.contains(q, regex=False, na=False)
        matches = listing[mask].head(20)
        return success(
            frame_records(matches),
            source=used_source,
            fallback_used=fallback_used(used_source, sources),
        )

    @tool
    def get_company_overview(symbol: str, source: str = 'auto') -> str:
        """Get company profile information for a Vietnam-listed stock."""
        symbol = vn_symbol(symbol)
        sources = provider_order(source, VN_STOCK_SOURCES, 'VN stock source')
        used_source, df = first_success(
            sources,
            lambda src: fetch_company_overview(symbol, src),
            f"no company overview found for {symbol}",
        )
        return success(
            frame_records(df),
            symbol=symbol,
            source=used_source,
            fallback_used=fallback_used(used_source, sources),
        )

    @tool
    def get_financials(
        symbol: str,
        statement: str = 'income_statement',
        period: str = 'year',
        source: str = 'auto',
    ) -> str:
        """Get financial statements for a Vietnam-listed stock."""
        symbol = vn_symbol(symbol)
        if statement not in FINANCIAL_STATEMENTS:
            return invalid(f"Invalid statement. Choose from: {', '.join(sorted(FINANCIAL_STATEMENTS))}")
        sources = provider_order(source, VN_STOCK_SOURCES, 'VN stock source')
        used_source, df = first_success(
            sources,
            lambda src: fetch_financials(symbol, src, statement, period),
            f"no financials found for {symbol}",
        )
        return success(
            frame_records(df),
            symbol=symbol,
            statement=statement,
            source=used_source,
            fallback_used=fallback_used(used_source, sources),
        )

    @tool
    def get_vn_stock_indicators(
        symbol: str,
        days: int = 180,
        indicators: str = 'sma,ema,rsi,macd,bollinger',
        source: str = 'auto',
    ) -> str:
        """Calculate technical indicators from Vietnam stock daily history.

        indicators: comma-separated from sma, ema, rsi, macd, bollinger (default set), atr, stochastic,
        vwap, volume_ma, support_resistance, signals (MA/MACD crosses and bias), or "all".
        signals' golden/death cross needs ~200 sessions, so use days=365. vwap covers the fetched window.
        """
        symbol = vn_symbol(symbol)
        requested = parse_indicators(indicators)
        sources = provider_order(source, VN_QUOTE_SOURCES, 'VN quote source')
        # ~90 calendar days is ~60 trading sessions: enough for SMA50 and MACD with its signal line.
        days = clamp(days, 90, 365)
        start, end = date_range(days)
        used_source, df = first_success(
            sources,
            lambda src: fetch_vn_history(symbol, src, start, end),
            f"no data found for {symbol}",
        )
        df = df.dropna(subset=['high', 'low', 'close'])
        closes = [float(value) for value in df['close'].tolist()]
        return success({
            'symbol': symbol,
            'days': days,
            'candles': len(closes),
            'last_close': closes[-1] if closes else None,
            'source': used_source,
            'fallback_used': fallback_used(used_source, sources),
            'indicators': calculate_indicators(
                closes,
                requested,
                highs=[float(v) for v in df['high']],
                lows=[float(v) for v in df['low']],
                volumes=[float(v) for v in df['volume'].fillna(0)],
            ),
        })
