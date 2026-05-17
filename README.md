# finance-mcp

> Copyright (c) 2026 bachbnt. All rights reserved.

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that gives AI assistants real-time access to financial market data — Vietnam stocks, cryptocurrencies, gold prices, forex rates, and configurable price alerts.

---

## Features

| Category | Tools |
|---|---|
| **Vietnam stocks** | Latest price, OHLCV history, market indices overview, symbol search |
| **Company & financials** | Company profile, income statement, balance sheet, cash flow, ratios |
| **Crypto (spot)** | Price ticker, OHLCV history, top by volume, order book, recent trades |
| **Crypto (futures)** | Funding rate + history, open interest |
| **Market extras** | SJC gold price, Vietcombank forex rates |
| **Price alerts** | Create / list / delete alerts; Telegram notification via daemon |

---

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) or `pip` for dependency management
- A Telegram bot token (optional, only needed for alert notifications)

---

## Installation

```bash
git clone <repo-url>
cd finance-mcp

# Create virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # or: uv pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and fill in TELEGRAM_BOT_TOKEN if you want alert notifications
```

---

## MCP Server Setup

Add the server to your Claude Code configuration (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "finance": {
      "command": "/absolute/path/to/finance-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/finance-mcp/server.py"]
    }
  }
}
```

Restart Claude Code. The `finance` MCP server will be available automatically.

---

## Alert Daemon

The alert daemon runs as a separate process alongside the MCP server. It reads `alerts.json` (written by the MCP tools) and sends Telegram notifications when price conditions are met.

```bash
# Terminal 1 — MCP server (managed by Claude Code)
python server.py

# Terminal 2 — alert daemon
python alert_daemon.py
```

### Getting a Telegram chat ID

1. Create a bot via [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Send `/start` to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser.
4. Find `"chat": {"id": <your_chat_id>}` in the response.
5. Set `TELEGRAM_BOT_TOKEN` in `.env`.
6. Pass the chat ID when calling `add_alert`.

---

## Tool Reference

### Vietnam Stocks

| Tool | Description |
|---|---|
| `get_vn_stock_price(symbol)` | Latest OHLCV + day-over-day change |
| `get_vn_stock_history(symbol, days)` | Daily OHLCV for up to 365 days back |
| `get_vn_market_overview()` | VNINDEX, VN30, HNXINDEX snapshot |
| `search_vn_stock(query)` | Search by ticker or company name (up to 20 results) |

### Company & Financials

| Tool | Description |
|---|---|
| `get_company_overview(symbol)` | Sector, market cap, listing exchange, website |
| `get_financials(symbol, statement, period)` | `income_statement` / `balance_sheet` / `cash_flow` / `ratio` × `year` / `quarter` |

### Crypto — Spot

| Tool | Description |
|---|---|
| `get_crypto_price(symbol, exchange)` | Ticker: last, bid, ask, 24h high/low, volume, VWAP |
| `get_crypto_history(symbol, timeframe, limit, exchange)` | OHLCV candles — 1m / 5m / 15m / 1h / 4h / 1d / 1w |
| `get_top_crypto(limit, exchange)` | Top N by 24h USDT volume |
| `get_crypto_orderbook(symbol, depth, exchange)` | Bid/ask book with spread and spread % |
| `get_crypto_trades(symbol, limit, exchange)` | Most recent public fills |

### Crypto — Futures / Perpetuals

| Tool | Description |
|---|---|
| `get_crypto_funding_rate(symbol, exchange, history_limit)` | Current rate, mark price, index price, rate history |
| `get_crypto_open_interest(symbol, exchange)` | Outstanding contracts in coins and USDT |

### Market Extras

| Tool | Description |
|---|---|
| `get_gold_price()` | SJC gold buy/sell prices across Vietnam branches |
| `get_forex()` | Vietcombank exchange rates for major currencies |

### Alerts

| Tool | Description |
|---|---|
| `add_alert(symbol, condition, price, asset_type, telegram_chat_id)` | Create a price alert (`above` / `below`) |
| `list_alerts()` | Show all alerts and their status |
| `remove_alert(alert_id)` | Delete an alert by ID |

**Supported exchanges:** `binance`, `okx`, `bybit`, `kucoin`, `gate`, `mexc`

---

## Data Sources

| Data | Source |
|---|---|
| Vietnam stock prices & history | [vnstock](https://github.com/thinh-vu/vnstock) via VCI |
| Company info & financials | vnstock via VCI |
| Crypto market data | [ccxt](https://github.com/ccxt/ccxt) |
| SJC gold price | SJC official API |
| Forex rates | Vietcombank API |

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | No | _(empty)_ | Telegram bot token for alert notifications |
| `CHECK_INTERVAL` | No | `60` | Alert daemon polling interval in seconds |

---

## License

Copyright (c) 2026 bachbnt. All rights reserved.
