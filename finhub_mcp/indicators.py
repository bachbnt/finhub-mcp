# Copyright (c) 2026 bachbnt. All rights reserved.

import statistics
from typing import Any

DEFAULT_INDICATORS = ('sma', 'ema', 'rsi', 'macd', 'bollinger')
SUPPORTED_INDICATORS = DEFAULT_INDICATORS + (
    'atr', 'stochastic', 'vwap', 'volume_ma', 'support_resistance', 'signals',
)
# Thresholds and windows shared by the code and the finhub://indicators/glossary resource.
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
STOCH_OVERBOUGHT = 80
STOCH_OVERSOLD = 20
CROSS_LOOKBACK = 5
SR_WINDOW = 3
SR_TOLERANCE_PCT = 1.0
SR_MAX_LEVELS = 3

# Indicators that need more than closing prices.
OHLCV_INDICATORS = {'atr', 'stochastic', 'vwap', 'volume_ma', 'support_resistance'}


def parse_indicators(value: str) -> list[str]:
    """Parse a comma-separated list; 'auto'/'default' give the classic set, 'all' gives everything."""
    if value is None or value.strip().lower() in {'auto', 'default'}:
        return list(DEFAULT_INDICATORS)
    if value.strip().lower() == 'all':
        return list(SUPPORTED_INDICATORS)
    requested = []
    for item in value.split(','):
        key = item.strip().lower()
        if not key:
            continue
        if key not in SUPPORTED_INDICATORS:
            raise ValueError(f"Unsupported indicator: {item.strip()}. Choose from: {', '.join(SUPPORTED_INDICATORS)}")
        if key not in requested:
            requested.append(key)
    if not requested:
        raise ValueError("No indicators requested")
    return requested


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    """Standard EMA seeded with the SMA of the first `period` values.

    Returns len(values) - period + 1 points (the EMA at each index from period-1 on),
    or [] when there is not enough data.
    """
    if period <= 0 or len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    result = [current]
    for value in values[period:]:
        current = (value - current) * multiplier + current
        result.append(current)
    return result


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(values: list[float], period: int = RSI_PERIOD) -> float | None:
    """RSI with Wilder's smoothing (matches TradingView and most charting tools)."""
    if len(values) <= period:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    avg_gain = sum(max(change, 0) for change in changes[:period]) / period
    avg_loss = sum(abs(min(change, 0)) for change in changes[:period]) / period
    for change in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + abs(min(change, 0))) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def macd_series(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9,
) -> tuple[list[float], list[float]]:
    """MACD line and signal line, each aligned to the END of ``values``.

    The MACD line has len(values) - slow + 1 points and the signal line
    len(macd) - signal + 1; either is [] when there is not enough data.
    """
    fast_series = ema_series(values, fast)
    slow_series = ema_series(values, slow)
    if not slow_series:
        return [], []
    # Align both series on the indices where the slow EMA exists.
    macd_line = [f - s for f, s in zip(fast_series[len(fast_series) - len(slow_series):], slow_series, strict=True)]
    return macd_line, ema_series(macd_line, signal)


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, float | None]:
    macd_line, signal_line = macd_series(values, fast, slow, signal)
    if not macd_line:
        return {'macd': None, 'signal': None, 'histogram': None}
    current_macd = macd_line[-1]
    if not signal_line:
        return {'macd': current_macd, 'signal': None, 'histogram': None}
    return {
        'macd': current_macd,
        'signal': signal_line[-1],
        'histogram': current_macd - signal_line[-1],
    }


def bollinger(values: list[float], period: int = 20, stddev: float = 2.0) -> dict[str, float | None]:
    if len(values) < period:
        return {'middle': None, 'upper': None, 'lower': None, 'width_pct': None}
    window = values[-period:]
    middle = sum(window) / period
    deviation = statistics.pstdev(window)
    upper = middle + stddev * deviation
    lower = middle - stddev * deviation
    return {
        'middle': middle,
        'upper': upper,
        'lower': lower,
        'width_pct': ((upper - lower) / middle * 100) if middle else None,
    }


def sma_series(values: list[float], period: int) -> list[float]:
    """Simple moving average at each index from period-1 on (len(values) - period + 1 points)."""
    if period <= 0 or len(values) < period:
        return []
    window = sum(values[:period])
    result = [window / period]
    for index in range(period, len(values)):
        window += values[index] - values[index - period]
        result.append(window / period)
    return result


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """True range for each candle after the first."""
    return [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> dict[str, float | None]:
    """Average True Range with Wilder's smoothing, plus ATR as a percentage of the last close."""
    ranges = true_ranges(highs, lows, closes)
    if len(ranges) < period:
        return {str(period): None, 'pct_of_close': None}
    value = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return {str(period): value, 'pct_of_close': (value / closes[-1] * 100) if closes[-1] else None}


def stochastic(
    highs: list[float], lows: list[float], closes: list[float], k_period: int = 14, d_period: int = 3, smooth: int = 3,
) -> dict[str, Any]:
    """Slow stochastic oscillator (%K smoothed by `smooth`, %D = SMA of %K)."""
    empty = {'k': None, 'd': None, 'state': None}
    raw = []
    for i in range(k_period - 1, len(closes)):
        highest = max(highs[i - k_period + 1:i + 1])
        lowest = min(lows[i - k_period + 1:i + 1])
        raw.append(50.0 if highest == lowest else (closes[i] - lowest) / (highest - lowest) * 100)
    k_line = sma_series(raw, smooth)
    d_line = sma_series(k_line, d_period)
    if not d_line:
        return empty
    k, d = k_line[-1], d_line[-1]
    state = 'overbought' if k > STOCH_OVERBOUGHT else 'oversold' if k < STOCH_OVERSOLD else 'neutral'
    return {'k': k, 'd': d, 'state': state}


def vwap(highs: list[float], lows: list[float], closes: list[float], volumes: list[float]) -> dict[str, float | None]:
    """Volume-weighted average price over the whole supplied window (typical price = (H+L+C)/3)."""
    total_volume = sum(volumes)
    if not closes or total_volume <= 0:
        return {'vwap': None, 'close_vs_vwap_pct': None}
    weighted = sum((h + l_ + c) / 3 * v for h, l_, c, v in zip(highs, lows, closes, volumes, strict=True))
    value = weighted / total_volume
    return {'vwap': value, 'close_vs_vwap_pct': (closes[-1] / value - 1) * 100 if value else None}


def volume_ma(volumes: list[float], periods: tuple[int, ...] = (20, 50)) -> dict[str, float | None]:
    """Volume moving averages and how the latest volume compares with the shortest one."""
    data: dict[str, float | None] = {str(period): sma(volumes, period) for period in periods}
    data['last_volume'] = volumes[-1] if volumes else None
    base = data.get(str(periods[0]))
    data['last_vs_ma_ratio'] = (volumes[-1] / base) if volumes and base else None
    return data


def _cluster(levels: list[float], tolerance_pct: float) -> list[tuple[float, int]]:
    """Merge price levels closer than tolerance_pct into (mean level, touches) clusters."""
    clusters: list[list[float]] = []
    for level in sorted(levels):
        if clusters and abs(level - sum(clusters[-1]) / len(clusters[-1])) / level * 100 <= tolerance_pct:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    return [(sum(group) / len(group), len(group)) for group in clusters]


def support_resistance(
    highs: list[float], lows: list[float], closes: list[float],
    window: int = SR_WINDOW, tolerance_pct: float = SR_TOLERANCE_PCT, max_levels: int = SR_MAX_LEVELS,
) -> dict[str, Any]:
    """Swing-pivot support/resistance clusters around the last close, plus classic pivot points.

    A pivot high/low is a candle whose high/low is the extreme of the ``window`` candles on each side.
    Nearby pivots (within ``tolerance_pct``) merge into one level; ``touches`` counts how many.
    Classic pivots are computed from the most recent candle.
    """
    if len(closes) < window * 2 + 1:
        return {'pivot_points': None, 'support': [], 'resistance': []}
    pivot_highs = [
        highs[i] for i in range(window, len(highs) - window) if highs[i] == max(highs[i - window:i + window + 1])
    ]
    pivot_lows = [
        lows[i] for i in range(window, len(lows) - window) if lows[i] == min(lows[i - window:i + window + 1])
    ]
    levels = _cluster(pivot_highs + pivot_lows, tolerance_pct)
    last = closes[-1]
    support = sorted((lv for lv in levels if lv[0] < last), key=lambda lv: last - lv[0])[:max_levels]
    resistance = sorted((lv for lv in levels if lv[0] > last), key=lambda lv: lv[0] - last)[:max_levels]
    high, low, close = highs[-1], lows[-1], closes[-1]
    pivot = (high + low + close) / 3
    return {
        'pivot_points': {
            'pivot': pivot,
            'r1': 2 * pivot - low,
            'r2': pivot + (high - low),
            's1': 2 * pivot - high,
            's2': pivot - (high - low),
        },
        'support': [{'level': level, 'touches': touches} for level, touches in support],
        'resistance': [{'level': level, 'touches': touches} for level, touches in resistance],
    }


def last_cross(fast: list[float], slow: list[float], lookback: int = CROSS_LOOKBACK) -> dict[str, Any]:
    """Most recent crossover of two series aligned at the end, within the last ``lookback`` candles.

    Returns {'state': 'bullish'|'bearish'|None, 'cross': 'up'|'down'|None, 'candles_ago': int|None}.
    """
    size = min(len(fast), len(slow))
    if size < 2:
        return {'state': None, 'cross': None, 'candles_ago': None}
    fast, slow = fast[-size:], slow[-size:]
    diff = [f - s for f, s in zip(fast, slow, strict=True)]
    state = 'bullish' if diff[-1] > 0 else 'bearish' if diff[-1] < 0 else None
    for ago in range(min(lookback, size - 1)):
        now, before = diff[-1 - ago], diff[-2 - ago]
        if before <= 0 < now:
            return {'state': state, 'cross': 'up', 'candles_ago': ago}
        if before >= 0 > now:
            return {'state': state, 'cross': 'down', 'candles_ago': ago}
    return {'state': state, 'cross': None, 'candles_ago': None}


def signals(closes: list[float], lookback: int = CROSS_LOOKBACK) -> dict[str, Any]:
    """Trend/momentum signals derived from closes: MA crosses, MACD cross, RSI and Bollinger state.

    Golden/death cross (SMA50 vs SMA200) needs at least 200 candles; otherwise it is null.
    """
    data: dict[str, Any] = {}

    sma20, sma50, sma200 = (sma_series(closes, n) for n in (20, 50, 200))
    short_cross = last_cross(sma20, sma50, lookback) if sma50 else None
    data['sma_20_50'] = short_cross and {
        **short_cross,
        'event': {'up': 'bullish_cross', 'down': 'bearish_cross'}.get(short_cross['cross']),
    }
    long_cross = last_cross(sma50, sma200, lookback) if sma200 else None
    data['sma_50_200'] = long_cross and {
        **long_cross,
        'event': {'up': 'golden_cross', 'down': 'death_cross'}.get(long_cross['cross']),
    }

    macd_line, signal_line = macd_series(closes)
    data['macd'] = (
        {**last_cross(macd_line, signal_line, lookback), 'histogram': macd_line[-1] - signal_line[-1]}
        if signal_line else None
    )

    rsi_value = rsi(closes)
    data['rsi'] = None if rsi_value is None else {
        'value': rsi_value,
        'state': (
            'overbought' if rsi_value >= RSI_OVERBOUGHT else 'oversold' if rsi_value <= RSI_OVERSOLD else 'neutral'
        ),
    }

    band = bollinger(closes)
    if band['middle'] is None:
        data['bollinger'] = None
    else:
        last = closes[-1]
        data['bollinger'] = {
            'position': 'above_upper' if last > band['upper'] else 'below_lower' if last < band['lower'] else 'inside',
        }

    last_close = closes[-1] if closes else None
    data['trend'] = {
        'above_sma50': (last_close > sma50[-1]) if sma50 else None,
        'above_sma200': (last_close > sma200[-1]) if sma200 else None,
    }
    data['bias'] = _bias(data)
    return data


def _bias(data: dict[str, Any]) -> dict[str, Any]:
    """Crude vote across the signals above: +1 for each bullish reading, -1 for each bearish one."""
    score = 0
    for key in ('sma_20_50', 'sma_50_200', 'macd'):
        entry = data.get(key)
        if entry and entry.get('state'):
            score += 1 if entry['state'] == 'bullish' else -1
    rsi_entry = data.get('rsi')
    if rsi_entry and rsi_entry['state'] != 'neutral':
        # Overbought/oversold are contrarian readings.
        score += -1 if rsi_entry['state'] == 'overbought' else 1
    for flag in data['trend'].values():
        if flag is not None:
            score += 1 if flag else -1
    return {'score': score, 'label': 'bullish' if score >= 2 else 'bearish' if score <= -2 else 'neutral'}


def round_nested(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: round_nested(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [round_nested(item, digits) for item in value]
    return value


def calculate_indicators(
    closes: list[float],
    indicators: list[str],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
) -> dict[str, Any]:
    """Compute the requested indicators. OHLCV-based ones need highs/lows/volumes aligned with closes."""
    data: dict[str, Any] = {}
    if 'sma' in indicators:
        data['sma'] = {
            '20': sma(closes, 20),
            '50': sma(closes, 50),
        }
    if 'ema' in indicators:
        data['ema'] = {
            '12': ema(closes, 12),
            '26': ema(closes, 26),
        }
    if 'rsi' in indicators:
        data['rsi'] = {
            '14': rsi(closes, 14),
        }
    if 'macd' in indicators:
        data['macd'] = macd(closes)
    if 'bollinger' in indicators:
        data['bollinger'] = bollinger(closes)

    needs_ohlcv = OHLCV_INDICATORS & set(indicators)
    if needs_ohlcv and not (highs and lows and volumes
                            and len(highs) == len(lows) == len(volumes) == len(closes)):
        raise ValueError(f"Indicators {', '.join(sorted(needs_ohlcv))} need aligned high/low/close/volume data")
    if 'atr' in indicators:
        data['atr'] = atr(highs, lows, closes)
    if 'stochastic' in indicators:
        data['stochastic'] = stochastic(highs, lows, closes)
    if 'vwap' in indicators:
        data['vwap'] = vwap(highs, lows, closes, volumes)
    if 'volume_ma' in indicators:
        data['volume_ma'] = volume_ma(volumes)
    if 'support_resistance' in indicators:
        data['support_resistance'] = support_resistance(highs, lows, closes)
    if 'signals' in indicators:
        data['signals'] = signals(closes)
    return round_nested(data)
