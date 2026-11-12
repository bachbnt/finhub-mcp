# Copyright (c) 2026 bachbnt. All rights reserved.

import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

import alert_daemon
import server
from finhub_mcp import alerts, cache, config, providers
from finhub_mcp import indicators as ind
from finhub_mcp.exceptions import ProviderFallbackError
from finhub_mcp.responses import error, success


class TtlCacheTests(unittest.TestCase):
    def test_caches_within_ttl_and_expires(self):
        calls = []

        @cache.ttl_cache('ticker')
        def fetch(x):
            calls.append(x)
            return x * 2

        with patch('finhub_mcp.cache.get_ttl', return_value=10):
            with patch('finhub_mcp.cache.time.monotonic', return_value=100.0):
                self.assertEqual(fetch(2), 4)
                self.assertEqual(fetch(2), 4)
                self.assertEqual(fetch(3), 6)
            self.assertEqual(calls, [2, 3])
            with patch('finhub_mcp.cache.time.monotonic', return_value=111.0):
                fetch(2)
            self.assertEqual(calls, [2, 3, 2])

    def test_zero_ttl_disables_caching(self):
        calls = []

        @cache.ttl_cache('ticker')
        def fetch():
            calls.append(1)
            return 1

        with patch('finhub_mcp.cache.get_ttl', return_value=0):
            fetch()
            fetch()
        self.assertEqual(len(calls), 2)

    def test_exceptions_are_not_cached(self):
        calls = []

        @cache.ttl_cache('ticker')
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('boom')
            return 'ok'

        with patch('finhub_mcp.cache.get_ttl', return_value=10):
            with self.assertRaises(RuntimeError):
                flaky()
            self.assertEqual(flaky(), 'ok')
            self.assertEqual(flaky(), 'ok')
        self.assertEqual(len(calls), 2)

    def test_dynamic_ttl_and_eviction_bound(self):
        @cache.ttl_cache('history', ttl=lambda x: 0 if x == 'skip' else 60)
        def fetch(x):
            return object()

        self.assertIsNot(fetch('skip'), fetch('skip'))
        self.assertIs(fetch('a'), fetch('a'))
        with patch.object(cache, 'MAX_ENTRIES', 8):
            for i in range(50):
                fetch(i)

    def test_ohlcv_ttl_scales_with_candle_size(self):
        from finhub_mcp import services
        with patch('finhub_mcp.services.get_ttl', return_value=300):
            self.assertEqual(services._ohlcv_ttl('binance', 'BTC/USDT', '1m', 10), 15)
            self.assertEqual(services._ohlcv_ttl('binance', 'BTC/USDT', '1d', 10), 300)
            self.assertEqual(services._ohlcv_ttl('binance', 'BTC/USDT', 'bad', 10), 0)
        with patch('finhub_mcp.services.get_ttl', return_value=0):
            self.assertEqual(services._ohlcv_ttl('binance', 'BTC/USDT', '1h', 10), 0)


class ConfigCacheTests(unittest.TestCase):
    def test_reloads_only_when_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w') as f:
                json.dump({'crypto_exchanges': ['okx']}, f)
            with patch.dict(os.environ, {'FINHUB_CONFIG': path}):
                self.assertEqual(config.get_list('crypto_exchanges'), ('okx',))
                first = config._read_config()
                self.assertIs(config._read_config(), first)
                with open(path, 'w') as f:
                    json.dump({'crypto_exchanges': ['bybit', 'okx']}, f)
                os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
                self.assertEqual(config.get_list('crypto_exchanges'), ('bybit', 'okx'))

    def test_public_loaders_return_copies_and_validate(self):
        loaded = config.load_config()
        loaded['crypto_exchanges'].append('mutated')
        self.assertNotIn('mutated', config.get_list('crypto_exchanges'))
        config.get_dict('futures_default_types')['binance'] = 'mutated'
        self.assertNotEqual(config.get_dict('futures_default_types')['binance'], 'mutated')
        self.assertGreater(config.get_number('crypto_timeout_ms', 1), 0)
        self.assertEqual(config.get_ttl('no_such_kind'), 0)

    def test_invalid_ttl_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w') as f:
                json.dump({'cache_ttl_seconds': {'ticker': -1}, 'crypto_exchanges': []}, f)
            with patch.dict(os.environ, {'FINHUB_CONFIG': path}):
                with self.assertRaises(config.ConfigError):
                    config.get_ttl('ticker')
                with self.assertRaises(config.ConfigError):
                    config.get_list('crypto_exchanges')


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        providers.reset_cooldowns()

    def tearDown(self):
        providers.reset_cooldowns()

    def test_network_error_puts_provider_on_cooldown(self):
        calls = []

        def fn(provider):
            calls.append(provider)
            if provider == 'a':
                raise TimeoutError('timed out')
            return [1]

        self.assertEqual(providers.first_success(['a', 'b'], fn)[0], 'b')
        self.assertEqual(providers.first_success(['a', 'b'], fn)[0], 'b')
        self.assertEqual(calls, ['a', 'b', 'b'])

    def test_non_network_error_does_not_cool_down(self):
        calls = []

        def fn(provider):
            calls.append(provider)
            if provider == 'a':
                raise ValueError('bad symbol')
            return [1]

        providers.first_success(['a', 'b'], fn)
        providers.first_success(['a', 'b'], fn)
        self.assertEqual(calls, ['a', 'b', 'a', 'b'])

    def test_all_cooling_down_still_tries_everyone(self):
        def fn(provider):
            raise ConnectionError('down')

        with self.assertRaises(ProviderFallbackError):
            providers.first_success(['a', 'b'], fn)
        calls = []

        def fn2(provider):
            calls.append(provider)
            return [1]

        self.assertEqual(providers.first_success(['a', 'b'], fn2)[0], 'a')
        self.assertEqual(calls, ['a'])

    def test_skipped_provider_reported_in_errors(self):
        def fn(provider):
            raise TimeoutError('x') if provider == 'a' else RuntimeError('y')

        with self.assertRaises(ProviderFallbackError):
            providers.first_success(['a', 'b'], fn)
        with self.assertRaises(ProviderFallbackError) as caught:
            providers.first_success(['a', 'b'], lambda p: [] if p == 'b' else 1 / 0)
        self.assertIn('cooling down', caught.exception.provider_errors['a'])
        self.assertEqual(caught.exception.provider_errors['b'], 'no data')

    def test_cooldown_disabled_by_config(self):
        calls = []

        def fn(provider):
            calls.append(provider)
            raise TimeoutError('x')

        with patch('finhub_mcp.providers.get_number', return_value=0):
            for _ in range(2):
                with self.assertRaises(ProviderFallbackError):
                    providers.first_success(['a'], fn)
        self.assertEqual(calls, ['a', 'a'])

    def test_transient_classification_matches_ccxt_hierarchy(self):
        import ccxt
        self.assertTrue(providers.is_transient_error(ccxt.RequestTimeout('t')))
        self.assertTrue(providers.is_transient_error(ccxt.ExchangeNotAvailable('t')))
        self.assertFalse(providers.is_transient_error(ccxt.BadSymbol('t')))

    def test_transient_classification_unwraps_retry_and_cause_chains(self):
        from concurrent.futures import Future

        class RetryError(Exception):  # same shape as tenacity.RetryError, which vnstock raises
            def __init__(self, last_attempt):
                super().__init__('retries exhausted')
                self.last_attempt = last_attempt

        future = Future()
        future.set_exception(ConnectionError('reset'))
        self.assertTrue(providers.is_transient_error(RetryError(future)))
        bad = Future()
        bad.set_exception(ValueError('nope'))
        self.assertFalse(providers.is_transient_error(RetryError(bad)))
        try:
            try:
                raise TimeoutError('slow')
            except TimeoutError as inner:
                raise RuntimeError('wrapped') from inner
        except RuntimeError as outer:
            self.assertTrue(providers.is_transient_error(outer))
        self.assertFalse(providers.is_transient_error(None))


class ProviderHelperTests(unittest.TestCase):
    def test_exchange_instances_are_shared_and_have_timeout(self):
        first = providers.get_exchange('binance')
        self.assertIs(first, providers.get_exchange('BINANCE'))
        self.assertEqual(first.timeout, 10000)
        futures = providers.get_futures_exchange('binance')
        self.assertIsNot(first, futures)
        self.assertIs(futures, providers.get_futures_exchange('binance'))
        self.assertEqual(futures.options['defaultType'], 'future')
        with self.assertRaisesRegex(ValueError, 'Unsupported exchange'):
            providers.get_exchange('bogus')

    def test_clamp_and_symbol_validation(self):
        self.assertEqual(providers.clamp(5000, 1, 100), 100)
        self.assertEqual(providers.clamp('7', 1, 100), 7)
        with self.assertRaises(ValueError):
            providers.clamp('abc', 1, 100)
        self.assertEqual(providers.vn_symbol(' fpt '), 'FPT')
        for bad in ['', '  ', 'FP T', 'A' * 30, None]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    providers.vn_symbol(bad)
        with self.assertRaises(ValueError):
            providers.crypto_symbol('  ')

    def test_timeframe_validation(self):
        for bad in ['', 'm', '0m', '-5m', '5x']:
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, 'Invalid timeframe'):
                    providers.timeframe_to_ms(bad)

    def test_suppress_noise_restores_state_with_threads(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        disable_before = logging.root.manager.disable
        barrier = threading.Barrier(4)

        def worker():
            with providers.suppress_noise():
                barrier.wait(timeout=5)
                print('hidden')
                time.sleep(0.01)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIs(sys.stdout, original_stdout)
        self.assertIs(sys.stderr, original_stderr)
        self.assertEqual(logging.root.manager.disable, disable_before)

    def test_suppress_noise_swallows_output(self):
        with patch('sys.stdout') as real:
            with providers.suppress_noise():
                print('hidden')
            real.write.assert_not_called()


class ResponseTests(unittest.TestCase):
    def test_output_is_compact_and_drops_none_meta(self):
        text = success([{'a': 1}], source='VCI', warnings=None)
        self.assertNotIn('\n', text)
        self.assertNotIn(', ', text)
        self.assertEqual(json.loads(text)['meta'], {'source': 'VCI'})
        self.assertNotIn('meta', json.loads(success([1], warnings=None)))

    def test_unexpected_exception_is_tagged_internal(self):
        payload = json.loads(error(RuntimeError('boom')))
        self.assertEqual(payload['meta']['error_type'], 'internal')

    def test_non_json_values_do_not_crash(self):
        self.assertIn('2026', success({'when': datetime(2026, 1, 1)}))


class IndicatorAccuracyTests(unittest.TestCase):
    CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
              45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]

    def test_rsi_uses_wilder_smoothing(self):
        # Reference value from Wilder's worked example / StockCharts RSI walkthrough.
        self.assertAlmostEqual(ind.rsi(self.CLOSES, 14), 57.92, places=1)
        self.assertIsNone(ind.rsi(self.CLOSES[:14], 14))

    def test_ema_is_seeded_with_sma(self):
        self.assertEqual(ind.ema([1, 2, 3, 4, 5], 3), 4.0)
        self.assertEqual(ind.ema_series([1, 2, 3], 3), [2.0])
        self.assertIsNone(ind.ema([1, 2], 3))

    def test_macd_needs_enough_data_for_each_part(self):
        values = [float(v) for v in range(1, 60)]
        self.assertEqual(ind.macd(values[:25]), {'macd': None, 'signal': None, 'histogram': None})
        partial = ind.macd(values[:30])
        self.assertIsNotNone(partial['macd'])
        self.assertIsNone(partial['signal'])
        full = ind.macd(values)
        self.assertAlmostEqual(full['histogram'], full['macd'] - full['signal'])


class AlertPruneTests(unittest.TestCase):
    def setUp(self):
        self.old_file = alerts.ALERTS_FILE
        self.tmp = tempfile.TemporaryDirectory()
        alerts.set_alerts_file(os.path.join(self.tmp.name, 'alerts.json'))

    def tearDown(self):
        alerts.set_alerts_file(self.old_file)
        self.tmp.cleanup()

    def test_prunes_only_old_triggered_alerts(self):
        old, _ = alerts.add_alert('btc', 'above', 1)
        recent, _ = alerts.add_alert('eth', 'above', 1)
        pending, _ = alerts.add_alert('sol', 'above', 1)
        stored = alerts.load_alerts()
        stored[0].update(triggered=True, triggered_at=(datetime.now() - timedelta(days=40)).isoformat())
        stored[1].update(triggered=True, triggered_at=datetime.now().isoformat())
        alerts.save_alerts(stored)
        self.assertEqual(alerts.prune_triggered(30), 1)
        self.assertEqual([a['id'] for a in alerts.load_alerts()], [recent['id'], pending['id']])
        self.assertEqual(alerts.prune_triggered(0), 0)

    def test_rejects_empty_symbol(self):
        alert, message = alerts.add_alert('  ', 'above', 1)
        self.assertIsNone(alert)
        self.assertIn('Symbol', message)


class DaemonOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.old_file = alerts.ALERTS_FILE
        self.tmp = tempfile.TemporaryDirectory()
        alerts.set_alerts_file(os.path.join(self.tmp.name, 'alerts.json'))

    def tearDown(self):
        alerts.set_alerts_file(self.old_file)
        self.tmp.cleanup()

    def test_redacts_token_from_logged_errors(self):
        with patch.object(alert_daemon, 'TELEGRAM_TOKEN', 'SECRET123'):
            with patch('alert_daemon.requests.post', side_effect=RuntimeError('failed for /botSECRET123/sendMessage')):
                with patch('builtins.print') as printed:
                    self.assertFalse(alert_daemon.send_telegram('1', 'hi'))
        logged = ' '.join(str(call.args[0]) for call in printed.call_args_list)
        self.assertNotIn('SECRET123', logged)
        self.assertIn('***', logged)

    def test_prices_each_symbol_once_per_pass(self):
        for _ in range(3):
            alerts.add_alert('btc', 'above', 10**9)
        alerts.add_alert('BTC/USDT', 'below', 1)
        alerts.add_alert('fpt', 'above', 10**9, asset_type='vn_stock')
        with patch('alert_daemon.fetch_price', return_value=50_000.0) as fetch, patch('builtins.print'):
            alert_daemon.check_alerts()
        # 'btc' and 'BTC/USDT' differ as alert symbols, so two crypto lookups plus one VN lookup.
        self.assertEqual(fetch.call_count, 3)

    def test_failed_lookup_is_not_retried_for_other_alerts(self):
        for _ in range(3):
            alerts.add_alert('btc', 'above', 1)
        with patch('alert_daemon.fetch_price', side_effect=RuntimeError('down')) as fetch, patch('builtins.print'):
            alert_daemon.check_alerts()
        self.assertEqual(fetch.call_count, 1)

    def test_triggered_alert_is_sent_and_marked(self):
        alert, _ = alerts.add_alert('btc', 'above', 100)
        with patch('alert_daemon.fetch_price', return_value=150.0), \
                patch('alert_daemon.send_telegram', return_value=True) as send, patch('builtins.print'):
            alert_daemon.check_alerts()
        send.assert_called_once()
        self.assertTrue(alerts.load_alerts()[0]['triggered'])


class ToolBehaviourTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, name, arguments):
        result = await server.mcp.call_tool(name, arguments)
        return json.loads(result[1]['result'])

    async def test_list_responses_carry_provider_info_in_meta(self):
        candles = [[1_700_000_000_000 + i * 86_400_000, 1, 2, 0.5, 1.5, 10] for i in range(3)]
        with patch('finhub_mcp.tools.crypto.fetch_crypto_ohlcv', return_value=candles):
            payload = await self.call('get_crypto_history', {'symbol': 'btc', 'limit': 3, 'exchange': 'okx,binance'})
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(len(payload['data']), 3)
        self.assertNotIn('exchange', payload['data'][0])
        self.assertEqual(payload['meta']['exchange'], 'okx')
        self.assertFalse(payload['meta']['fallback_used'])
        self.assertEqual(payload['meta']['symbol'], 'BTC/USDT')

    async def test_limit_is_clamped_before_reaching_exchange(self):
        seen = {}

        def fake(exchange, symbol, timeframe, limit):
            seen['limit'] = limit
            return [[1_700_000_000_000, 1, 2, 0.5, 1.5, 10]]

        with patch('finhub_mcp.tools.crypto.fetch_crypto_ohlcv', side_effect=fake):
            await self.call('get_crypto_history', {'symbol': 'btc', 'limit': 10**6})
        self.assertEqual(seen['limit'], 1000)

    async def test_invalid_timeframe_is_a_validation_error(self):
        payload = await self.call('get_crypto_history', {'symbol': 'btc', 'timeframe': 'banana'})
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['meta']['error_type'], 'validation')

    async def test_unexpected_exception_becomes_error_envelope(self):
        with patch('finhub_mcp.tools.crypto.fetch_crypto_ticker', side_effect=RuntimeError('kaput')):
            payload = await self.call('get_crypto_price', {'symbol': 'btc', 'exchange': 'okx'})
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['meta']['error_type'], 'provider_failure')
        self.assertIn('kaput', payload['meta']['provider_errors']['okx'])

    async def test_vn_symbol_validation(self):
        payload = await self.call('get_vn_stock_price', {'symbol': 'FP T'})
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['meta']['error_type'], 'validation')

    async def test_market_overview_runs_in_parallel_and_reports_partial_failure(self):
        started = []

        def fake(index, sources):
            started.append(index)
            if index == 'VN30':
                raise RuntimeError('no data')
            time.sleep(0.2)
            return {'close': 1.0, 'source': sources[0]}

        began = time.monotonic()
        with patch('finhub_mcp.tools.vn_stock.vn_index_snapshot', side_effect=fake):
            payload = await self.call('get_vn_market_overview', {})
        elapsed = time.monotonic() - began
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(set(payload['data']), {'VNINDEX', 'HNXINDEX'})
        self.assertIn('VN30', payload['meta']['warnings'])
        self.assertLess(elapsed, 0.38)

    async def test_market_overview_all_failed(self):
        with patch('finhub_mcp.tools.vn_stock.vn_index_snapshot', side_effect=RuntimeError('down')):
            payload = await self.call('get_vn_market_overview', {})
        self.assertFalse(payload['ok'])
        self.assertIn('all market indices failed', payload['error'])

    async def test_search_treats_query_literally(self):
        listing = pd.DataFrame({'symbol': ['FPT', 'VCB'], 'organ_name': ['FPT (Corp)', 'Vietcombank']})
        with patch('finhub_mcp.tools.vn_stock.fetch_vn_listing', return_value=listing):
            payload = await self.call('search_vn_stock', {'query': '(corp'})
            empty = await self.call('search_vn_stock', {'query': '  '})
        self.assertTrue(payload['ok'], payload)
        self.assertEqual([row['symbol'] for row in payload['data']], ['FPT'])
        self.assertFalse(empty['ok'])

    async def test_slow_tool_does_not_block_other_calls(self):
        def slow(exchange, symbol):
            time.sleep(0.3)
            return {'last': 1, 'bid': 1, 'ask': 1, 'high': 1, 'low': 1, 'baseVolume': 1}

        import asyncio
        with patch('finhub_mcp.tools.crypto.fetch_crypto_ticker', side_effect=slow):
            began = time.monotonic()
            await asyncio.gather(*(self.call('get_crypto_price', {'symbol': s, 'exchange': 'okx'})
                                   for s in ['a', 'b', 'c']))
        self.assertLess(time.monotonic() - began, 0.8)


if __name__ == '__main__':
    unittest.main()
