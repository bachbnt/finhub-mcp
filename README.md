# FinHub MCP

> Copyright (c) 2026 bachbnt. All rights reserved.

A local [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for financial market data: Vietnam stocks, crypto spot and perpetual futures, gold, forex, and price alerts.

## Latest

- Added free provider fallback for Vietnam stocks and crypto market data.
- Added Vietnam stock intraday matched trades via `get_vn_stock_intraday`.
- Added crypto intraday OHLCV for the last 1-24 hours via `get_crypto_intraday`.
- Added source/exchange metadata in fallback-aware responses.
- Updated alert daemon to use the same fallback model for crypto and Vietnam stock prices.
- Suppressed noisy `vnstock` banners/logging so MCP stdio stays clean.

## Features

| Category | Tools |
|---|---|
| Vietnam stocks | Latest price, intraday trades, OHLCV history, market indices overview, symbol search |
| Company & financials | Company profile, income statement, balance sheet, cash flow, ratios |
| Crypto spot | Price ticker, intraday OHLCV, OHLCV history, top by volume, order book, recent trades |
| Crypto futures | Funding rate and history, open interest |
| Market extras | SJC gold price, Vietcombank forex rates |
| Price alerts | Create, list, and delete alerts; optional Telegram notification daemon |

## Requirements

- Python 3.11+
- `pip` or `uv`
- Optional Telegram bot token for alert notifications

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

## MCP Setup

Add this server to your Claude Code MCP config:

```json
{
  "mcpServers": {
    "finance": {
      "command": "/Users/bachbui/Desktop/source/finhub/.venv/bin/python",
      "args": ["/Users/bachbui/Desktop/source/finhub/server.py"]
    }
  }
}
```

Restart Claude Code after changing MCP tool signatures so the client reloads the schema.

## Provider Fallback

Default `auto` mode uses free public sources and falls back when a provider errors or returns empty data.

| Area | Default fallback order |
|---|---|
| VN quote/history | `VCI -> KBS -> MSN` |
| VN intraday | `KBS -> VCI -> MSN` |
| VN listing/company/financials | `VCI -> KBS` |
| Crypto spot/futures | `binance -> okx -> bybit -> kucoin -> gate -> mexc` |

You can pass a single provider or comma-separated fallback list, for example:

```text
source="KBS"
source="KBS,VCI"
exchange="okx,binance"
```

Responses include `source` or `exchange` plus `fallback_used` where applicable.

## Tool Reference

### Vietnam Stocks

| Tool | Description |
|---|---|
| `get_vn_stock_price(symbol, source="auto")` | Latest OHLCV snapshot and day-over-day change |
| `get_vn_stock_intraday(symbol, period="latest", limit=100, page_size=100, pages=5, source="auto")` | Matched trades for today or latest session |
| `get_vn_stock_history(symbol, days=30, source="auto")` | Daily OHLCV history for 1-365 calendar days |
| `get_vn_market_overview(source="auto")` | VNINDEX, VN30, and HNXINDEX snapshot |
| `search_vn_stock(query, source="auto")` | Search by ticker or company name |

`get_vn_stock_intraday` supports:

- `period="latest"`: most recent trading session.
- `period="today"`: strict current calendar day; may return an empty list on weekends/holidays.

### Company & Financials

| Tool | Description |
|---|---|
| `get_company_overview(symbol, source="auto")` | Company profile, sector, market cap, listing metadata |
| `get_financials(symbol, statement="income_statement", period="year", source="auto")` | Financial statements and ratios |

Supported `statement` values: `income_statement`, `balance_sheet`, `cash_flow`, `ratio`.

Supported `period` values depend on the upstream source, commonly `year` and `quarter`.

### Crypto Spot

| Tool | Description |
|---|---|
| `get_crypto_price(symbol, exchange="auto")` | Ticker, bid/ask, 24h high/low, volume, VWAP |
| `get_crypto_intraday(symbol, timeframe="5m", hours=24, max_candles=1000, exchange="auto")` | OHLCV candles for the last 1-24 hours |
| `get_crypto_history(symbol, timeframe="1d", limit=30, exchange="auto")` | Historical OHLCV candles |
| `get_top_crypto(limit=10, exchange="auto")` | Top USDT pairs by 24h quote volume |
| `get_crypto_orderbook(symbol, depth=10, exchange="auto")` | Bid/ask depth and spread |
| `get_crypto_trades(symbol, limit=20, exchange="auto")` | Recent public trades |

Symbols may be base assets like `BTC` or full pairs like `BTC/USDT`.

Common timeframes: `1m`, `5m`, `15m`, `1h`, `4h`, `1d`, `1w`.

### Crypto Futures

| Tool | Description |
|---|---|
| `get_crypto_funding_rate(symbol, exchange="auto", history_limit=8)` | Current funding rate plus recent history |
| `get_crypto_open_interest(symbol, exchange="auto")` | Current perpetual futures open interest |

### Market Extras

| Tool | Description |
|---|---|
| `get_gold_price()` | SJC gold buy/sell prices |
| `get_forex()` | Vietcombank exchange rates |

### Alerts

| Tool | Description |
|---|---|
| `add_alert(symbol, condition, price, asset_type="crypto", telegram_chat_id="")` | Create a price alert |
| `list_alerts()` | Show alert store contents |
| `remove_alert(alert_id)` | Delete an alert |

`condition` must be `above` or `below`. `asset_type` supports `crypto` and `vn_stock`.

## Alert Daemon

The MCP server writes alerts to `alerts.json`. The daemon polls that file and sends Telegram notifications when conditions are met.

```bash
python alert_daemon.py
```

Telegram setup:

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Send `/start` to the bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates`.
4. Copy the chat ID.
5. Set `TELEGRAM_BOT_TOKEN` in `.env`.
6. Pass the chat ID to `add_alert`.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | No | empty | Telegram bot token for alert delivery |
| `CHECK_INTERVAL` | No | `60` | Alert daemon polling interval in seconds |

## Data Sources

| Data | Source |
|---|---|
| Vietnam stocks | `vnstock` public providers: VCI, KBS, MSN where supported |
| Crypto | `ccxt` public exchange APIs |
| Gold | `vnstock` SJC helper |
| Forex | `vnstock` Vietcombank helper |

TCBS is not wired as a direct adapter in the current code because the installed `vnstock` API classes do not accept `TCBS` as a source for quote/listing/company/financials. Add a dedicated TCBS adapter if you want direct WebSocket or quote-board support.

## License

Copyright (c) 2026 bachbnt. All rights reserved.
