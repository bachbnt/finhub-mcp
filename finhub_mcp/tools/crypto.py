# Copyright (c) 2026 bachbnt. All rights reserved.

from datetime import datetime, timezone

from finhub_mcp.indicators import calculate_indicators, parse_indicators
from finhub_mcp.providers import (
    SUPPORTED_EXCHANGES,
    clamp,
    crypto_symbol,
    fallback_used,
    first_success,
    get_exchange,
    get_futures_exchange,
    provider_order,
    timeframe_to_ms,
    ts_to_utc,
)
from finhub_mcp.responses import success
from finhub_mcp.services import fetch_crypto_ohlcv, fetch_crypto_ticker, fetch_crypto_tickers
from finhub_mcp.tooling import register_tool


def _candles(ohlcv) -> list[dict]:
    return [
        {'time': ts_to_utc(ts), 'open': o, 'high': h, 'low': l, 'close': c, 'volume': v}
        for ts, o, h, l, c, v in ohlcv
    ]


def register(mcp) -> None:
    tool = register_tool(mcp)

    @tool
    def get_crypto_price(symbol: str, exchange: str = 'auto') -> str:
        """Get the current spot price and 24-hour statistics for a cryptocurrency pair."""
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        used_exchange, t = first_success(
            exchanges,
            lambda ex_name: fetch_crypto_ticker(ex_name, symbol),
            f"no ticker found for {symbol}",
        )
        return success({
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
            'fallback_used': fallback_used(used_exchange, exchanges),
        })

    @tool
    def get_crypto_history(symbol: str, timeframe: str = '1d', limit: int = 30, exchange: str = 'auto') -> str:
        """Get historical OHLCV candlestick data for a cryptocurrency pair."""
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        timeframe_to_ms(timeframe)
        limit = clamp(limit, 1, 1000)
        used_exchange, ohlcv = first_success(
            exchanges,
            lambda ex_name: fetch_crypto_ohlcv(ex_name, symbol, timeframe, limit),
            f"no candles found for {symbol}",
        )
        return success(
            _candles(ohlcv),
            symbol=symbol,
            timeframe=timeframe,
            exchange=used_exchange,
            fallback_used=fallback_used(used_exchange, exchanges),
        )

    @tool
    def get_crypto_intraday(
        symbol: str,
        timeframe: str = '5m',
        hours: int = 24,
        max_candles: int = 1000,
        exchange: str = 'auto',
    ) -> str:
        """Get intraday OHLCV candles for a cryptocurrency over the last N hours."""
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        hours = clamp(hours, 1, 24)
        max_candles = clamp(max_candles, 1, 2000)
        timeframe_ms = timeframe_to_ms(timeframe)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        since_ms = now_ms - hours * 60 * 60 * 1000

        def fetch(exchange_name: str):
            ex = get_exchange(exchange_name)
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

        used_exchange, ohlcv = first_success(
            exchanges,
            fetch,
            f"no intraday candles found for {symbol}",
        )
        return success(
            _candles(ohlcv),
            symbol=symbol,
            timeframe=timeframe,
            hours=hours,
            exchange=used_exchange,
            fallback_used=fallback_used(used_exchange, exchanges),
        )

    @tool
    def get_top_crypto(limit: int = 10, exchange: str = 'auto') -> str:
        """List the top cryptocurrencies ranked by 24-hour USDT trading volume."""
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        limit = clamp(limit, 1, 100)
        used_exchange, tickers = first_success(
            exchanges,
            fetch_crypto_tickers,
            'no tickers found',
        )
        usdt = {k: v for k, v in tickers.items() if k.endswith('/USDT') and v.get('quoteVolume')}
        top = sorted(usdt.values(), key=lambda x: x.get('quoteVolume', 0), reverse=True)[:limit]
        data = [{
            'symbol': t['symbol'],
            'last': t['last'],
            'change_pct_24h': round(t.get('percentage') or 0, 2),
            'volume_usdt_24h': round(t.get('quoteVolume') or 0, 0),
        } for t in top]
        return success(data, exchange=used_exchange, fallback_used=fallback_used(used_exchange, exchanges))

    @tool
    def get_crypto_orderbook(symbol: str, depth: int = 10, exchange: str = 'auto') -> str:
        """Get the current order book for a cryptocurrency pair."""
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        depth = clamp(depth, 5, 50)
        used_exchange, ob = first_success(
            exchanges,
            lambda ex_name: get_exchange(ex_name).fetch_order_book(symbol, limit=depth),
            f"no order book found for {symbol}",
        )
        spread = ob['asks'][0][0] - ob['bids'][0][0] if ob['bids'] and ob['asks'] else None
        return success({
            'symbol': symbol,
            'exchange': used_exchange,
            'datetime': ob.get('datetime'),
            'spread': round(spread, 4) if spread is not None else None,
            'spread_pct': round(spread / ob['asks'][0][0] * 100, 4) if spread is not None else None,
            'bids': ob['bids'][:depth],
            'asks': ob['asks'][:depth],
            'fallback_used': fallback_used(used_exchange, exchanges),
        })

    @tool
    def get_crypto_trades(symbol: str, limit: int = 20, exchange: str = 'auto') -> str:
        """Get the most recent public trades for a cryptocurrency pair."""
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        limit = clamp(limit, 1, 50)
        used_exchange, trades = first_success(
            exchanges,
            lambda ex_name: get_exchange(ex_name).fetch_trades(symbol, limit=limit),
            f"no trades found for {symbol}",
        )
        data = [{
            'time': t['datetime'],
            'side': t['side'],
            'price': t['price'],
            'amount': t['amount'],
            'cost': t.get('cost'),
        } for t in trades]
        return success(
            data,
            symbol=symbol,
            exchange=used_exchange,
            fallback_used=fallback_used(used_exchange, exchanges),
        )

    @tool
    def get_crypto_funding_rate(symbol: str, exchange: str = 'auto', history_limit: int = 8) -> str:
        """Get the current funding rate and recent history for a perpetual futures contract."""
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        history_limit = clamp(history_limit, 1, 100)

        def fetch(exchange_name: str):
            ex = get_futures_exchange(exchange_name)
            fr = ex.fetch_funding_rate(symbol)
            history = ex.fetch_funding_rate_history(symbol, limit=history_limit)
            return fr, history

        used_exchange, payload = first_success(exchanges, fetch, f"no funding data found for {symbol}")
        fr, history = payload
        return success({
            'symbol': symbol,
            'exchange': used_exchange,
            'funding_rate': fr.get('fundingRate'),
            'funding_rate_pct': round((fr.get('fundingRate') or 0) * 100, 6),
            'mark_price': fr.get('markPrice'),
            'index_price': fr.get('indexPrice'),
            'next_funding': fr.get('nextFundingDatetime'),
            'fallback_used': fallback_used(used_exchange, exchanges),
            'history': [
                {
                    'datetime': h['datetime'],
                    'rate': h['fundingRate'],
                    'rate_pct': round(h['fundingRate'] * 100, 6) if h['fundingRate'] is not None else None,
                }
                for h in history
            ],
        })

    @tool
    def get_crypto_open_interest(symbol: str, exchange: str = 'auto') -> str:
        """Get the current open interest for a perpetual futures contract."""
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        used_exchange, oi = first_success(
            exchanges,
            lambda ex_name: get_futures_exchange(ex_name).fetch_open_interest(symbol),
            f"no open interest found for {symbol}",
        )
        return success({
            'symbol': symbol,
            'exchange': used_exchange,
            'datetime': oi.get('datetime'),
            'open_interest_coins': oi.get('openInterestAmount'),
            'open_interest_usdt': oi.get('openInterestValue'),
            'fallback_used': fallback_used(used_exchange, exchanges),
        })

    @tool
    def get_crypto_indicators(
        symbol: str,
        timeframe: str = '1d',
        limit: int = 120,
        indicators: str = 'sma,ema,rsi,macd,bollinger',
        exchange: str = 'auto',
    ) -> str:
        """Calculate technical indicators from crypto OHLCV history.

        indicators: comma-separated from sma, ema, rsi, macd, bollinger (default set), atr, stochastic,
        vwap, volume_ma, support_resistance, signals (MA/MACD crosses and bias), or "all".
        signals' golden/death cross needs limit >= 200 candles; vwap covers the fetched window; the last
        candle may still be forming, which skews volume_ma's last_vs_ma_ratio.
        """
        requested = parse_indicators(indicators)
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        timeframe_to_ms(timeframe)
        limit = clamp(limit, 60, 1000)
        used_exchange, ohlcv = first_success(
            exchanges,
            lambda ex_name: fetch_crypto_ohlcv(ex_name, symbol, timeframe, limit),
            f"no candles found for {symbol}",
        )
        rows = [c for c in ohlcv if c[2] is not None and c[3] is not None and c[4] is not None]
        closes = [float(c[4]) for c in rows]
        return success({
            'symbol': symbol,
            'timeframe': timeframe,
            'candles': len(closes),
            'last_close': closes[-1] if closes else None,
            'exchange': used_exchange,
            'fallback_used': fallback_used(used_exchange, exchanges),
            'indicators': calculate_indicators(
                closes,
                requested,
                highs=[float(c[2]) for c in rows],
                lows=[float(c[3]) for c in rows],
                volumes=[float(c[5] or 0) for c in rows],
            ),
        })
