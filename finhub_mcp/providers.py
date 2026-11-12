import contextlib
import importlib
import io
import logging
import re
import sys
import threading
import time
from datetime import datetime, timezone

from finhub_mcp.config import get_dict, get_list, get_number
from finhub_mcp.exceptions import ProviderFallbackError

VN_QUOTE_SOURCES = get_list('vn_quote_sources')
VN_INTRADAY_SOURCES = get_list('vn_intraday_sources')
VN_LISTING_SOURCES = get_list('vn_listing_sources')
VN_STOCK_SOURCES = get_list('vn_stock_sources')
VN_BOARD_SOURCES = get_list('vn_board_sources')
SUPPORTED_EXCHANGES = get_list('crypto_exchanges')
FUTURES_DEFAULT_TYPES = get_dict('futures_default_types')

_noise_lock = threading.Lock()
_noise_depth = 0
_noise_saved: tuple | None = None


@contextlib.contextmanager
def suppress_noise():
    """Silence stdout/stderr and warning logs from upstream libraries.

    Safe to nest and to use from several threads: the first entrant installs the
    redirect and the last one to leave restores the original state.
    """
    global _noise_depth, _noise_saved
    with _noise_lock:
        if _noise_depth == 0:
            _noise_saved = (sys.stdout, sys.stderr, logging.root.manager.disable)
            sys.stdout = sys.stderr = io.StringIO()
            logging.disable(logging.WARNING)
        _noise_depth += 1
    try:
        yield
    finally:
        with _noise_lock:
            _noise_depth -= 1
            if _noise_depth == 0 and _noise_saved is not None:
                sys.stdout, sys.stderr, previous_disable = _noise_saved
                logging.disable(previous_disable)
                _noise_saved = None


def quiet(fn, *args, **kwargs):
    """Call fn while suppressing stdout/stderr noise from upstream libraries."""
    with suppress_noise():
        return fn(*args, **kwargs)


class VnstockUnavailableError(RuntimeError):
    """Raised when a Vietnam-data tool is used but the optional vnstock package is not installed."""

    def __init__(self):
        super().__init__(
            "Vietnam market data needs the optional 'vnstock' package (4.x), which is not installed. "
            "See the README section 'Vietnam data (vnstock)'. Crypto, world markets and news do not need it."
        )


def _is_missing_vnstock(error: ModuleNotFoundError) -> bool:
    return bool(error.name) and error.name.split('.')[0] == 'vnstock'


def silent_import(module: str, name: str):
    """Import a symbol without leaking banners to MCP stdout."""
    try:
        with suppress_noise():
            module_obj = importlib.import_module(module)
    except ModuleNotFoundError as e:
        if _is_missing_vnstock(e):
            raise VnstockUnavailableError() from e
        raise
    return getattr(module_obj, name)


def provider_order(value: str, defaults: tuple[str, ...], label: str) -> list[str]:
    """Build a provider fallback order from 'auto' or a comma-separated list."""
    if value is None or value.strip().lower() in {'auto', 'fallback', 'any'}:
        parts = defaults
    else:
        parts = tuple(part.strip() for part in value.split(',') if part.strip())

    allowed = {item.lower(): item for item in defaults}
    order = []
    for part in parts:
        key = part.lower()
        if key not in allowed:
            raise ValueError(f"Unsupported {label}: {part}. Choose from: auto, {', '.join(defaults)}")
        canonical = allowed[key]
        if canonical not in order:
            order.append(canonical)
    if not order:
        raise ValueError(f"No {label} providers configured")
    return order


_COOLDOWN_ERROR = 'skipped: cooling down after a recent network error'
_TRANSIENT_ERROR_NAMES = {'NetworkError', 'Timeout', 'ConnectionError'}
_cooldown_lock = threading.Lock()
_cooldown_until: dict[str, float] = {}


def is_transient_error(exc: BaseException | None) -> bool:
    """True for network-level failures (timeouts, connection errors, ccxt NetworkError).

    Looks through wrapper exceptions: tenacity's RetryError (used by vnstock after its
    own retries) and ``__cause__`` chains.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        if any(cls.__name__ in _TRANSIENT_ERROR_NAMES for cls in type(exc).__mro__):
            return True
        inner = None
        last_attempt = getattr(exc, 'last_attempt', None)  # tenacity.RetryError
        if last_attempt is not None and hasattr(last_attempt, 'exception'):
            try:
                inner = last_attempt.exception()
            except Exception:
                inner = None
        exc = inner or exc.__cause__
    return False


def reset_cooldowns() -> None:
    with _cooldown_lock:
        _cooldown_until.clear()


def is_cooling_down(provider: str) -> bool:
    with _cooldown_lock:
        return _cooldown_until.get(provider, 0) > time.monotonic()


def _start_cooldown(provider: str) -> None:
    seconds = get_number('provider_cooldown_seconds')
    if seconds > 0:
        with _cooldown_lock:
            _cooldown_until[provider] = time.monotonic() + seconds


def _end_cooldown(provider: str) -> None:
    with _cooldown_lock:
        _cooldown_until.pop(provider, None)


def first_success(providers: list[str], fn, empty_msg: str = 'no data'):
    """Run fn(provider) until one provider returns a non-empty result.

    A provider that fails with a network error is skipped for ``provider_cooldown_seconds``
    so a dead exchange does not slow down every call. If every provider is cooling down
    they are all tried anyway.
    """
    errors = {}
    active = [p for p in providers if not is_cooling_down(p)] or list(providers)
    for provider in providers:
        if provider not in active:
            errors[provider] = _COOLDOWN_ERROR
            continue
        try:
            result = fn(provider)
            is_empty = getattr(result, 'empty', None)
            if is_empty is True or (is_empty is None and hasattr(result, '__len__') and len(result) == 0):
                errors[provider] = empty_msg
                continue
            _end_cooldown(provider)
            return provider, result
        except Exception as e:
            errors[provider] = str(e) or type(e).__name__
            if is_transient_error(e):
                _start_cooldown(provider)
    details = '; '.join(f"{provider}: {message}" for provider, message in errors.items())
    raise ProviderFallbackError(f"All providers failed ({details})", errors)


def fallback_used(provider: str, providers: list[str]) -> bool:
    return len(providers) > 1 and provider != providers[0]


def clamp(value: int, low: int, high: int) -> int:
    """Clamp a numeric tool argument into [low, high], coercing numeric strings."""
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Expected an integer between {low} and {high}, got {value!r}") from e
    return min(max(number, low), high)


_VN_SYMBOL_RE = re.compile(r'^[A-Z0-9._-]{1,20}$')


def vn_symbol(symbol: str) -> str:
    """Normalize a Vietnam ticker/index symbol, rejecting empty or odd input."""
    normalized = (symbol or '').strip().upper()
    if not _VN_SYMBOL_RE.match(normalized):
        raise ValueError(f"Invalid symbol: {symbol!r}. Use a ticker such as FPT, VCB or VNINDEX.")
    return normalized


def vn_quote(symbol: str, source: str = 'VCI'):
    Quote = silent_import('vnstock.api.quote', 'Quote')
    return quiet(Quote, symbol=symbol.upper(), source=source)


def vn_stock(symbol: str, source: str = 'VCI'):
    Vnstock = silent_import('vnstock', 'Vnstock')
    with suppress_noise():
        return Vnstock().stock(symbol=symbol.upper(), source=source)


def vn_listing(source: str):
    Listing = silent_import('vnstock.api.listing', 'Listing')
    return quiet(Listing, source=source)


def vn_trading(source: str):
    Trading = silent_import('vnstock.api.trading', 'Trading')
    return quiet(Trading, source=source)


_exchange_lock = threading.Lock()
_exchanges: dict[tuple[str, str | None], object] = {}


def _exchange(name: str, default_type: str | None):
    """Return a shared ccxt instance so markets are loaded once, not on every call."""
    import ccxt
    key = name.lower()
    if key not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange. Choose from: {', '.join(sorted(SUPPORTED_EXCHANGES))}")
    with _exchange_lock:
        instance = _exchanges.get((key, default_type))
        if instance is None:
            options = {'enableRateLimit': True, 'timeout': int(get_number('crypto_timeout_ms', 1))}
            if default_type:
                options['options'] = {'defaultType': default_type}
            instance = getattr(ccxt, key)(options)
            _exchanges[(key, default_type)] = instance
        return instance


def get_exchange(name: str):
    return _exchange(name, None)


def get_futures_exchange(name: str):
    return _exchange(name, FUTURES_DEFAULT_TYPES.get(name.lower(), 'swap'))


def crypto_symbol(symbol: str) -> str:
    symbol = (symbol or '').strip().upper()
    if not symbol:
        raise ValueError("Symbol must not be empty")
    if '/' not in symbol:
        return f"{symbol}/USDT"
    return symbol


def ts_to_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def timeframe_to_ms(timeframe: str) -> int:
    timeframe = (timeframe or '').strip()
    unit = timeframe[-1:]
    try:
        value = int(timeframe[:-1])
    except ValueError as e:
        raise ValueError("Invalid timeframe. Use values like 1m, 5m, 15m, 1h, 4h.") from e
    if value <= 0:
        raise ValueError("Invalid timeframe. Use values like 1m, 5m, 15m, 1h, 4h.")
    multipliers = {
        's': 1000,
        'm': 60 * 1000,
        'h': 60 * 60 * 1000,
        'd': 24 * 60 * 60 * 1000,
        'w': 7 * 24 * 60 * 60 * 1000,
    }
    if unit not in multipliers:
        raise ValueError("Invalid timeframe. Use values like 1m, 5m, 15m, 1h, 4h.")
    return value * multipliers[unit]
