# Copyright (c) 2026 bachbnt. All rights reserved.

import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import alert_daemon
import server
from finhub_mcp import alerts, config, providers
from finhub_mcp import indicators as indicator_lib
from finhub_mcp.exceptions import ProviderFallbackError
from finhub_mcp.responses import error, invalid, success


class ResponseTests(unittest.TestCase):
    def test_success_schema(self):
        payload = json.loads(success({'symbol': 'BTC'}))
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['data'], {'symbol': 'BTC'})
        self.assertNotIn('error', payload)

    def test_error_schema(self):
        payload = json.loads(error('boom', error_type='runtime'))
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error'], 'boom')
        self.assertEqual(payload['meta']['error_type'], 'runtime')

    def test_provider_error_schema(self):
        exc = ProviderFallbackError('All providers failed', {'binance': 'timeout'})
        payload = json.loads(error(exc))
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['meta']['error_type'], 'provider_failure')
        self.assertTrue(payload['meta']['retryable'])
        self.assertEqual(payload['meta']['provider_errors'], {'binance': 'timeout'})

    def test_invalid_schema(self):
        payload = json.loads(invalid('bad input'))
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error'], 'bad input')
        self.assertEqual(payload['meta']['error_type'], 'validation')


class ProviderTests(unittest.TestCase):
    def test_provider_order_auto(self):
        self.assertEqual(
            providers.provider_order('auto', providers.SUPPORTED_EXCHANGES, 'crypto exchange'),
            list(providers.SUPPORTED_EXCHANGES),
        )

    def test_provider_order_dedupes_and_canonicalizes(self):
        self.assertEqual(
            providers.provider_order('okx, binance, OKX', providers.SUPPORTED_EXCHANGES, 'crypto exchange'),
            ['okx', 'binance'],
        )

    def test_provider_order_rejects_unknown(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported crypto exchange'):
            providers.provider_order('bogus', providers.SUPPORTED_EXCHANGES, 'crypto exchange')

    def test_crypto_symbol_defaults_to_usdt(self):
        self.assertEqual(providers.crypto_symbol('btc'), 'BTC/USDT')
        self.assertEqual(providers.crypto_symbol('eth/usdc'), 'ETH/USDC')

    def test_timeframe_to_ms(self):
        self.assertEqual(providers.timeframe_to_ms('5m'), 300_000)
        self.assertEqual(providers.timeframe_to_ms('1h'), 3_600_000)
        with self.assertRaisesRegex(ValueError, 'Invalid timeframe'):
            providers.timeframe_to_ms('x')

    def test_first_success_skips_empty_results(self):
        provider, result = providers.first_success(
            ['a', 'b'],
            lambda src: [] if src == 'a' else [1],
        )
        self.assertEqual(provider, 'b')
        self.assertEqual(result, [1])

    def test_first_success_exposes_provider_errors(self):
        with self.assertRaises(ProviderFallbackError) as caught:
            providers.first_success(['a', 'b'], lambda src: [])
        self.assertEqual(caught.exception.provider_errors, {'a': 'no data', 'b': 'no data'})


class ConfigTests(unittest.TestCase):
    def test_load_config_merges_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w') as f:
                json.dump({'crypto_exchanges': ['okx'], 'cache_ttl_seconds': {'ticker': 3}}, f)
            with patch.dict(os.environ, {'FINHUB_CONFIG': path}):
                loaded = config.load_config()
        self.assertEqual(loaded['crypto_exchanges'], ['okx'])
        self.assertEqual(loaded['cache_ttl_seconds']['ticker'], 3)
        self.assertIn('history', loaded['cache_ttl_seconds'])


class IndicatorTests(unittest.TestCase):
    def test_parse_indicators(self):
        self.assertEqual(indicator_lib.parse_indicators('rsi, sma, RSI'), ['rsi', 'sma'])
        with self.assertRaisesRegex(ValueError, 'Unsupported indicator'):
            indicator_lib.parse_indicators('rsi,nope')

    def test_calculate_indicators(self):
        closes = [float(value) for value in range(1, 61)]
        data = indicator_lib.calculate_indicators(closes, ['sma', 'ema', 'rsi', 'macd', 'bollinger'])
        self.assertEqual(data['sma']['20'], 50.5)
        self.assertIn('12', data['ema'])
        self.assertEqual(data['rsi']['14'], 100.0)
        self.assertIn('histogram', data['macd'])
        self.assertIn('upper', data['bollinger'])


class AlertStoreTests(unittest.TestCase):
    def setUp(self):
        self.old_file = alerts.ALERTS_FILE
        self.old_lock = alerts.ALERTS_LOCK_FILE
        self.tmp = tempfile.TemporaryDirectory()
        alerts.set_alerts_file(os.path.join(self.tmp.name, 'alerts.json'))

    def tearDown(self):
        alerts.set_alerts_file(self.old_file)
        alerts.ALERTS_LOCK_FILE = self.old_lock
        self.tmp.cleanup()

    def test_add_alert_validates_and_normalizes(self):
        alert, validation_error = alerts.add_alert('btc', 'ABOVE', '100', asset_type='CRYPTO')
        self.assertIsNone(validation_error)
        self.assertEqual(alert['symbol'], 'BTC')
        self.assertEqual(alert['condition'], 'above')
        self.assertEqual(alert['asset_type'], 'crypto')
        self.assertEqual(alert['price'], 100.0)
        self.assertEqual(alerts.load_alerts(), [alert])

    def test_add_alert_rejects_invalid_values(self):
        for args in [
            ('btc', 'sideways', 100, 'crypto'),
            ('btc', 'above', 100, 'bond'),
            ('btc', 'above', 0, 'crypto'),
            ('btc', 'above', 'nope', 'crypto'),
        ]:
            with self.subTest(args=args):
                alert, validation_error = alerts.add_alert(*args)
                self.assertIsNone(alert)
                self.assertIsNotNone(validation_error)
        self.assertEqual(alerts.load_alerts(), [])

    def test_remove_alert_and_mark_triggered(self):
        alert, validation_error = alerts.add_alert('eth', 'below', 2000)
        self.assertIsNone(validation_error)
        self.assertFalse(alerts.remove_alert('missing'))
        self.assertTrue(alerts.mark_alert_triggered(alert, 1999.5))
        stored = alerts.load_alerts()[0]
        self.assertTrue(stored['triggered'])
        self.assertEqual(stored['triggered_price'], 1999.5)
        self.assertTrue(alerts.remove_alert(alert['id']))
        self.assertEqual(alerts.load_alerts(), [])


class AlertDaemonTests(unittest.TestCase):
    def test_send_telegram_retries_on_http_error(self):
        old_token = alert_daemon.TELEGRAM_TOKEN
        alert_daemon.TELEGRAM_TOKEN = 'fake-token'
        try:
            with patch('alert_daemon.requests.post') as post:
                response = Mock()
                response.raise_for_status.side_effect = Exception('401 unauthorized')
                post.return_value = response
                with patch('builtins.print'):
                    self.assertFalse(alert_daemon.send_telegram('123', 'hello'))
        finally:
            alert_daemon.TELEGRAM_TOKEN = old_token

    def test_send_telegram_log_only_without_token(self):
        old_token = alert_daemon.TELEGRAM_TOKEN
        alert_daemon.TELEGRAM_TOKEN = ''
        try:
            with patch('builtins.print'):
                self.assertTrue(alert_daemon.send_telegram('', 'hello'))
        finally:
            alert_daemon.TELEGRAM_TOKEN = old_token


class McpRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_registers_expected_tools(self):
        tools = await server.mcp.list_tools()
        names = {tool.name for tool in tools}
        self.assertEqual(len(names), 38)
        self.assertIn('get_crypto_price', names)
        self.assertIn('get_crypto_indicators', names)
        self.assertIn('get_vn_stock_price', names)
        self.assertIn('get_vn_stock_indicators', names)
        self.assertIn('add_alert', names)
        for name in ('get_crypto_prices', 'get_vn_top_movers', 'get_world_metal_price', 'get_market_news'):
            self.assertIn(name, names)

    async def test_tool_error_uses_standard_schema(self):
        result = await server.mcp.call_tool('get_vn_market_overview', {'source': 'bad'})
        payload = json.loads(result[1]['result'])
        self.assertFalse(payload['ok'])
        self.assertIn('Unsupported VN quote source', payload['error'])


if __name__ == '__main__':
    unittest.main()
