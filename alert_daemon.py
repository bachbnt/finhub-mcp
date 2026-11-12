# Copyright (c) 2026 bachbnt. All rights reserved.
"""
alert_daemon.py — Price alert background daemon for FinHub.

Runs alongside the MCP server, polling the shared alerts.json store at a
configurable interval. When a price condition is met the daemon sends a
Telegram message and marks the alert as triggered so it is not re-fired.
"""

import os
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

from finhub_mcp import alerts
from finhub_mcp.providers import crypto_symbol, vn_symbol
from finhub_mcp.services import crypto_last_price, vn_last_close

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHECK_INTERVAL = max(5, int(os.getenv('CHECK_INTERVAL', '60')))


def now_str() -> str:
    return datetime.now().strftime('%H:%M:%S')


def redact(text: str) -> str:
    """Hide the Telegram token: requests puts the full bot URL into its error messages."""
    return text.replace(TELEGRAM_TOKEN, '***') if TELEGRAM_TOKEN else text


def fetch_price(asset_type: str, symbol: str) -> float:
    """Latest price for an alert's asset, with provider fallback and short-lived caching."""
    if asset_type == 'crypto':
        return crypto_last_price(crypto_symbol(symbol))
    return vn_last_close(vn_symbol(symbol))


def send_telegram(chat_id: str, message: str) -> bool:
    """Send a text message to Telegram, or log only when credentials are absent."""
    if not TELEGRAM_TOKEN:
        print(f"[NO TOKEN] {message}")
        return True
    if not chat_id:
        print(f"[NO CHAT_ID] {message}")
        return True
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={'chat_id': chat_id, 'text': message}, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(redact(f"Telegram error: {type(e).__name__}: {e}"))
        return False


def check_alerts() -> None:
    """Run one evaluation pass over all pending alerts.

    Each distinct (asset_type, symbol) is priced once per pass, however many alerts
    watch it, and a failed lookup is not retried for the other alerts on that symbol.
    """
    prices: dict[tuple[str, str], float | Exception] = {}

    def price_of(asset_type: str, symbol: str) -> float:
        key = (asset_type, symbol)
        if key not in prices:
            try:
                prices[key] = fetch_price(asset_type, symbol)
            except Exception as e:
                prices[key] = e
        result = prices[key]
        if isinstance(result, Exception):
            raise result
        return result

    for alert in alerts.load_alerts():
        if alert.get('triggered'):
            continue
        try:
            current = price_of(alert.get('asset_type', 'crypto'), alert['symbol'])
            symbol = alert['symbol']

            triggered = (
                (alert['condition'] == 'above' and current >= alert['price']) or
                (alert['condition'] == 'below' and current <= alert['price'])
            )
            if not triggered:
                print(
                    f"{now_str()} {symbol}: {current:,.2f} "
                    f"(target {alert['condition']} {alert['price']:,.2f})"
                )
                continue

            msg = (
                f"[ALERT {alert['id']}] {symbol}\n"
                f"Current price: {current:,.2f}\n"
                f"Condition: {alert['condition']} {alert['price']:,.2f}"
            )
            if not send_telegram(alert.get('telegram_chat_id', ''), msg):
                print(f"{now_str()} Alert {alert['id']} will retry.")
                continue
            print(f"{now_str()} {msg}")
            if not alerts.mark_alert_triggered(alert, current):
                print(f"{now_str()} Alert {alert['id']} changed or was removed.")
        except Exception as e:
            print(redact(f"Error checking alert {alert.get('id')}: {e}"))


def main() -> None:
    print(f"Alert daemon started. Checking every {CHECK_INTERVAL}s...")
    try:
        while True:
            try:
                removed = alerts.prune_triggered()
                if removed:
                    print(f"{now_str()} Pruned {removed} old triggered alert(s).")
                check_alerts()
            except Exception as e:  # keep the daemon alive on a corrupt store or transient error
                print(redact(f"{now_str()} Check failed: {e}"))
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("Alert daemon stopped.")


if __name__ == '__main__':
    main()
