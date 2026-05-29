# Copyright (c) 2026 bachbnt. All rights reserved.
"""
alert_daemon.py — Price alert background daemon for finance-mcp.

Runs alongside the MCP server, polling the shared alerts.json store at a
configurable interval. When a price condition is met the daemon sends a
Telegram message and marks the alert as triggered so it is not re-fired.

Setup:
  1. Create a Telegram bot via @BotFather → /newbot → copy the token.
  2. Retrieve your chat ID: send /start to your bot, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and read "chat.id".
  3. Copy the sample config:  cp .env.example .env
  4. Fill in TELEGRAM_BOT_TOKEN (and optionally CHECK_INTERVAL) in .env.
  5. Run:  python alert_daemon.py
"""

import contextlib
import importlib
import io
import json
import logging
import os
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# Path to the shared alert store written by server.py
ALERTS_FILE = os.path.join(os.path.dirname(__file__), 'alerts.json')

# Telegram bot token loaded from .env; empty string disables Telegram delivery
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')

# Seconds between price-check cycles; lower = faster alerts but more API calls
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '60'))

VN_QUOTE_SOURCES = ('VCI', 'KBS', 'MSN')
CRYPTO_EXCHANGES = ('binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc')


def _quiet(fn, *args, **kwargs):
    buf = io.StringIO()
    previous_disable = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return fn(*args, **kwargs)
    finally:
        logging.disable(previous_disable)


def _silent_import(module: str, name: str):
    buf = io.StringIO()
    previous_disable = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            module_obj = importlib.import_module(module)
        return getattr(module_obj, name)
    finally:
        logging.disable(previous_disable)


def _load_alerts() -> list:
    """Load the alert list from the JSON store.

    Returns:
        List of alert dicts, or an empty list if the file does not exist.
    """
    if not os.path.exists(ALERTS_FILE):
        return []
    with open(ALERTS_FILE) as f:
        return json.load(f)


def _save_alerts(alerts: list) -> None:
    """Persist the alert list back to the JSON store.

    Args:
        alerts: Updated list of alert dicts to write.
    """
    with open(ALERTS_FILE, 'w') as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)


def _get_crypto_price(symbol: str) -> float:
    """Fetch the latest spot price for a cryptocurrency pair with exchange fallback.

    Args:
        symbol: Base asset or full pair, e.g. 'BTC' or 'BTC/USDT'.
                Defaults to USDT quote when no slash is present.

    Returns:
        Last traded price as a float.
    """
    import ccxt
    if '/' not in symbol:
        symbol = f"{symbol}/USDT"
    errors = []
    for exchange in CRYPTO_EXCHANGES:
        try:
            ex = getattr(ccxt, exchange)({'enableRateLimit': True})
            return ex.fetch_ticker(symbol)['last']
        except Exception as e:
            errors.append(f"{exchange}: {e}")
    raise RuntimeError(f"All crypto exchanges failed ({'; '.join(errors)})")


def _get_vn_stock_price(symbol: str) -> float | None:
    """Fetch the latest closing price for a Vietnam-listed stock.

    Looks back up to 10 calendar days to find the most recent session.

    Args:
        symbol: Ticker symbol, e.g. 'VNM', 'TCB'.

    Returns:
        Latest closing price as a float, or None if no data was found.
    """
    Quote = _silent_import('vnstock.api.quote', 'Quote')

    end = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
    for source in VN_QUOTE_SOURCES:
        try:
            quote = _quiet(Quote, symbol=symbol.upper(), source=source)
            df = _quiet(quote.history, start=start, end=end, interval='1D')
            if not df.empty:
                return float(df.iloc[-1]['close'])
        except Exception:
            continue
    return None


def _send_telegram(chat_id: str, message: str) -> None:
    """Send a text message to a Telegram chat via the bot API.

    Falls back to printing to stdout when TELEGRAM_TOKEN or chat_id is absent.

    Args:
        chat_id: Telegram chat or user ID to deliver the message to.
        message: Plain-text message content.
    """
    if not TELEGRAM_TOKEN:
        print(f"[NO TOKEN] {message}")
        return
    if not chat_id:
        print(f"[NO CHAT_ID] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={'chat_id': chat_id, 'text': message}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def check_alerts() -> None:
    """Run one evaluation pass over all pending (non-triggered) alerts.

    For each alert:
      - Fetches the current price from the appropriate source.
      - Evaluates the condition ('above' / 'below').
      - If triggered: sends a Telegram notification, stamps the alert,
        and marks it triggered so it is not re-evaluated.

    The alerts.json file is written only when at least one alert fires,
    reducing unnecessary disk I/O.
    """
    alerts = _load_alerts()
    changed = False

    for alert in alerts:
        if alert.get('triggered'):
            continue
        try:
            asset_type = alert.get('asset_type', 'crypto')
            symbol = alert['symbol']

            if asset_type == 'crypto':
                current = _get_crypto_price(symbol)
            else:
                current = _get_vn_stock_price(symbol)

            if current is None:
                continue

            triggered = (
                (alert['condition'] == 'above' and current >= alert['price']) or
                (alert['condition'] == 'below' and current <= alert['price'])
            )

            if triggered:
                msg = (
                    f"[ALERT {alert['id']}] {symbol}\n"
                    f"Current price: {current:,.2f}\n"
                    f"Condition: {alert['condition']} {alert['price']:,.2f}"
                )
                _send_telegram(alert.get('telegram_chat_id', ''), msg)
                print(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
                alert['triggered'] = True
                alert['triggered_at'] = datetime.now().isoformat()
                alert['triggered_price'] = current
                changed = True
            else:
                print(
                    f"{datetime.now().strftime('%H:%M:%S')} "
                    f"{symbol}: {current:,.2f} "
                    f"(target {alert['condition']} {alert['price']:,.2f})"
                )

        except Exception as e:
            print(f"Error checking alert {alert.get('id')}: {e}")

    if changed:
        _save_alerts(alerts)


if __name__ == '__main__':
    print(f"Alert daemon started. Checking every {CHECK_INTERVAL}s...")
    while True:
        check_alerts()
        time.sleep(CHECK_INTERVAL)
