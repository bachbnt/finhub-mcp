"""
Alert daemon - chay song song voi MCP server de kiem tra dieu kien va gui Telegram.

Cach dung:
  1. Tao Telegram bot: @BotFather -> /newbot -> lay token
  2. Lay chat ID: gui /start cho bot, vao https://api.telegram.org/bot<TOKEN>/getUpdates
  3. cp .env.example .env -> dien TELEGRAM_BOT_TOKEN
  4. python alert_daemon.py
"""

import json
import os
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

ALERTS_FILE = os.path.join(os.path.dirname(__file__), 'alerts.json')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHECK_INTERVAL = 60  # giay


def _load_alerts():
    if not os.path.exists(ALERTS_FILE):
        return []
    with open(ALERTS_FILE) as f:
        return json.load(f)


def _save_alerts(alerts):
    with open(ALERTS_FILE, 'w') as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)


def _get_crypto_price(symbol):
    import ccxt
    ex = ccxt.binance({'enableRateLimit': True})
    if '/' not in symbol:
        symbol = f"{symbol}/USDT"
    return ex.fetch_ticker(symbol)['last']


def _get_vn_stock_price(symbol):
    import contextlib, io
    from vnstock.api.quote import Quote
    end = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        df = Quote(symbol=symbol.upper(), source='VCI').history(start=start, end=end, interval='1D')
    if df.empty:
        return None
    return float(df.iloc[-1]['close'])


def _send_telegram(chat_id, message):
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


def check_alerts():
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
                    f"Gia hien tai: {current:,.2f}\n"
                    f"Dieu kien: {alert['condition']} {alert['price']:,.2f}"
                )
                _send_telegram(alert.get('telegram_chat_id', ''), msg)
                print(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
                alert['triggered'] = True
                alert['triggered_at'] = datetime.now().isoformat()
                alert['triggered_price'] = current
                changed = True
            else:
                print(f"{datetime.now().strftime('%H:%M:%S')} {symbol}: {current:,.2f} (target {alert['condition']} {alert['price']:,.2f})")

        except Exception as e:
            print(f"Loi kiem tra alert {alert.get('id')}: {e}")

    if changed:
        _save_alerts(alerts)


if __name__ == '__main__':
    print(f"Alert daemon da khoi dong. Kiem tra moi {CHECK_INTERVAL}s...")
    while True:
        check_alerts()
        time.sleep(CHECK_INTERVAL)
