# Copyright (c) 2026 bachbnt. All rights reserved.

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG: dict[str, Any] = {
    'vn_quote_sources': ['VCI', 'KBS', 'MSN'],
    'vn_intraday_sources': ['KBS', 'VCI', 'MSN'],
    'vn_listing_sources': ['VCI', 'KBS'],
    'vn_stock_sources': ['VCI', 'KBS'],
    'vn_board_sources': ['KBS'],
    'crypto_exchanges': ['binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc'],
    'futures_default_types': {
        'binance': 'future',
        'okx': 'swap',
        'bybit': 'swap',
        'kucoin': 'swap',
        'gate': 'swap',
        'mexc': 'swap',
    },
    'alert_store': 'alerts.json',
    'alert_retention_days': 30,
    'crypto_timeout_ms': 10000,
    'provider_cooldown_seconds': 30,
    'http_timeout_seconds': 10,
    'cache_ttl_seconds': {
        'ticker': 10,
        'history': 300,
        'market_overview': 60,
        'news': 300,
    },
    # RSS feeds for get_market_news. Add your own, or set "enabled": false to drop one.
    'news_feeds': {
        'cafef_stock': {
            'name': 'CafeF - Thị trường chứng khoán',
            'url': 'https://cafef.vn/thi-truong-chung-khoan.rss',
            'group': 'vn',
            'lang': 'vi',
        },
        'cafef_company': {
            'name': 'CafeF - Doanh nghiệp',
            'url': 'https://cafef.vn/doanh-nghiep.rss',
            'group': 'vn',
            'lang': 'vi',
        },
        'cafef_finance': {
            'name': 'CafeF - Tài chính ngân hàng',
            'url': 'https://cafef.vn/tai-chinh-ngan-hang.rss',
            'group': 'vn',
            'lang': 'vi',
        },
        'vietstock_stock': {
            'name': 'Vietstock - Cổ phiếu',
            'url': 'https://vietstock.vn/830/chung-khoan/co-phieu.rss',
            'group': 'vn',
            'lang': 'vi',
        },
        'vnexpress_business': {
            'name': 'VnExpress - Kinh doanh',
            'url': 'https://vnexpress.net/rss/kinh-doanh.rss',
            'group': 'vn',
            'lang': 'vi',
        },
        'cointelegraph': {
            'name': 'Cointelegraph',
            'url': 'https://cointelegraph.com/rss',
            'group': 'crypto',
            'lang': 'en',
        },
        'decrypt': {
            'name': 'Decrypt',
            'url': 'https://decrypt.co/feed',
            'group': 'crypto',
            'lang': 'en',
        },
    },
}


class ConfigError(ValueError):
    """Raised when config.json exists but is invalid."""


def config_path() -> Path:
    configured = os.getenv('FINHUB_CONFIG')
    if configured:
        return Path(configured).expanduser()
    return BASE_DIR / 'config.json'


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


_cache_lock = threading.Lock()
_cache_state: tuple[Path, int | None, dict[str, Any]] | None = None


def _read_config() -> dict[str, Any]:
    """Return the merged config, re-reading config.json only when its mtime changes.

    The returned object is shared: callers must not mutate it.
    """
    global _cache_state
    path = config_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    with _cache_lock:
        if _cache_state is not None and _cache_state[0] == path and _cache_state[1] == mtime:
            return _cache_state[2]
        if mtime is None:
            merged = deepcopy(DEFAULT_CONFIG)
        else:
            try:
                with open(path) as f:
                    configured = json.load(f)
            except json.JSONDecodeError as e:
                raise ConfigError(f"Invalid config JSON at {path}: {e}") from e
            if not isinstance(configured, dict):
                raise ConfigError(f"Invalid config at {path}: root must be an object")
            merged = _merge(DEFAULT_CONFIG, configured)
        _cache_state = (path, mtime, merged)
        return merged


def load_config() -> dict[str, Any]:
    return deepcopy(_read_config())


def get_list(name: str) -> tuple[str, ...]:
    value = _read_config().get(name)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"Invalid config value for {name}: expected a non-empty string list")
    return tuple(value)


def get_dict(name: str) -> dict[str, Any]:
    value = _read_config().get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"Invalid config value for {name}: expected an object")
    return deepcopy(value)


def get_number(name: str, minimum: float = 0) -> float:
    """Read a numeric top-level config value that must be >= minimum."""
    value = _read_config().get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"Invalid config value for {name}: expected a number >= {minimum}")
    return value


def get_ttl(kind: str) -> float:
    """Cache TTL in seconds for a cache kind (ticker/history/market_overview); 0 disables."""
    value = _read_config().get('cache_ttl_seconds', {}).get(kind, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"Invalid config value for cache_ttl_seconds.{kind}: expected a number >= 0")
    return value


def get_alert_store_path() -> str:
    """Alert store path. The FINHUB_ALERT_STORE environment variable overrides config.json (used by Docker)."""
    value = os.getenv('FINHUB_ALERT_STORE') or _read_config().get('alert_store')
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("Invalid config value for alert_store: expected a non-empty string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path)

