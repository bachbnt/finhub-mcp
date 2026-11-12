# Copyright (c) 2026 bachbnt. All rights reserved.
"""MCP prompts: ready-made analysis workflows that tell the model which tools to call and how to report."""

import re

from finhub_mcp.providers import clamp, vn_symbol

_CRYPTO_RE = re.compile(r'^[A-Z0-9]{2,15}(/[A-Z0-9]{2,10})?$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]+')

STYLE = """Rules:
- Use only numbers returned by the tools. Never invent or estimate a price, level or percentage.
- Quote the data time and source where tools give one, and relay any `meta.warnings` or `error` you see.
- If a tool fails or returns null for an indicator, say so and continue with what you have.
- Reply in the language the user writes in (Vietnamese if unsure). This is analysis, not investment advice;
  say so in one short line at the end."""


def _market(market: str) -> str:
    value = (market or '').strip().lower()
    if value not in {'crypto', 'vn'}:
        raise ValueError("market must be 'crypto' or 'vn'")
    return value


def _symbol(symbol: str, market: str) -> str:
    """Normalise a ticker. Arguments are interpolated into prompt text, so reject anything that is not a ticker."""
    if market == 'vn':
        return vn_symbol(symbol)
    value = (symbol or '').strip().upper()
    if not _CRYPTO_RE.match(value):
        raise ValueError(f"Invalid crypto symbol: {symbol!r}. Use something like BTC or ETH/USDT.")
    return value


def _topic(topic: str) -> str:
    """Free-text topic: strip control characters and cap the length."""
    return _CONTROL_RE.sub(' ', topic or '').strip()[:80]


def technical_analysis_text(symbol: str, market: str = 'crypto', timeframe: str = '1d') -> str:
    market = _market(market)
    symbol = _symbol(symbol, market)
    if market == 'crypto':
        timeframe = (timeframe or '1d').strip()
        if not re.fullmatch(r'\d{1,3}[mhdw]', timeframe):
            raise ValueError("timeframe must look like 15m, 1h, 4h or 1d")
        steps = f"""1. `get_crypto_indicators` with symbol="{symbol}", timeframe="{timeframe}", limit=250, indicators="all".
2. `get_crypto_price` for symbol="{symbol}" (current price, 24h range and volume).
3. Derivatives context if {symbol} has a perpetual: `get_crypto_funding_rate`, `get_crypto_long_short_ratio`
   and `get_crypto_basis`. Skip any that error.
4. `get_crypto_fear_greed` for overall market mood."""
        context = f"{symbol} on the {timeframe} timeframe"
    else:
        steps = f"""1. `get_vn_stock_indicators` with symbol="{symbol}", days=365, indicators="all".
2. `get_vn_stock_prices` with symbols="{symbol}" for today's price, volume and foreign net volume
   (prices are in thousand VND; a "No trades yet" warning means the market has not traded today).
3. `get_market_news` with query="{symbol}", limit=5 for recent headlines and their sentiment."""
        context = f"{symbol} on daily candles"
    return f"""Run a technical analysis of {context} using the FinHub tools.

Steps:
{steps}

If you need the exact definition of an indicator or signal, read the resource `finhub://indicators/glossary`.

Report in this structure:
- **Trend**: price versus SMA20/50 and SMA200 (or say 200 candles were not available), and the `signals` crossovers.
- **Momentum**: RSI, MACD state and histogram, stochastic.
- **Volatility**: Bollinger position and width, ATR as a percent of price.
- **Levels**: the nearest supports and resistances (with touches) and the classic pivot points.
- **Volume**: `volume_ma` ratio (the last candle may be incomplete).
- **Context**: the extra data from the steps above.
- **Summary**: one balanced paragraph covering the bullish and bearish readings, plus what would change the view.

{STYLE}"""


def vn_market_brief_text() -> str:
    return f"""Write a brief of the Vietnam stock market right now using the FinHub tools.

Steps:
1. `get_vn_market_overview` for VNINDEX, VN30 and HNXINDEX.
2. `get_vn_market_breadth` with group="ALL" (advancers, decliners, ceiling/floor, turnover, foreign totals).
3. `get_vn_top_movers` with group="HOSE", by="change_pct", limit=5, then by="value", limit=5.
4. `get_vn_foreign_trading` with group="HOSE", limit=5.
5. `get_market_news` with source="vn", limit=5.

If the tools report "No trades yet", say the market has not opened or has not traded and describe the last session
only as far as the data allows. Prices are in thousand VND; foreign net values are estimates.

Report: **Indices**, **Breadth**, **Leaders and laggards**, **Foreign flow**, **Headlines**, then a two-sentence takeaway.

{STYLE}"""


def crypto_market_brief_text(symbols: str = 'BTC,ETH,SOL') -> str:
    if _CONTROL_RE.search(symbols or ''):
        raise ValueError("symbols must be a comma-separated list on one line")
    cleaned = [_symbol(s, 'crypto') for s in re.split(r'[,\s]+', symbols or '') if s]
    if not cleaned:
        raise ValueError("Provide at least one symbol")
    if len(cleaned) > 10:
        raise ValueError("At most 10 symbols")
    listed = ','.join(cleaned)
    return f"""Write a brief of the crypto market using the FinHub tools.

Steps:
1. `get_crypto_prices` with symbols="{listed}".
2. `get_crypto_fear_greed` with days=7.
3. For {cleaned[0]}: `get_crypto_funding_rate`, `get_crypto_long_short_ratio`, `get_crypto_basis` and
   `get_crypto_liquidations` (skip any that error).
4. `get_top_crypto` with limit=10 for what is trading the most.
5. `get_market_news` with source="crypto", limit=5.

Report: **Prices and 24h moves**, **Sentiment** (Fear & Greed trend), **Leverage and positioning** (funding, long/short
crowding, basis, which side was liquidated), **Most active**, **Headlines**, then a two-sentence takeaway.

{STYLE}"""


def gold_vs_sjc_text() -> str:
    return f"""Compare Vietnam's SJC gold price with the world gold price using the FinHub tools.

Steps:
1. `get_gold_price` for SJC buy and sell prices.
2. `get_world_metal_price` with metal="XAU": it returns USD per ounce and `vnd_per_luong_estimate`.
3. Optionally `get_world_forex` with pairs="USDVND" for the exchange rate used.

Compute the SJC sell price minus the world-equivalent price, in VND per lượng and as a percentage, using only the
returned numbers. State the units you convert between (SJC quotes may be per lượng or per chỉ: check the
returned fields; 1 lượng = 10 chỉ = 37.5 g) and that the estimate excludes tax, fees and the domestic premium.

Report: the two prices, the premium, and what could explain it (supply restrictions, fees, timing) without claiming
a cause the data does not show.

{STYLE}"""


def news_digest_text(topic: str = '', hours: int = 24) -> str:
    topic = _topic(topic)
    hours = clamp(hours, 1, 168)
    scope = f'about "{topic}"' if topic else 'across the market'
    query = f', query="{topic}"' if topic else ''
    return f"""Summarize the news {scope} from the last {hours} hours using the FinHub tools.

Steps:
1. `get_market_news` with source="auto"{query}, hours={hours}, limit=20.
2. `get_news_sentiment` with hours={hours}{query} for the overall tone and per-source split.

Group the headlines into 3 to 5 themes. For each theme give a one-line summary and the sources. Then give the
overall sentiment score and label and list which feeds failed (`meta.warnings`). Remember sentiment is a keyword
heuristic: do not present it as a measured market view. Link or name the source of each claim and do not add
facts that are not in the headlines or summaries.

{STYLE}"""


def alert_levels_text(symbol: str, market: str = 'crypto', timeframe: str = '1d') -> str:
    market = _market(market)
    symbol = _symbol(symbol, market)
    if market == 'crypto':
        timeframe = (timeframe or '1d').strip()
        if not re.fullmatch(r'\d{1,3}[mhdw]', timeframe):
            raise ValueError("timeframe must look like 15m, 1h, 4h or 1d")
        fetch = f'`get_crypto_indicators` with symbol="{symbol}", timeframe="{timeframe}", limit=250, indicators="support_resistance,atr,signals"'
        asset_type = 'crypto'
        units = 'the same units as the exchange price (USDT)'
    else:
        fetch = f'`get_vn_stock_indicators` with symbol="{symbol}", days=365, indicators="support_resistance,atr,signals"'
        asset_type = 'vn_stock'
        units = 'thousand VND, exactly like `get_vn_stock_price` (50.9 means 50,900 VND)'
    return f"""Suggest price alerts for {symbol} based on support and resistance, then create only the ones I approve.

Steps:
1. {fetch}.
2. `list_alerts` to see which alerts already exist for {symbol} so you do not duplicate them.
3. Propose up to 4 alerts: an "above" alert at the nearest resistance and a "below" alert at the nearest support, plus
   optionally one beyond the next level. For each give the price, the reason (level, number of touches, ATR distance
   from the current price) and the alert condition. Alert prices must be in {units}.
4. Show the proposals in a table and **ask which ones to create. Do not call `add_alert` until I confirm.**
5. After I confirm, call `add_alert` once per approved alert with asset_type="{asset_type}" and report the returned ids.

Alerts are checked by `alert_daemon.py`, so tell me that the daemon must be running for them to fire.

{STYLE}"""


def register(mcp) -> None:
    @mcp.prompt(name='technical_analysis', title='Technical analysis',
                description='Full technical analysis of a crypto or Vietnam stock: trend, momentum, levels, volume.')
    def technical_analysis(symbol: str, market: str = 'crypto', timeframe: str = '1d') -> str:
        return technical_analysis_text(symbol, market, timeframe)

    @mcp.prompt(name='vn_market_brief', title='Vietnam market brief',
                description='Indices, breadth, top movers, foreign flow and headlines for the Vietnam market.')
    def vn_market_brief() -> str:
        return vn_market_brief_text()

    @mcp.prompt(name='crypto_market_brief', title='Crypto market brief',
                description='Prices, Fear & Greed, leverage and positioning, most active coins and headlines.')
    def crypto_market_brief(symbols: str = 'BTC,ETH,SOL') -> str:
        return crypto_market_brief_text(symbols)

    @mcp.prompt(name='gold_vs_sjc', title='SJC vs world gold',
                description='Compare the SJC gold price with the world price converted to VND per lượng.')
    def gold_vs_sjc() -> str:
        return gold_vs_sjc_text()

    @mcp.prompt(name='news_digest', title='News digest',
                description='Themed summary of recent market news with sentiment, optionally about one topic.')
    def news_digest(topic: str = '', hours: int = 24) -> str:
        return news_digest_text(topic, hours)

    @mcp.prompt(name='alert_levels', title='Suggest alerts from support/resistance',
                description='Propose price alerts at support and resistance and create only the ones you approve.')
    def alert_levels(symbol: str, market: str = 'crypto', timeframe: str = '1d') -> str:
        return alert_levels_text(symbol, market, timeframe)
