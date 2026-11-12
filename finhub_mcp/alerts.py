# Copyright (c) 2026 bachbnt. All rights reserved.

import contextlib
import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any

from finhub_mcp.config import get_alert_store_path, get_number

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

ALERTS_FILE = get_alert_store_path()
ALERTS_LOCK_FILE = f"{ALERTS_FILE}.lock"


def set_alerts_file(path: str) -> None:
    """Override the alert store path, primarily for tests."""
    global ALERTS_FILE, ALERTS_LOCK_FILE
    ALERTS_FILE = path
    ALERTS_LOCK_FILE = f"{path}.lock"


def _acquire(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return
    lock_file.seek(0)
    while True:  # LK_LOCK retries for ~10s and then raises, so keep trying
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            return
        except OSError:
            continue


def _release(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def alerts_lock():
    with open(ALERTS_LOCK_FILE, 'a') as lock_file:
        _acquire(lock_file)
        try:
            yield
        finally:
            _release(lock_file)


def read_alerts_unlocked() -> list[dict[str, Any]]:
    if not os.path.exists(ALERTS_FILE):
        return []
    with open(ALERTS_FILE) as f:
        return json.load(f)


def write_alerts_unlocked(alerts: list[dict[str, Any]]) -> None:
    directory = os.path.dirname(ALERTS_FILE)
    fd, tmp_path = tempfile.mkstemp(prefix='.alerts.', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(alerts, f, indent=2, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, ALERTS_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def load_alerts() -> list[dict[str, Any]]:
    with alerts_lock():
        return read_alerts_unlocked()


def save_alerts(alerts: list[dict[str, Any]]) -> None:
    with alerts_lock():
        write_alerts_unlocked(alerts)


def validate_alert(condition: str, price: float, asset_type: str) -> tuple[str | None, float | None, str, str]:
    condition = condition.strip().lower()
    asset_type = asset_type.strip().lower()
    try:
        target_price = float(price)
    except (TypeError, ValueError):
        return "Invalid price. Price must be a positive number.", None, condition, asset_type
    if condition not in {'above', 'below'}:
        return "Invalid condition. Choose from: above, below", None, condition, asset_type
    if asset_type not in {'crypto', 'vn_stock'}:
        return "Invalid asset_type. Choose from: crypto, vn_stock", None, condition, asset_type
    if not math.isfinite(target_price) or target_price <= 0:
        return "Invalid price. Price must be a positive number.", None, condition, asset_type
    return None, target_price, condition, asset_type


def add_alert(symbol: str, condition: str, price: float, asset_type: str = 'crypto', telegram_chat_id: str = ''):
    validation_error, target_price, condition, asset_type = validate_alert(condition, price, asset_type)
    if validation_error:
        return None, validation_error
    symbol = (symbol or '').strip().upper()
    if not symbol:
        return None, "Invalid symbol. Symbol must not be empty."

    alert = {
        'id': str(uuid.uuid4())[:8],
        'symbol': symbol,
        'asset_type': asset_type,
        'condition': condition,
        'price': target_price,
        'telegram_chat_id': telegram_chat_id,
        'created_at': datetime.now().isoformat(),
        'triggered': False,
    }
    with alerts_lock():
        alerts = read_alerts_unlocked()
        alerts.append(alert)
        write_alerts_unlocked(alerts)
    return alert, None


def remove_alert(alert_id: str) -> bool:
    with alerts_lock():
        alerts = read_alerts_unlocked()
        filtered = [a for a in alerts if a['id'] != alert_id]
        if len(filtered) == len(alerts):
            return False
        write_alerts_unlocked(filtered)
    return True


def mark_alert_triggered(triggered_alert: dict[str, Any], triggered_price: float) -> bool:
    with alerts_lock():
        alerts = read_alerts_unlocked()
        for alert in alerts:
            same_alert = (
                alert.get('id') == triggered_alert.get('id') and
                alert.get('symbol') == triggered_alert.get('symbol') and
                alert.get('asset_type', 'crypto') == triggered_alert.get('asset_type', 'crypto') and
                alert.get('condition') == triggered_alert.get('condition') and
                alert.get('price') == triggered_alert.get('price') and
                not alert.get('triggered')
            )
            if same_alert:
                alert['triggered'] = True
                alert['triggered_at'] = datetime.now().isoformat()
                alert['triggered_price'] = triggered_price
                write_alerts_unlocked(alerts)
                return True
    return False


def prune_triggered(retention_days: float | None = None) -> int:
    """Delete alerts triggered more than retention_days ago; returns how many were removed.

    Defaults to the ``alert_retention_days`` config value; 0 keeps everything.
    """
    days = get_number('alert_retention_days') if retention_days is None else retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)

    def expired(alert: dict[str, Any]) -> bool:
        if not alert.get('triggered'):
            return False
        try:
            return datetime.fromisoformat(alert['triggered_at']) < cutoff
        except (KeyError, TypeError, ValueError):
            return False

    with alerts_lock():
        alerts = read_alerts_unlocked()
        kept = [alert for alert in alerts if not expired(alert)]
        if len(kept) != len(alerts):
            write_alerts_unlocked(kept)
        return len(alerts) - len(kept)
