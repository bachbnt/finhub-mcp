# Copyright (c) 2026 bachbnt. All rights reserved.
"""MCP resources: read-only context a client can attach (alerts, effective config, guides, company profiles)."""

import asyncio
import json
from typing import Any

from finhub_mcp import alerts
from finhub_mcp import indicators as ind
from finhub_mcp.config import get_dict, get_number
from finhub_mcp.news import get_feeds
from finhub_mcp.providers import (
    SUPPORTED_EXCHANGES,
    VN_BOARD_SOURCES,
    VN_INTRADAY_SOURCES,
    VN_LISTING_SOURCES,
    VN_QUOTE_SOURCES,
    VN_STOCK_SOURCES,
    first_success,
    provider_order,
    vn_symbol,
)
from finhub_mcp.responses import error, success
from finhub_mcp.services import fetch_company_overview, frame_records


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def alerts_snapshot() -> dict[str, Any]:
    """Stored alerts with the Telegram chat id masked: this text is attached to model context."""

    def mask(chat_id: str) -> str:
        return f"***{chat_id[-3:]}" if chat_id else ''

    rows = [{**alert, 'telegram_chat_id': mask(str(alert.get('telegram_chat_id') or ''))}
            for alert in alerts.load_alerts()]
    return {
        'pending': [a for a in rows if not a.get('triggered')],
        'triggered': [a for a in rows if a.get('triggered')],
    }


def effective_config() -> dict[str, Any]:
    """The settings that shape tool behaviour. Contains no credentials."""
    return {
        'provider_order': {
            'crypto_exchanges': list(SUPPORTED_EXCHANGES),
            'vn_quote': list(VN_QUOTE_SOURCES),
            'vn_intraday': list(VN_INTRADAY_SOURCES),
            'vn_listing': list(VN_LISTING_SOURCES),
            'vn_stock': list(VN_STOCK_SOURCES),
            'vn_board': list(VN_BOARD_SOURCES),
        },
        'cache_ttl_seconds': get_dict('cache_ttl_seconds'),
        'provider_cooldown_seconds': get_number('provider_cooldown_seconds'),
        'crypto_timeout_ms': get_number('crypto_timeout_ms'),
        'http_timeout_seconds': get_number('http_timeout_seconds'),
        'alert_retention_days': get_number('alert_retention_days'),
        'news_feeds': {key: {k: v for k, v in feed.items() if k != 'url'} for key, feed in get_feeds().items()},
        'vn_groups': ['HOSE', 'HNX', 'UPCOM', 'VN30', 'VN100', 'ALL'],
    }


def indicator_glossary() -> str:
    """Markdown reference for every indicator, generated from the constants the code uses."""
    return f"""# Indicator reference

How `get_crypto_indicators` and `get_vn_stock_indicators` compute each value. Use `indicators="all"`
for everything or a comma-separated subset. Values are rounded to 6 decimals. An indicator without
enough candles is `null`.

| Indicator | Definition | Minimum candles |
|---|---|---|
| `sma` | Simple moving average of closes, periods 20 and 50 | 20 / 50 |
| `ema` | Exponential moving average, periods 12 and 26, seeded with the SMA of the first N closes | 12 / 26 |
| `rsi` | RSI({ind.RSI_PERIOD}) with Wilder smoothing (matches TradingView). 100 when there are no losses | {ind.RSI_PERIOD + 1} |
| `macd` | EMA12 - EMA26; signal = EMA9 of the MACD line; histogram = MACD - signal | 26 (signal needs 34) |
| `bollinger` | Middle = SMA20; bands = middle +/- 2 population standard deviations; `width_pct` = (upper - lower) / middle * 100 | 20 |
| `atr` | Average True Range(14), Wilder smoothing; `pct_of_close` = ATR / last close * 100 | 15 |
| `stochastic` | Slow stochastic (14,3,3): %K = SMA3 of raw %K, %D = SMA3 of %K. `overbought` above {ind.STOCH_OVERBOUGHT}, `oversold` below {ind.STOCH_OVERSOLD} | 18 |
| `vwap` | Sum((high+low+close)/3 * volume) / sum(volume) over the **whole fetched window**, plus `close_vs_vwap_pct` | 1 |
| `volume_ma` | Volume SMA20 and SMA50, `last_volume`, and `last_vs_ma_ratio` = last volume / SMA20 | 20 / 50 |
| `support_resistance` | See below | {ind.SR_WINDOW * 2 + 1} |
| `signals` | See below | 20 (golden/death cross needs 200) |

Notes: the last candle may still be forming, which distorts `volume_ma` and `vwap`. VWAP over a long
window (a year of daily bars) is a long-run average price, not an intraday VWAP; fetch a short window
of intraday candles for that.

## support_resistance

- A swing high/low is a candle whose high/low is the extreme of the {ind.SR_WINDOW} candles on each side.
- Pivots within {ind.SR_TOLERANCE_PCT}% of each other merge into one level; `touches` counts the merged
  pivots (more touches = stronger level).
- Up to {ind.SR_MAX_LEVELS} supports (below the last close) and resistances (above), nearest first.
- `pivot_points` are classic pivots from the most recent candle: P = (H+L+C)/3, R1 = 2P-L, S1 = 2P-H,
  R2 = P+(H-L), S2 = P-(H-L).

## signals

Crossovers are looked for in the last {ind.CROSS_LOOKBACK} candles; `candles_ago` is 0 for the latest candle.

- `sma_20_50`: state (fast above/below slow) and a recent `bullish_cross` / `bearish_cross`.
- `sma_50_200`: state and a recent `golden_cross` / `death_cross`. Needs 200 candles, else null.
- `macd`: state from the histogram sign and a recent MACD/signal cross.
- `rsi`: `overbought` at {ind.RSI_OVERBOUGHT} or above, `oversold` at {ind.RSI_OVERSOLD} or below.
- `bollinger.position`: `above_upper`, `below_lower` or `inside`.
- `trend`: whether the close is above SMA50 and SMA200.
- `bias`: a crude vote, +1 per bullish and -1 per bearish reading among `sma_20_50`, `sma_50_200`,
  `macd` and each `trend` flag; RSI is contrarian (overbought -1, oversold +1). `bullish` at score >= 2,
  `bearish` at <= -2, otherwise `neutral`. It summarises the readings above; it is not a forecast.

## News sentiment

`get_market_news` / `get_news_sentiment` score headlines with a keyword lexicon (Vietnamese with
diacritics, and English) in [-1, 1]; the headline counts double the summary. Labels: positive >= 0.2,
negative <= -0.2. It reads tone only and can misjudge sarcasm, negation and context.
"""


def usage_guide() -> str:
    """Markdown guide to choosing tools and reading their output."""
    return """# FinHub usage guide

## Reading responses
- Every tool returns `{"ok": true, "data": ..., "meta": {...}}` or `{"ok": false, "error": ..., "meta": {...}}`.
- `meta.error_type` is `validation`, `config`, `provider_failure` (retryable) or `not_found`.
- For a single object, `source` / `exchange` and `fallback_used` are inside `data`; for lists they are in `meta`.
- `meta.warnings` lists partial failures (a feed or pair that failed while others succeeded). Report them.
- Data is cached for the TTLs in `finhub://config` (tickers 10s, history 300s): polling faster returns the same data.

## Units and symbols
- Vietnam stock prices are in **thousand VND** (50.9 = 50,900 VND). Indices are in points. Traded value is in
  billion VND (`value_bn_vnd`). Foreign net **values are estimates** (net volume x average price).
- Crypto symbols: `BTC` means `BTC/USDT`; pass `ETH/USDC` for other quotes.
- VN groups for market tools: HOSE, HNX, UPCOM, VN30, VN100, ALL.

## Which tool for what
- One quote: `get_crypto_price`, `get_vn_stock_price`. Many at once: `get_crypto_prices`, `get_vn_stock_prices`.
- Technical analysis: `get_crypto_indicators` / `get_vn_stock_indicators` with `indicators="all"`
  (see `finhub://indicators/glossary`). Use `limit=250` (crypto) or `days=365` (VN) so SMA200 and the golden cross exist.
- Derivatives mood: `get_crypto_funding_rate`, `get_crypto_open_interest`, `get_crypto_long_short_ratio`,
  `get_crypto_liquidations`, `get_crypto_basis`, `get_crypto_fear_greed`.
- VN market state: `get_vn_market_overview` (indices), `get_vn_market_breadth`, `get_vn_top_movers`,
  `get_vn_foreign_trading`, `screen_vn_stocks`.
- Gold and FX: `get_gold_price` (SJC), `get_forex` (Vietcombank), `get_world_metal_price`, `get_world_forex`.
- News: `get_market_news`, `get_news_sentiment`.
- Alerts: `add_alert`, `list_alerts`, `remove_alert` (monitored by `alert_daemon.py`, not by the server).

## Caveats to state to the user
- VN price-board tools show reference prices with a "No trades yet" warning before the first trade of the day
  (HOSE matches from about 09:15 ICT) and outside trading days.
- Liquidations come only from OKX and Gate and cover just the latest orders, not 24 hours.
- Yahoo-based world forex/metal data is unofficial; gold returns spot from gold-api.com with a futures fallback.
- Sentiment is a keyword heuristic. Nothing here is investment advice.
"""


def register(mcp) -> None:
    @mcp.resource('finhub://alerts', name='alerts', mime_type='application/json',
                  description='Pending and triggered price alerts (Telegram chat ids masked).')
    async def alerts_resource() -> str:
        return _json(await asyncio.to_thread(alerts_snapshot))

    @mcp.resource('finhub://config', name='config', mime_type='application/json',
                  description='Effective provider order, cache TTLs, timeouts and news feeds. No credentials.')
    def config_resource() -> str:
        return _json(effective_config())

    @mcp.resource('finhub://indicators/glossary', name='indicator-glossary', mime_type='text/markdown',
                  description='How every technical indicator and signal is computed, with thresholds.')
    def glossary_resource() -> str:
        return indicator_glossary()

    @mcp.resource('finhub://guide/usage', name='usage-guide', mime_type='text/markdown',
                  description='Which tool to use, units, response format and data caveats.')
    def usage_resource() -> str:
        return usage_guide()

    @mcp.resource('finhub://vn/{symbol}/overview', name='vn-company-overview', mime_type='application/json',
                  description='Company profile for a Vietnam-listed ticker (same data as get_company_overview).')
    async def company_overview_resource(symbol: str) -> str:
        def load() -> str:
            ticker = vn_symbol(symbol)
            sources = provider_order('auto', tuple(VN_STOCK_SOURCES), 'VN stock source')
            used, df = first_success(
                sources, lambda src: fetch_company_overview(ticker, src), f"no company overview found for {ticker}",
            )
            return success(frame_records(df), symbol=ticker, source=used)

        try:
            return await asyncio.to_thread(load)
        except Exception as e:
            return error(e)
