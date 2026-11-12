# Copyright (c) 2026 bachbnt. All rights reserved.

from finhub_mcp.derivatives import (
    LIQUIDATION_EXCHANGES,
    LONG_SHORT_EXCHANGES,
    fetch_basis,
    fetch_fear_greed,
    fetch_liquidations,
    fetch_long_short_ratio,
    summarize_liquidations,
    summarize_long_short,
)
from finhub_mcp.providers import (
    SUPPORTED_EXCHANGES,
    clamp,
    crypto_symbol,
    fallback_used,
    first_success,
    provider_order,
    timeframe_to_ms,
)
from finhub_mcp.responses import success
from finhub_mcp.services import fetch_crypto_tickers_for
from finhub_mcp.tooling import register_tool
from finhub_mcp.tools.world import split_symbols

MAX_SYMBOLS = 50


def register(mcp) -> None:
    tool = register_tool(mcp)

    @tool
    def get_crypto_prices(symbols: list[str] | str, exchange: str = 'auto') -> str:
        """Get spot prices and 24h stats for many cryptocurrencies in one call (max 50).

        symbols: list or comma-separated string such as "BTC,ETH,SOL" (bare symbols default to /USDT).
        Symbols an exchange does not list are fetched from the next exchange; any still missing are reported
        in meta.missing.
        """
        wanted = list(dict.fromkeys(crypto_symbol(s) for s in split_symbols(symbols, MAX_SYMBOLS)))
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        found: dict[str, dict] = {}
        sources: dict[str, str] = {}
        remaining_exchanges = list(exchanges)
        while remaining_exchanges and len(found) < len(wanted):
            missing = tuple(s for s in wanted if s not in found)
            try:
                used, tickers = first_success(
                    remaining_exchanges,
                    lambda name, missing=missing: fetch_crypto_tickers_for(name, missing),
                    'no tickers found',
                )
            except Exception:
                if not found:
                    raise
                break
            for symbol, ticker in tickers.items():
                if symbol not in found and ticker.get('last') is not None:
                    found[symbol] = ticker
                    sources[symbol] = used
            remaining_exchanges = remaining_exchanges[remaining_exchanges.index(used) + 1:]
        data = [{
            'symbol': symbol,
            'exchange': sources[symbol],
            'last': t['last'],
            'change_pct_24h': round(t.get('percentage') or 0, 2),
            'high_24h': t.get('high'),
            'low_24h': t.get('low'),
            'volume_24h_usdt': round(t.get('quoteVolume') or 0, 0),
        } for symbol, t in ((s, found[s]) for s in wanted if s in found)]
        return success(
            data,
            missing=[s for s in wanted if s not in found] or None,
            fallback_used=any(fallback_used(sources[s], exchanges) for s in sources),
        )

    @tool
    def get_crypto_long_short_ratio(
        symbol: str, timeframe: str = '1h', limit: int = 24, exchange: str = 'auto',
    ) -> str:
        """Get the long/short account ratio history for a perpetual futures contract (Binance, OKX, Bybit).

        A ratio above 1 means more accounts are long than short. timeframe options depend on the exchange
        (Binance: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d; OKX: 5m, 1h, 1d).
        """
        exchanges = provider_order(exchange, LONG_SHORT_EXCHANGES, 'long/short exchange')
        symbol = crypto_symbol(symbol)
        timeframe_to_ms(timeframe)
        limit = clamp(limit, 2, 500)
        used, rows = first_success(
            exchanges,
            lambda name: fetch_long_short_ratio(name, symbol, timeframe, limit),
            f"no long/short data for {symbol}",
        )
        return success(
            rows,
            symbol=symbol,
            timeframe=timeframe,
            exchange=used,
            fallback_used=fallback_used(used, exchanges),
            summary=summarize_long_short(rows),
        )

    @tool
    def get_crypto_liquidations(symbol: str, limit: int = 50, exchange: str = 'auto') -> str:
        """Get recent forced liquidations on a perpetual swap (OKX and Gate; Binance/Bybit expose none over REST).

        Returns the most recent liquidations, newest first, with which side (long or short) was liquidated,
        plus totals. The window covers only the latest orders the exchange returns, not a full 24 hours.
        """
        exchanges = provider_order(exchange, LIQUIDATION_EXCHANGES, 'liquidation exchange')
        symbol = crypto_symbol(symbol)
        limit = clamp(limit, 1, 100)
        used, rows = first_success(
            exchanges,
            lambda name: fetch_liquidations(name, symbol, limit),
            f"no liquidations found for {symbol}",
        )
        return success(
            rows,
            symbol=symbol,
            exchange=used,
            fallback_used=fallback_used(used, exchanges),
            summary=summarize_liquidations(rows),
        )

    @tool
    def get_crypto_fear_greed(days: int = 1) -> str:
        """Get the Crypto Fear & Greed Index (0 = extreme fear, 100 = extreme greed) from alternative.me."""
        days = clamp(days, 1, 90)
        return success(fetch_fear_greed(days), source='alternative.me')

    @tool
    def get_crypto_basis(symbol: str, exchange: str = 'auto') -> str:
        """Get the spot-vs-futures basis: the perpetual and the nearest dated futures against the spot price.

        basis_pct is (futures - spot) / spot; annualized_pct scales it to a year for dated contracts, a rough
        read on the cash-and-carry yield. Coin-margined contracts are flagged margin="inverse".
        """
        exchanges = provider_order(exchange, SUPPORTED_EXCHANGES, 'crypto exchange')
        symbol = crypto_symbol(symbol)
        used, data = first_success(
            exchanges,
            lambda name: fetch_basis(name, symbol),
            f"no futures found for {symbol}",
        )
        warnings = data.get('warnings') or None
        return success(
            {key: value for key, value in data.items() if key != 'warnings'},
            fallback_used=fallback_used(used, exchanges),
            warnings=warnings,
        )
