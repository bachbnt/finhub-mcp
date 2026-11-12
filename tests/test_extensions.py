# Copyright (c) 2026 bachbnt. All rights reserved.

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
from defusedxml import DefusedXmlException as ElementTreeError

import server
from finhub_mcp import derivatives, news, providers, vnboard, worldmarkets
from finhub_mcp import indicators as ind
from finhub_mcp.config import ConfigError
from finhub_mcp.exceptions import ProviderFallbackError
from finhub_mcp.tools.world import split_symbols

NOW = datetime.now(timezone.utc)


def rfc822(dt):
    return dt.strftime('%a, %d %b %Y %H:%M:%S +0000')


def rss(*items):
    body = ''.join(
        f'<item><title>{t}</title><link>https://x/{i}</link><pubDate>{rfc822(when)}</pubDate>'
        f'<description><![CDATA[<p>{d}</p><img src="a.png"/>]]></description></item>'
        for i, (t, when, d) in enumerate(items)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'.encode()


class NewIndicatorTests(unittest.TestCase):
    def test_parse_indicators_sets(self):
        self.assertEqual(ind.parse_indicators('auto'), list(ind.DEFAULT_INDICATORS))
        self.assertEqual(ind.parse_indicators('all'), list(ind.SUPPORTED_INDICATORS))
        self.assertEqual(ind.parse_indicators('atr, SIGNALS, atr'), ['atr', 'signals'])
        self.assertNotIn('atr', ind.DEFAULT_INDICATORS)

    def test_ohlcv_indicators_require_aligned_inputs(self):
        with self.assertRaisesRegex(ValueError, 'need aligned'):
            ind.calculate_indicators([1.0, 2.0], ['atr'])
        with self.assertRaisesRegex(ValueError, 'need aligned'):
            ind.calculate_indicators([1.0, 2.0], ['vwap'], highs=[1.0], lows=[1.0], volumes=[1.0])

    def test_atr_with_constant_range(self):
        closes = [10.0] * 20
        value = ind.atr([11.0] * 20, [9.0] * 20, closes, 14)
        self.assertAlmostEqual(value['14'], 2.0)
        self.assertAlmostEqual(value['pct_of_close'], 20.0)
        self.assertIsNone(ind.atr([1.0] * 10, [1.0] * 10, [1.0] * 10, 14)['14'])

    def test_stochastic_extremes(self):
        closes = [float(i) for i in range(1, 31)]
        highs, lows = [c + 0.5 for c in closes], [c - 0.5 for c in closes]
        up = ind.stochastic(highs, lows, closes)
        self.assertGreater(up['k'], 80)
        self.assertEqual(up['state'], 'overbought')
        down = ind.stochastic(highs[::-1], lows[::-1], closes[::-1])
        self.assertEqual(down['state'], 'oversold')
        self.assertIsNone(ind.stochastic([1.0], [1.0], [1.0])['k'])

    def test_vwap_and_volume_ma(self):
        result = ind.vwap([2.0, 4.0], [2.0, 4.0], [2.0, 4.0], [1.0, 3.0])
        self.assertAlmostEqual(result['vwap'], 3.5)
        self.assertAlmostEqual(result['close_vs_vwap_pct'], (4 / 3.5 - 1) * 100)
        self.assertIsNone(ind.vwap([1.0], [1.0], [1.0], [0.0])['vwap'])
        vol = ind.volume_ma([10.0] * 19 + [30.0])
        self.assertEqual(vol['20'], 11.0)
        self.assertEqual(vol['50'], None)
        self.assertAlmostEqual(vol['last_vs_ma_ratio'], 30 / 11)

    def test_support_resistance_finds_levels_around_price(self):
        # Zig-zag between ~100 and ~110 so both become touched levels, last close mid-range.
        closes = [100, 105, 110, 105, 100, 105, 110, 105, 100, 105, 110, 105, 100, 105, 107.0]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        data = ind.support_resistance([float(h) for h in highs], [float(low) for low in lows],
                                      [float(c) for c in closes], window=2, tolerance_pct=1.0)
        self.assertTrue(data['support'] and data['resistance'])
        self.assertLess(data['support'][0]['level'], 107)
        self.assertGreater(data['resistance'][0]['level'], 107)
        self.assertGreaterEqual(data['resistance'][0]['touches'], 2)
        pivots = data['pivot_points']
        self.assertAlmostEqual(pivots['pivot'], (108 + 106 + 107) / 3)
        self.assertEqual(ind.support_resistance([1.0], [1.0], [1.0])['support'], [])

    def test_last_cross_detects_direction_and_recency(self):
        up = ind.last_cross([1, 2, 3, 6], [3, 3, 3, 3])
        self.assertEqual((up['cross'], up['candles_ago'], up['state']), ('up', 0, 'bullish'))
        down = ind.last_cross([5, 4, 2, 1], [3, 3, 3, 3])
        self.assertEqual((down['cross'], down['candles_ago'], down['state']), ('down', 1, 'bearish'))
        old = ind.last_cross([1, 9, 9, 9, 9, 9, 9], [5] * 7, lookback=3)
        self.assertIsNone(old['cross'])
        self.assertEqual(old['state'], 'bullish')
        self.assertIsNone(ind.last_cross([1], [1])['state'])

    def test_golden_cross_is_detected(self):
        # 200 flat-ish candles then a sharp rally so SMA50 crosses above SMA200 recently.
        closes = [100.0] * 200 + [100.0 + 4 * i for i in range(1, 41)]
        sma50, sma200 = ind.sma_series(closes, 50), ind.sma_series(closes, 200)
        crossed_at = None
        for ago in range(len(closes)):
            data = ind.signals(closes[:len(closes) - ago], lookback=1)
            if data['sma_50_200'] and data['sma_50_200']['event'] == 'golden_cross':
                crossed_at = ago
                break
        self.assertIsNotNone(crossed_at)
        latest = ind.signals(closes)
        self.assertEqual(latest['sma_50_200']['state'], 'bullish')
        self.assertTrue(latest['trend']['above_sma200'])
        self.assertGreater(latest['bias']['score'], 0)
        self.assertEqual((len(sma50), len(sma200)), (len(closes) - 49, len(closes) - 199))

    def test_signals_with_short_history_are_null_not_errors(self):
        data = ind.signals([float(i) for i in range(1, 25)])
        self.assertIsNone(data['sma_50_200'])
        self.assertIsNone(data['macd'])
        self.assertIsNone(data['trend']['above_sma50'])
        self.assertIn(data['bias']['label'], {'bullish', 'bearish', 'neutral'})
        self.assertEqual(ind.signals([])['bias']['score'], 0)

    def test_calculate_all_indicators_end_to_end(self):
        closes = [100 + (i % 7) + i * 0.1 for i in range(260)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        volumes = [1000.0 + i for i in range(260)]
        data = ind.calculate_indicators(closes, list(ind.SUPPORTED_INDICATORS), highs, lows, volumes)
        self.assertEqual(set(data), set(ind.SUPPORTED_INDICATORS))
        json.dumps(data)  # must be JSON-serializable

    def test_macd_series_alignment_matches_macd(self):
        values = [float(i % 11) + i for i in range(80)]
        macd_line, signal_line = ind.macd_series(values)
        full = ind.macd(values)
        self.assertAlmostEqual(full['macd'], macd_line[-1])
        self.assertAlmostEqual(full['signal'], signal_line[-1])
        self.assertEqual(len(macd_line), 80 - 26 + 1)


class WorldMarketTests(unittest.TestCase):
    def setUp(self):
        providers.reset_cooldowns()

    tearDown = setUp

    def test_parse_pair(self):
        for raw in ('eurusd', 'EUR/USD', 'eur-usd', ' EUR_USD '):
            self.assertEqual(worldmarkets.parse_pair(raw), ('EUR', 'USD'))
        for bad in ('', 'EUR', 'EURUSDX', 'EUREUR', '123USD', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                worldmarkets.parse_pair(bad)

    def test_parse_metal(self):
        self.assertEqual(worldmarkets.parse_metal('gold'), 'XAU')
        self.assertEqual(worldmarkets.parse_metal(' xag '), 'XAG')
        with self.assertRaisesRegex(ValueError, 'Unsupported metal'):
            worldmarkets.parse_metal('copper')

    def test_history_days_clamps_to_yahoo_limits(self):
        self.assertEqual(worldmarkets.history_days(100, '1m'), 7)
        self.assertEqual(worldmarkets.history_days(500, '5m'), 59)
        self.assertEqual(worldmarkets.history_days(1000, '1d'), 1000)
        self.assertEqual(worldmarkets.history_days(0, '1d'), 1)
        with self.assertRaisesRegex(ValueError, 'Invalid interval'):
            worldmarkets.history_days(5, '2h')

    def test_gold_conversion_to_vnd_per_luong(self):
        # 1 lượng = 37.5 g; 1 oz = 31.1034768 g.
        value = worldmarkets.usd_per_oz_to_vnd_per_luong(3100.0, 25000.0)
        self.assertAlmostEqual(value, 3100.0 * 25000.0 * 37.5 / 31.1034768)

    def test_forex_prefers_yahoo_and_computes_change(self):
        quote = {'price': 1.1, 'previous_close': 1.0, 'as_of': 'T'}
        with patch('finhub_mcp.worldmarkets.yahoo_quote', return_value=quote):
            data = worldmarkets.forex_rate('eurusd')
        self.assertEqual((data['pair'], data['source'], data['fallback_used']), ('EUR/USD', 'YAHOO', False))
        self.assertAlmostEqual(data['change_pct'], 10.0)

    def test_forex_falls_back_to_er_api(self):
        payload = {'result': 'success', 'rates': {'VND': 25000.0}, 'time_last_update_unix': 1_700_000_000}
        with patch('finhub_mcp.worldmarkets.yahoo_quote', side_effect=RuntimeError('blocked')), \
                patch('finhub_mcp.worldmarkets.er_api_rates', return_value=payload):
            data = worldmarkets.forex_rate('USDVND')
        self.assertEqual((data['source'], data['fallback_used'], data['rate']), ('ER-API', True, 25000.0))
        self.assertIsNone(data['change'])

    def test_forex_unknown_currency_reports_all_providers(self):
        with patch('finhub_mcp.worldmarkets.yahoo_quote', side_effect=ValueError('no data')), \
                patch('finhub_mcp.worldmarkets.er_api_rates', return_value={'result': 'success', 'rates': {}}):
            with self.assertRaises(ProviderFallbackError) as caught:
                worldmarkets.forex_rate('XXXYYY')
        self.assertEqual(set(caught.exception.provider_errors), {'YAHOO', 'ER-API'})

    def test_metal_price_spot_then_futures_fallback(self):
        with patch('finhub_mcp.worldmarkets.gold_api_price', return_value={'price': 4000, 'updatedAt': 'T'}):
            spot = worldmarkets.metal_price('gold')
        self.assertEqual((spot['source'], spot['basis'], spot['price']), ('GOLD-API', 'spot', 4000.0))
        quote = {'price': 4010.0, 'previous_close': 4000.0, 'as_of': 'T'}
        with patch('finhub_mcp.worldmarkets.gold_api_price', side_effect=RuntimeError('x')), \
                patch('finhub_mcp.worldmarkets.yahoo_quote', return_value=quote):
            fut = worldmarkets.metal_price('XAU')
        self.assertEqual((fut['source'], fut['fallback_used']), ('YAHOO', True))
        self.assertIn('GC=F', fut['basis'])
        self.assertAlmostEqual(fut['change_pct'], 0.25)

    def test_yahoo_history_parsing_skips_null_candles(self):
        result = {
            'timestamp': [1_700_000_000, 1_700_086_400, 1_700_172_800],
            'indicators': {'quote': [{
                'open': [1, None, 3], 'high': [2, None, 4], 'low': [0.5, None, 2.5],
                'close': [1.5, None, 3.5], 'volume': [10, None, 30],
            }]},
        }
        with patch('finhub_mcp.worldmarkets._chart_result', return_value=result):
            rows = worldmarkets.yahoo_history.__wrapped__('EURUSD=X', 5, '1d')
        self.assertEqual([r['close'] for r in rows], [1.5, 3.5])
        self.assertEqual(rows[0]['time'], '2023-11-14')

    def test_yahoo_chart_error_is_raised(self):
        with patch('finhub_mcp.worldmarkets.get_json', return_value={'chart': {'error': {'description': 'bad'}}}):
            with self.assertRaisesRegex(ValueError, 'Yahoo: bad'):
                worldmarkets._chart_result('X', {})
        with patch('finhub_mcp.worldmarkets.get_json', return_value={'chart': {'result': None, 'error': None}}):
            with self.assertRaisesRegex(ValueError, 'no data'):
                worldmarkets._chart_result('X', {})

    def test_split_symbols(self):
        self.assertEqual(split_symbols('btc, eth  sol,btc', 10), ['btc', 'eth', 'sol'])
        self.assertEqual(split_symbols(['A', ' B ', 'A'], 10), ['A', 'B'])
        with self.assertRaises(ValueError):
            split_symbols('', 10)
        with self.assertRaisesRegex(ValueError, 'Too many'):
            split_symbols('a,b,c', 2)


class NewsParsingTests(unittest.TestCase):
    def test_parse_rss_strips_html_and_parses_dates(self):
        when = NOW - timedelta(hours=1)
        articles = news.parse_feed(rss(('Hello &amp; welcome', when, 'Body <b>text</b> here')))
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['title'], 'Hello & welcome')
        self.assertEqual(articles[0]['summary'], 'Body text here')
        self.assertAlmostEqual(articles[0]['published'].timestamp(), when.timestamp(), delta=1)

    def test_parse_atom(self):
        xml = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom item</title>'
               b'<link href="https://a/1"/><updated>2026-11-12T01:00:00Z</updated>'
               b'<summary>Sum</summary></entry></feed>')
        article = news.parse_feed(xml)[0]
        self.assertEqual((article['title'], article['link'], article['summary']), ('Atom item', 'https://a/1', 'Sum'))
        self.assertEqual(article['published'].hour, 1)

    def test_parse_skips_untitled_and_rejects_entity_bombs(self):
        self.assertEqual(news.parse_feed(b'<rss><channel><item><title> </title></item></channel></rss>'), [])
        bomb = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                b'<rss><channel><item><title>&b;</title></item></channel></rss>')
        with self.assertRaises(ElementTreeError):
            news.parse_feed(bomb)

    def test_summary_is_truncated(self):
        long_text = 'x' * 1000
        article = news.parse_feed(rss(('T', NOW, long_text)))[0]
        self.assertEqual(len(article['summary']), news.SUMMARY_CHARS)

    def test_fold_and_query_matching(self):
        self.assertEqual(news.fold('Cổ phiếu Đà Nẵng'), 'co phieu da nang')
        article = {'title': 'Cổ phiếu FPT tăng mạnh', 'summary': 'Ngân hàng khác'}
        self.assertTrue(news.matches_query(article, 'co phieu fpt'))
        self.assertTrue(news.matches_query(article, 'ngan hang'))
        self.assertTrue(news.matches_query(article, ''))
        self.assertFalse(news.matches_query(article, 'fp'))  # whole words only
        self.assertFalse(news.matches_query(article, 'vcb'))


class SentimentTests(unittest.TestCase):
    def label(self, title, summary=''):
        return news.score_article({'title': title, 'summary': summary})

    def test_vietnamese_phrases(self):
        self.assertEqual(self.label('VN-Index giảm mạnh, nhà đầu tư bán tháo')['label'], 'negative')
        self.assertEqual(self.label('Cổ phiếu tăng trần, thanh khoản kỷ lục')['label'], 'positive')
        self.assertEqual(self.label('Hôm nay thị trường đi ngang')['label'], 'neutral')

    def test_phrase_overrides_and_diacritics(self):
        self.assertEqual(self.label('Công ty giảm lỗ quý 3')['label'], 'positive')  # "giảm lỗ" beats giảm/lỗ
        self.assertEqual(self.label('Ngân hàng tăng lãi suất huy động')['label'], 'negative')
        self.assertEqual(self.label('Giám đốc điều hành phát biểu')['score'], 0.0)  # giám != giảm
        self.assertEqual(self.label('Lãi suất liên ngân hàng đi ngang')['score'], 0.0)

    def test_english_words_and_wildcards(self):
        self.assertEqual(self.label('Bitcoin surges to record high as ETF inflows rally')['label'], 'positive')
        self.assertEqual(self.label('Exchange hacked, $50M liquidated amid crash')['label'], 'negative')
        self.assertEqual(self.label('Quarterly report published')['score'], 0.0)

    def test_headline_counts_double(self):
        mixed = self.label('Shares surge', 'but analysts warn of a crash and losses')
        self.assertGreater(mixed['score'], -1)
        self.assertEqual(self.label('Bitcoin crash', 'analysts see recovery ahead')['label'], 'negative')

    def test_summarize_sentiment(self):
        articles = [{'sentiment': {'score': 1.0, 'label': 'positive'}},
                    {'sentiment': {'score': -1.0, 'label': 'negative'}},
                    {'sentiment': {'score': 0.0, 'label': 'neutral'}},
                    {'sentiment': {'score': 1.0, 'label': 'positive'}}]
        summary = news.summarize_sentiment(articles)
        self.assertEqual((summary['positive'], summary['neutral'], summary['negative']), (2, 1, 1))
        self.assertEqual(summary['score'], 0.25)
        self.assertEqual(summary['label'], 'positive')
        empty = news.summarize_sentiment([])
        self.assertEqual((empty['articles'], empty['label']), (0, None))


class NewsFeedConfigTests(unittest.TestCase):
    def write_config(self, tmp, feeds):
        path = os.path.join(tmp, 'config.json')
        with open(path, 'w') as f:
            json.dump({'news_feeds': feeds}, f)
        return path

    def test_defaults_include_requested_feeds(self):
        feeds = news.get_feeds()
        self.assertIn('cafef_stock', feeds)
        self.assertIn('vnexpress_business', feeds)
        self.assertEqual(feeds['cafef_stock']['lang'], 'vi')

    def test_select_feeds_by_group_key_and_unknown(self):
        self.assertEqual(set(news.select_feeds('auto')), set(news.get_feeds()))
        self.assertTrue(all(f['group'] == 'crypto' for f in news.select_feeds('crypto').values()))
        self.assertEqual(list(news.select_feeds('decrypt, cafef_stock')), ['decrypt', 'cafef_stock'])
        with self.assertRaisesRegex(ValueError, 'Unknown news source'):
            news.select_feeds('nope')

    def test_custom_feed_added_and_default_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {
                'mine': {'url': 'https://example.com/rss', 'name': 'Mine', 'group': 'custom'},
                'cafef_stock': {'enabled': False},
            })
            with patch.dict(os.environ, {'FINHUB_CONFIG': path}):
                feeds = news.get_feeds()
        self.assertIn('mine', feeds)
        self.assertNotIn('cafef_stock', feeds)
        self.assertEqual(feeds['mine']['lang'], 'en')

    def test_invalid_feed_url_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {'bad': {'url': 'ftp://nope'}})
            with patch.dict(os.environ, {'FINHUB_CONFIG': path}):
                with self.assertRaises(ConfigError):
                    news.get_feeds()


class CollectNewsTests(unittest.TestCase):
    def setUp(self):
        providers.reset_cooldowns()
        self.feeds = {
            'a': {'name': 'Feed A', 'url': 'https://a', 'group': 'vn', 'lang': 'vi'},
            'b': {'name': 'Feed B', 'url': 'https://b', 'group': 'crypto', 'lang': 'en'},
        }
        recent, old = NOW - timedelta(hours=2), NOW - timedelta(hours=100)
        self.articles = {
            'https://a': [
                {'title': 'FPT tăng mạnh', 'link': 'https://l/1', 'published': recent, 'summary': ''},
                {'title': 'Tin cũ', 'link': 'https://l/2', 'published': old, 'summary': ''},
                {'title': 'Không có ngày', 'link': 'https://l/3', 'published': None, 'summary': ''},
            ],
            'https://b': [
                {'title': 'Bitcoin rallies', 'link': 'https://l/4', 'published': NOW - timedelta(hours=1), 'summary': ''},
                {'title': 'FPT tăng mạnh', 'link': 'https://l/1', 'published': recent, 'summary': ''},  # duplicate
            ],
        }

    tearDown = lambda self: providers.reset_cooldowns()  # noqa: E731

    def collect(self, **kwargs):
        with patch('finhub_mcp.news.select_feeds', return_value=self.feeds), \
                patch('finhub_mcp.news.fetch_feed', side_effect=lambda url: self.articles[url]):
            return news.collect_news('auto', **kwargs)

    def test_filters_by_age_dedupes_and_sorts_newest_first(self):
        articles, warnings = self.collect(hours=48)
        self.assertEqual(warnings, {})
        self.assertEqual([a['link'] for a in articles], ['https://l/4', 'https://l/1', 'https://l/3'])
        self.assertEqual(articles[0]['source'], 'Feed B')

    def test_query_filter(self):
        articles, _ = self.collect(query='fpt', hours=48)
        self.assertEqual([a['link'] for a in articles], ['https://l/1'])

    def test_partial_failure_becomes_warning(self):
        def fetch(url):
            if url == 'https://a':
                raise ConnectionError('reset')
            return self.articles[url]

        with patch('finhub_mcp.news.select_feeds', return_value=self.feeds), \
                patch('finhub_mcp.news.fetch_feed', side_effect=fetch):
            articles, warnings = news.collect_news('auto')
        self.assertTrue(articles and all(a['feed'] == 'b' for a in articles))
        self.assertIn('a', warnings)
        # The failed feed is now cooling down and is skipped without another request.
        with patch('finhub_mcp.news.select_feeds', return_value=self.feeds), \
                patch('finhub_mcp.news.fetch_feed', side_effect=fetch) as again:
            _, warnings = news.collect_news('auto')
        self.assertIn('cooling down', warnings['a'])
        self.assertEqual(again.call_count, 1)

    def test_all_feeds_failing_raises(self):
        with patch('finhub_mcp.news.select_feeds', return_value=self.feeds), \
                patch('finhub_mcp.news.fetch_feed', side_effect=RuntimeError('boom')):
            with self.assertRaises(ProviderFallbackError) as caught:
                news.collect_news('auto')
        self.assertEqual(set(caught.exception.provider_errors), {'a', 'b'})

    def test_no_match_is_not_a_failure(self):
        articles, warnings = self.collect(query='zzzz')
        self.assertEqual((articles, warnings), ([], {}))


class DerivativesTests(unittest.TestCase):
    @staticmethod
    def market(symbol, **kwargs):
        base = {'symbol': symbol, 'contract': True, 'active': True, 'base': 'BTC', 'quote': 'USDT',
                'swap': False, 'future': False, 'linear': True, 'expiry': None}
        return {**base, **kwargs}

    def test_perp_symbol_and_split(self):
        self.assertEqual(derivatives.perp_symbol('BTC/USDT'), 'BTC/USDT:USDT')
        self.assertEqual(derivatives.split_pair('ETH/USDC'), ('ETH', 'USDC'))

    def test_select_contracts_prefers_linear_and_nearest_expiries(self):
        m = self.market
        markets = [
            m('BTC/USDT:USDT', swap=True),
            m('BTC/USD:BTC', swap=True, quote='USD', linear=False),
            m('BTC/USDT:USDT-270326', future=True, expiry=3),
            m('BTC/USDT:USDT-261225', future=True, expiry=2),
            m('BTC/USD:BTC-261225', future=True, expiry=2, quote='USD', linear=False),
            m('BTC/USDT:USDT-260101', future=True, expiry=1, active=False),
            m('ETH/USDT:USDT', swap=True, base='ETH'),
            {'symbol': 'BTC/USDT', 'contract': False, 'base': 'BTC', 'quote': 'USDT', 'spot': True},
        ]
        chosen = derivatives.select_contracts(markets, 'BTC', 'USDT')
        self.assertEqual(chosen['perpetual']['symbol'], 'BTC/USDT:USDT')
        self.assertEqual([c['symbol'] for c in chosen['dated']], ['BTC/USDT:USDT-261225', 'BTC/USDT:USDT-270326'])

    def test_select_contracts_falls_back_to_inverse_and_caps_count(self):
        m = self.market
        markets = [m(f'BTC/USD:BTC-{i}', future=True, expiry=i, quote='USD', linear=False) for i in range(10, 0, -1)]
        chosen = derivatives.select_contracts(markets, 'BTC', 'USDT')
        self.assertIsNone(chosen['perpetual'])
        self.assertEqual([c['expiry'] for c in chosen['dated']], [1, 2, 3, 4])

    def test_compute_basis_and_annualization(self):
        now = 0
        quarter = int(90 * 86_400_000)
        data = derivatives.compute_basis(100.0, 102.0, quarter, now)
        self.assertEqual(data['basis'], 2.0)
        self.assertEqual(data['basis_pct'], 2.0)
        self.assertAlmostEqual(data['annualized_pct'], 2.0 * 365 / 90, places=2)
        perp = derivatives.compute_basis(100.0, 99.0, None, now)
        self.assertEqual((perp['basis_pct'], perp['annualized_pct']), (-1.0, None))
        expired = derivatives.compute_basis(100.0, 101.0, -5, now)
        self.assertIsNone(expired['annualized_pct'])

    def test_reference_price_uses_mid_unless_unquoted(self):
        self.assertEqual(derivatives.reference_price({'bid': 99.0, 'ask': 101.0, 'last': 500.0}), (100.0, 'mid', 2.0))
        self.assertEqual(derivatives.reference_price({'bid': None, 'ask': 101.0, 'last': 50.0}), (50.0, 'last', None))
        self.assertEqual(derivatives.reference_price({'bid': 2.0, 'ask': 1.0, 'last': 7.0})[1], 'last')

    def test_fetch_basis_builds_rows_with_stale_last_protection(self):
        expiry = int((datetime.now(timezone.utc) + timedelta(days=60)).timestamp() * 1000)
        markets = [
            self.market('BTC/USDT:USDT', swap=True),
            self.market('BTC/USDT:USDT-X', future=True, expiry=expiry),
        ]
        tickers = {
            'BTC/USDT:USDT': {'bid': 99.9, 'ask': 100.1, 'last': 100.0},
            'BTC/USDT:USDT-X': {'bid': 101.9, 'ask': 102.1, 'last': 150.0},  # stale last
        }
        futures = MagicMock()
        futures.markets = {m['symbol']: m for m in markets}
        with patch('finhub_mcp.derivatives.get_futures_exchange', return_value=futures), \
                patch('finhub_mcp.derivatives.fetch_crypto_ticker', return_value={'last': 100.0}), \
                patch('finhub_mcp.derivatives.fetch_futures_ticker', side_effect=lambda ex, s: tickers[s]):
            data = derivatives.fetch_basis.__wrapped__('binance', 'BTC/USDT')
        rows = {r['contract']: r for r in data['contracts']}
        self.assertEqual(rows['BTC/USDT:USDT']['type'], 'perpetual')
        self.assertEqual(rows['BTC/USDT:USDT-X']['price_kind'], 'mid')
        self.assertAlmostEqual(rows['BTC/USDT:USDT-X']['basis_pct'], 2.0, places=2)
        self.assertIsNotNone(rows['BTC/USDT:USDT-X']['annualized_pct'])
        self.assertEqual(data['spot_price'], 100.0)

    def test_fetch_basis_without_futures_raises(self):
        futures = MagicMock()
        futures.markets = {}
        with patch('finhub_mcp.derivatives.get_futures_exchange', return_value=futures):
            with self.assertRaisesRegex(ValueError, 'lists no BTC futures'):
                derivatives.fetch_basis.__wrapped__('binance', 'BTC/USDT')

    def test_long_short_ratio_rows_and_summary(self):
        exchange = MagicMock()
        exchange.fetch_long_short_ratio_history.return_value = [
            {'timestamp': 1_700_003_600_000, 'longShortRatio': 3.0},
            {'timestamp': 1_700_000_000_000, 'longShortRatio': 1.0},
            {'timestamp': 1_700_007_200_000, 'longShortRatio': None},
        ]
        with patch('finhub_mcp.derivatives.get_futures_exchange', return_value=exchange):
            rows = derivatives.fetch_long_short_ratio.__wrapped__('binance', 'BTC/USDT', '1h', 3)
        exchange.fetch_long_short_ratio_history.assert_called_once_with('BTC/USDT:USDT', '1h', None, 3)
        self.assertEqual([r['ratio'] for r in rows], [1.0, 3.0])  # oldest first, null dropped
        self.assertEqual((rows[1]['long_pct'], rows[1]['short_pct']), (75.0, 25.0))
        summary = derivatives.summarize_long_short(rows)
        self.assertEqual(summary['crowding'], 'long-heavy')
        self.assertEqual(summary['change_in_ratio'], 2.0)
        balanced = derivatives.summarize_long_short([{'ratio': 1.0, 'long_pct': 50, 'short_pct': 50}])
        self.assertEqual(balanced['crowding'], 'balanced')

    def test_okx_liquidations_convert_contracts_and_sides(self):
        payload = {'code': '0', 'data': [{'details': [
            {'posSide': 'long', 'side': 'sell', 'sz': '30', 'bkPx': '100', 'ts': '1700000000000'},
            {'posSide': 'net', 'side': 'buy', 'sz': '10', 'bkPx': '200', 'ts': '1700000100000'},
        ]}]}
        futures = MagicMock()
        futures.market.return_value = {'contractSize': 0.01}
        with patch('finhub_mcp.derivatives.get_json', return_value=payload), \
                patch('finhub_mcp.derivatives.get_futures_exchange', return_value=futures):
            rows = derivatives.fetch_liquidations.__wrapped__('okx', 'BTC/USDT', 10)
        futures.load_markets.assert_called_once()
        self.assertEqual(rows[0]['liquidated_side'], 'short')  # newest first; net + buy closes a short
        self.assertEqual(rows[0]['value_usdt'], 20.0)
        self.assertEqual((rows[1]['liquidated_side'], rows[1]['amount_base'], rows[1]['value_usdt']), ('long', 0.3, 30.0))

    def test_okx_liquidation_api_error_is_raised(self):
        with patch('finhub_mcp.derivatives.get_json', return_value={'code': '50001', 'msg': 'down'}):
            with self.assertRaisesRegex(ValueError, 'OKX error: down'):
                derivatives.fetch_liquidations.__wrapped__('okx', 'BTC/USDT', 5)

    def test_gate_liquidations_side_from_order_size(self):
        exchange = MagicMock()
        exchange.fetch_liquidations.return_value = [
            {'timestamp': 1_700_000_000_000, 'price': 100.0, 'baseValue': 0.5, 'quoteValue': 50.0,
             'info': {'order_size': '-22'}},
            {'timestamp': 1_700_000_100_000, 'price': 101.0, 'baseValue': 1.0, 'quoteValue': 101.0,
             'info': {'order_size': '5'}},
        ]
        with patch('finhub_mcp.derivatives.get_futures_exchange', return_value=exchange):
            rows = derivatives.fetch_liquidations.__wrapped__('gate', 'BTC/USDT', 5)
        self.assertEqual([r['liquidated_side'] for r in rows], ['long', 'short'])
        summary = derivatives.summarize_liquidations(sorted(rows, key=lambda r: r['time'], reverse=True))
        self.assertEqual((summary['long_liquidated_usdt'], summary['short_liquidated_usdt']), (50.0, 101.0))
        self.assertEqual(summary['dominant_side'], 'short')

    def test_unsupported_liquidation_exchange(self):
        with self.assertRaisesRegex(ValueError, 'does not expose'):
            derivatives.fetch_liquidations.__wrapped__('binance', 'BTC/USDT', 5)

    def test_fear_greed_parsing(self):
        payload = {'data': [{'value': '28', 'value_classification': 'Fear', 'timestamp': '1794441600'}]}
        with patch('finhub_mcp.derivatives.get_json', return_value=payload):
            rows = derivatives.fetch_fear_greed.__wrapped__(1)
        self.assertEqual(rows[0]['date'], '2026-11-12')
        self.assertEqual((rows[0]['value'], rows[0]['classification']), (28, 'Fear'))
        with patch('finhub_mcp.derivatives.get_json', return_value={'data': []}):
            with self.assertRaises(ValueError):
                derivatives.fetch_fear_greed.__wrapped__(1)


def raw_board():
    """Raw KBS-style board: HOSE/HNX rows incl. an untraded symbol, a ceiling and a floor hit."""
    return pd.DataFrame([
        # symbol, exchange, ref, ceil, floor, open, high, low, close, avg, vol, value, fbuy, fsell
        ('AAA', 'HOSE', 10000, 10700, 9300, 10000, 10500, 9900, 10500, 10300, 1_000_000, 10_300_000_000, 50_000, 10_000),
        ('BBB', 'HOSE', 20000, 21400, 18600, 20000, 20000, 18600, 18600, 19000, 500_000, 9_500_000_000, 0, 30_000),
        ('CCC', 'HOSE', 30000, 32100, 27900, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        ('DDD', 'HNX', 15000, 16500, 13500, 15000, 16500, 15000, 16500, 16000, 200_000, 3_200_000_000, 0, 0),
        ('EEE', 'HNX', 8000, 8800, 7200, 8000, 8000, 8000, 8000, 8000, 100_000, 800_000_000, 5_000, 5_000),
    ], columns=['symbol', 'exchange', 'reference_price', 'ceiling_price', 'floor_price', 'open_price', 'high_price',
                'low_price', 'close_price', 'average_price', 'volume_accumulated', 'total_value',
                'foreign_buy_volume', 'foreign_sell_volume'])


class VnBoardTests(unittest.TestCase):
    def setUp(self):
        self.board = vnboard.normalize_board(raw_board()).set_index('symbol')

    def test_prices_are_converted_to_thousand_vnd(self):
        self.assertEqual(self.board.loc['AAA', 'price'], 10.5)
        self.assertEqual(self.board.loc['AAA', 'reference'], 10.0)

    def test_change_and_pct(self):
        self.assertAlmostEqual(self.board.loc['AAA', 'change'], 0.5)
        self.assertAlmostEqual(self.board.loc['AAA', 'change_pct'], 5.0)
        self.assertAlmostEqual(self.board.loc['BBB', 'change_pct'], -7.0)

    def test_untraded_symbol_shows_reference_and_zero_change(self):
        row = self.board.loc['CCC']
        self.assertFalse(row['traded'])
        self.assertEqual((row['price'], row['change'], row['change_pct']), (30.0, 0.0, 0.0))

    def test_ceiling_and_floor_status(self):
        self.assertEqual(self.board.loc['DDD', 'status'], 'ceiling')
        self.assertEqual(self.board.loc['BBB', 'status'], 'floor')
        self.assertEqual(self.board.loc['AAA', 'status'], '')

    def test_foreign_net_volume_and_estimated_value(self):
        self.assertEqual(self.board.loc['AAA', 'foreign_net_volume'], 40_000)
        self.assertEqual(self.board.loc['AAA', 'foreign_net_value_vnd'], 40_000 * 10300)
        self.assertEqual(self.board.loc['BBB', 'foreign_net_volume'], -30_000)

    def test_handles_missing_columns_and_string_numbers(self):
        raw = pd.DataFrame({'symbol': ['x'], 'reference_price': ['1000'], 'close_price': [''], 'exchange': ['HOSE']})
        board = vnboard.normalize_board(raw)
        self.assertEqual(board.loc[0, 'symbol'], 'X')
        self.assertEqual(board.loc[0, 'price'], 1.0)
        self.assertFalse(board.loc[0, 'traded'])

    def test_breadth(self):
        data = vnboard.breadth(self.board.reset_index())
        self.assertEqual((data['symbols'], data['traded'], data['not_traded']), (5, 4, 1))
        self.assertEqual((data['advancers'], data['decliners'], data['unchanged']), (2, 1, 1))
        self.assertEqual((data['ceiling'], data['floor']), (1, 1))
        self.assertEqual(data['advance_decline_ratio'], 2.0)
        self.assertEqual(data['value_bn_vnd'], 23.8)
        self.assertEqual(data['foreign']['net_volume'], 10_000)

    def test_breadth_with_no_decliners_has_null_ratio(self):
        board = self.board.reset_index()
        data = vnboard.breadth(board[board['symbol'].isin(['AAA', 'DDD'])])
        self.assertIsNone(data['advance_decline_ratio'])

    def test_screen_does_not_mutate_the_shared_board(self):
        board = self.board.reset_index()
        before = board.copy()
        vnboard.screen(board, min_change_pct=1, foreign_net='buy', status='ceiling', min_value_bn=1)
        pd.testing.assert_frame_equal(board, before)

    def test_screen_filters(self):
        board = self.board.reset_index()
        self.assertEqual(set(vnboard.screen(board)['symbol']), {'AAA', 'BBB', 'DDD', 'EEE'})  # traded only
        self.assertEqual(list(vnboard.screen(board, min_change_pct=1)['symbol']), ['AAA', 'DDD'])
        self.assertEqual(list(vnboard.screen(board, max_change_pct=-1)['symbol']), ['BBB'])
        self.assertEqual(list(vnboard.screen(board, min_price=15)['symbol']), ['BBB', 'DDD'])
        self.assertEqual(set(vnboard.screen(board, max_price=15)['symbol']), {'AAA', 'EEE'})
        self.assertEqual(list(vnboard.screen(board, min_value_bn=5)['symbol']), ['AAA', 'BBB'])
        self.assertEqual(list(vnboard.screen(board, min_volume=600_000)['symbol']), ['AAA'])
        self.assertEqual(list(vnboard.screen(board, foreign_net='buy')['symbol']), ['AAA'])
        self.assertEqual(list(vnboard.screen(board, foreign_net='sell')['symbol']), ['BBB'])
        self.assertEqual(list(vnboard.screen(board, status='ceiling')['symbol']), ['DDD'])
        self.assertEqual(list(vnboard.screen(board, status='floor')['symbol']), ['BBB'])

    def test_board_row_formatting(self):
        row = vnboard.rows_of(self.board.reset_index())[0]
        self.assertEqual(row['symbol'], 'AAA')
        self.assertEqual(row['value_bn_vnd'], 10.3)
        self.assertIsNone(row['status'])
        json.dumps(vnboard.rows_of(self.board.reset_index()))

    def test_no_trades_warning(self):
        idle = self.board.reset_index()
        self.assertIsNone(vnboard.no_trades_warning(idle))
        self.assertIn('No trades yet', vnboard.no_trades_warning(idle[idle['symbol'] == 'CCC']))

    def test_normalize_group(self):
        self.assertEqual(vnboard.normalize_group(' vn30 '), 'VN30')
        with self.assertRaisesRegex(ValueError, 'Invalid group'):
            vnboard.normalize_group('nasdaq')

    def test_chunking_splits_large_symbol_lists(self):
        symbols = tuple(f'S{i:04d}' for i in range(vnboard.BOARD_CHUNK * 2 + 5))
        calls = []

        def price_board(chunk):
            calls.append(len(chunk))
            return raw_board().head(1)

        trading = MagicMock()
        trading.price_board.side_effect = price_board
        with patch('finhub_mcp.vnboard.vn_trading', return_value=trading):
            frame = vnboard._fetch_raw(symbols, 'KBS')
        self.assertEqual(sorted(calls), [5, vnboard.BOARD_CHUNK, vnboard.BOARD_CHUNK])
        self.assertEqual(len(frame), 3)


class ExtensionToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        providers.reset_cooldowns()

    tearDown = setUp

    async def call(self, name, arguments):
        result = await server.mcp.call_tool(name, arguments)
        return json.loads(result[1]['result'])

    # ---- crypto
    async def test_crypto_prices_batch_with_missing_and_fallback(self):
        def fake(exchange, symbols):
            listed = {'binance': {'BTC/USDT', 'ETH/USDT'}, 'okx': {'SOL/USDT'}}.get(exchange, set())
            found = {s: {'last': 1.0, 'percentage': 1.234, 'quoteVolume': 5e6, 'high': 2, 'low': 0.5}
                     for s in symbols if s in listed}
            if not found:
                raise ValueError('none listed')
            return found

        with patch('finhub_mcp.tools.crypto_extra.fetch_crypto_tickers_for', side_effect=fake):
            payload = await self.call('get_crypto_prices',
                                      {'symbols': ['btc', 'ETH', 'sol', 'nope'], 'exchange': 'binance,okx'})
        self.assertTrue(payload['ok'], payload)
        self.assertEqual([r['symbol'] for r in payload['data']], ['BTC/USDT', 'ETH/USDT', 'SOL/USDT'])
        self.assertEqual({r['exchange'] for r in payload['data']}, {'binance', 'okx'})
        self.assertEqual(payload['meta']['missing'], ['NOPE/USDT'])
        self.assertTrue(payload['meta']['fallback_used'])
        self.assertEqual(payload['data'][0]['change_pct_24h'], 1.23)

    async def test_crypto_prices_accepts_string_and_stops_when_all_found(self):
        calls = []

        def fake(exchange, symbols):
            calls.append(exchange)
            return {s: {'last': 1.0} for s in symbols}

        with patch('finhub_mcp.tools.crypto_extra.fetch_crypto_tickers_for', side_effect=fake):
            payload = await self.call('get_crypto_prices', {'symbols': 'btc, eth'})
        self.assertEqual(len(payload['data']), 2)
        self.assertEqual(calls, ['binance'])
        self.assertNotIn('missing', payload.get('meta', {}))

    async def test_crypto_prices_failure_and_limits(self):
        with patch('finhub_mcp.tools.crypto_extra.fetch_crypto_tickers_for', side_effect=RuntimeError('down')):
            payload = await self.call('get_crypto_prices', {'symbols': 'btc', 'exchange': 'okx'})
        self.assertEqual(payload['meta']['error_type'], 'provider_failure')
        too_many = await self.call('get_crypto_prices', {'symbols': [f'C{i}' for i in range(51)]})
        self.assertFalse(too_many['ok'])
        self.assertIn('Too many', too_many['error'])
        empty = await self.call('get_crypto_prices', {'symbols': []})
        self.assertFalse(empty['ok'])

    async def test_long_short_tool(self):
        rows = [{'time': 'a', 'ratio': 1.0, 'long_pct': 50.0, 'short_pct': 50.0},
                {'time': 'b', 'ratio': 2.0, 'long_pct': 66.67, 'short_pct': 33.33}]
        with patch('finhub_mcp.tools.crypto_extra.fetch_long_short_ratio', return_value=rows) as fetch:
            payload = await self.call('get_crypto_long_short_ratio', {'symbol': 'btc', 'limit': 2})
        fetch.assert_called_once_with('binance', 'BTC/USDT', '1h', 2)
        self.assertEqual(payload['meta']['summary']['latest_ratio'], 2.0)
        bad = await self.call('get_crypto_long_short_ratio', {'symbol': 'btc', 'exchange': 'kucoin'})
        self.assertFalse(bad['ok'])
        self.assertIn('Unsupported long/short exchange', bad['error'])
        bad_tf = await self.call('get_crypto_long_short_ratio', {'symbol': 'btc', 'timeframe': 'x'})
        self.assertEqual(bad_tf['meta']['error_type'], 'validation')

    async def test_liquidations_tool_falls_back_between_exchanges(self):
        rows = [{'time': '2026-01-02 00:00:00', 'liquidated_side': 'long', 'price': 1.0, 'amount_base': 1.0,
                 'value_usdt': 10.0}]

        def fake(exchange, symbol, limit):
            if exchange == 'okx':
                raise ConnectionError('reset')
            return rows

        with patch('finhub_mcp.tools.crypto_extra.fetch_liquidations', side_effect=fake):
            payload = await self.call('get_crypto_liquidations', {'symbol': 'btc'})
        self.assertEqual(payload['meta']['exchange'], 'gate')
        self.assertTrue(payload['meta']['fallback_used'])
        self.assertEqual(payload['meta']['summary']['dominant_side'], 'long')

    async def test_fear_greed_and_basis_tools(self):
        rows = [{'date': '2026-11-12', 'value': 28, 'classification': 'Fear'}]
        with patch('finhub_mcp.tools.crypto_extra.fetch_fear_greed', return_value=rows) as fetch:
            payload = await self.call('get_crypto_fear_greed', {'days': 9999})
        fetch.assert_called_once_with(90)
        self.assertEqual(payload['data'], rows)
        basis = {'symbol': 'BTC/USDT', 'exchange': 'okx', 'spot_price': 1.0, 'contracts': [], 'warnings': {'x': 'y'}}
        with patch('finhub_mcp.tools.crypto_extra.fetch_basis', return_value=basis):
            payload = await self.call('get_crypto_basis', {'symbol': 'btc', 'exchange': 'okx'})
        self.assertNotIn('warnings', payload['data'])
        self.assertEqual(payload['meta']['warnings'], {'x': 'y'})

    # ---- vn market
    def patch_board(self):
        board = vnboard.normalize_board(raw_board())
        return patch('finhub_mcp.tools.vn_market.load_board', return_value=('KBS', board)), board

    async def test_top_movers_gainers_losers_and_liquidity_filter(self):
        patcher, _ = self.patch_board()
        with patcher:
            payload = await self.call('get_vn_top_movers', {'group': 'all', 'limit': 5, 'min_value_bn': 5})
            wide = await self.call('get_vn_top_movers', {'group': 'HOSE', 'limit': 5, 'min_value_bn': 0})
        self.assertEqual([r['symbol'] for r in payload['data']['gainers']], ['AAA'])
        self.assertEqual([r['symbol'] for r in payload['data']['losers']], ['BBB'])
        self.assertEqual([r['symbol'] for r in wide['data']['gainers']], ['DDD', 'AAA'])
        self.assertEqual(payload['meta']['price_unit'], 'thousand VND')

    async def test_top_movers_by_value_and_validation(self):
        patcher, _ = self.patch_board()
        with patcher:
            payload = await self.call('get_vn_top_movers', {'by': 'value', 'limit': 2})
            bad = await self.call('get_vn_top_movers', {'by': 'magic'})
            bad_group = await self.call('get_vn_top_movers', {'group': 'nasdaq'})
        self.assertEqual([r['symbol'] for r in payload['data']['top']], ['AAA', 'BBB'])
        self.assertFalse(bad['ok'])
        self.assertEqual(bad_group['meta']['error_type'], 'validation')

    async def test_breadth_tool_has_exchange_breakdown_for_all(self):
        patcher, _ = self.patch_board()
        with patcher:
            payload = await self.call('get_vn_market_breadth', {'group': 'ALL'})
            single = await self.call('get_vn_market_breadth', {'group': 'HNX'})
        self.assertEqual(payload['data']['advancers'], 2)
        self.assertEqual(set(payload['data']['by_exchange']), {'HOSE', 'HNX'})
        self.assertNotIn('by_exchange', single['data'])

    async def test_foreign_trading_tool(self):
        patcher, _ = self.patch_board()
        with patcher:
            payload = await self.call('get_vn_foreign_trading', {'limit': 5})
        data = payload['data']
        self.assertEqual([r['symbol'] for r in data['top_net_buy']], ['AAA'])
        self.assertEqual([r['symbol'] for r in data['top_net_sell']], ['BBB'])
        self.assertEqual(data['top_net_buy'][0]['foreign_net_value_bn_vnd_est'], 0.412)
        self.assertEqual(data['totals']['net_volume'], 10_000)

    async def test_screen_tool(self):
        patcher, _ = self.patch_board()
        with patcher:
            payload = await self.call('screen_vn_stocks', {'min_change_pct': 1, 'sort_by': 'price'})
            asc = await self.call('screen_vn_stocks', {'sort_by': 'price', 'descending': False, 'limit': 2})
            limited = await self.call('screen_vn_stocks', {'foreign_net': 'buy'})
            bad = await self.call('screen_vn_stocks', {'sort_by': 'karma'})
            bad_status = await self.call('screen_vn_stocks', {'status': 'moon'})
            bad_foreign = await self.call('screen_vn_stocks', {'foreign_net': 'maybe'})
        self.assertEqual([r['symbol'] for r in payload['data']], ['DDD', 'AAA'])
        self.assertEqual(payload['meta']['matched'], 2)
        self.assertEqual([r['symbol'] for r in asc['data']], ['EEE', 'AAA'])
        self.assertEqual([r['symbol'] for r in limited['data']], ['AAA'])
        for result in (bad, bad_status, bad_foreign):
            self.assertFalse(result['ok'])

    async def test_vn_stock_prices_filters_unknown_symbols(self):
        board = vnboard.normalize_board(raw_board())
        seen = {}

        def fake_board(symbols, source):
            seen['symbols'] = symbols
            return board[board['symbol'].isin(symbols)]

        with patch('finhub_mcp.tools.vn_market.fetch_group_symbols', return_value=('AAA', 'BBB', 'CCC')), \
                patch('finhub_mcp.tools.vn_market.fetch_board_for', side_effect=fake_board):
            payload = await self.call('get_vn_stock_prices', {'symbols': 'aaa, zzz, bbb'})
            none = await self.call('get_vn_stock_prices', {'symbols': ['zzz']})
            bad = await self.call('get_vn_stock_prices', {'symbols': ['A B']})
        self.assertEqual(seen['symbols'], ('AAA', 'BBB'))
        self.assertEqual([r['symbol'] for r in payload['data']], ['AAA', 'BBB'])
        self.assertEqual(payload['meta']['missing'], ['ZZZ'])
        self.assertFalse(none['ok'])
        self.assertEqual(bad['meta']['error_type'], 'validation')

    # ---- world
    async def test_world_forex_partial_failure_and_all_failed(self):
        def fake(pair):
            if pair == 'USDJPY':
                raise ValueError('nope')
            return {'pair': pair, 'rate': 1.0}

        with patch('finhub_mcp.tools.world.forex_rate', side_effect=fake):
            payload = await self.call('get_world_forex', {'pairs': 'EURUSD,usd/jpy'})
            failing = await self.call('get_world_forex', {'pairs': ['USDJPY']})
            invalid = await self.call('get_world_forex', {'pairs': ['EURUSD', 'bad']})
        self.assertEqual([r['pair'] for r in payload['data']], ['EURUSD'])
        self.assertIn('USD/JPY', payload['meta']['warnings'][0])
        self.assertFalse(failing['ok'])
        self.assertEqual(invalid['meta']['error_type'], 'validation')

    async def test_world_forex_history_validates_interval_and_clamps(self):
        rows = [{'time': '2026-11-12', 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.5, 'volume': 0}]
        with patch('finhub_mcp.tools.world.yahoo_history', return_value=rows) as history:
            payload = await self.call('get_world_forex_history', {'pair': 'eur/usd', 'days': 90, 'interval': '1m'})
            bad = await self.call('get_world_forex_history', {'pair': 'EURUSD', 'interval': '3h'})
        history.assert_called_once_with('EURUSD=X', 7, '1m')
        self.assertEqual(payload['meta']['days'], 7)
        self.assertFalse(bad['ok'])
        with patch('finhub_mcp.tools.world.yahoo_history', return_value=[]):
            empty = await self.call('get_world_forex_history', {'pair': 'EURUSD'})
        self.assertFalse(empty['ok'])

    async def test_gold_price_adds_vnd_estimate_and_survives_forex_failure(self):
        gold = {'metal': 'XAU', 'price': 3000.0}
        with patch('finhub_mcp.tools.world.metal_price', return_value=dict(gold)), \
                patch('finhub_mcp.tools.world.forex_rate', return_value={'rate': 25000.0}):
            payload = await self.call('get_world_metal_price', {'metal': 'gold'})
        expected = round(3000.0 * 25000.0 * 37.5 / 31.1034768)
        self.assertEqual(payload['data']['vnd_per_luong_estimate'], expected)
        with patch('finhub_mcp.tools.world.metal_price', return_value=dict(gold)), \
                patch('finhub_mcp.tools.world.forex_rate', side_effect=RuntimeError('fx down')):
            degraded = await self.call('get_world_metal_price', {})
        self.assertTrue(degraded['ok'])
        self.assertNotIn('vnd_per_luong_estimate', degraded['data'])
        self.assertIn('fx down', degraded['meta']['warnings']['vnd_per_luong_estimate'])
        with patch('finhub_mcp.tools.world.metal_price', return_value={'metal': 'XAG', 'price': 50.0}):
            silver = await self.call('get_world_metal_price', {'metal': 'silver'})
        self.assertNotIn('vnd_per_luong_estimate', silver['data'])
        bad = await self.call('get_world_metal_price', {'metal': 'copper'})
        self.assertEqual(bad['meta']['error_type'], 'validation')

    async def test_metal_history_uses_futures_symbol(self):
        rows = [{'time': 't', 'open': 1, 'high': 1, 'low': 1, 'close': 1, 'volume': 0}]
        with patch('finhub_mcp.tools.world.yahoo_history', return_value=rows) as history:
            payload = await self.call('get_world_metal_history', {'metal': 'silver', 'days': 10})
        history.assert_called_once_with('SI=F', 10, '1d')
        self.assertEqual(payload['meta']['yahoo_symbol'], 'SI=F')

    # ---- news
    def sample_articles(self):
        recent = NOW - timedelta(hours=1)
        return [
            {'title': 'Cổ phiếu tăng trần, thanh khoản kỷ lục', 'link': 'l1', 'published': recent,
             'summary': '', 'source': 'CafeF', 'feed': 'cafef_stock', 'lang': 'vi'},
            {'title': 'Thị trường lao dốc, bán tháo diện rộng', 'link': 'l2', 'published': recent,
             'summary': '', 'source': 'Vietstock', 'feed': 'vietstock_stock', 'lang': 'vi'},
            {'title': 'Họp cổ đông thường niên', 'link': 'l3', 'published': None,
             'summary': '', 'source': 'CafeF', 'feed': 'cafef_stock', 'lang': 'vi'},
        ]

    async def test_market_news_tool(self):
        with patch('finhub_mcp.tools.news.collect_news', return_value=(self.sample_articles(), {'x': 'down'})):
            payload = await self.call('get_market_news', {'limit': 2})
            plain = await self.call('get_market_news', {'include_sentiment': False})
        self.assertEqual(len(payload['data']), 2)
        self.assertEqual(payload['data'][0]['sentiment']['label'], 'positive')
        self.assertEqual(payload['meta']['matched'], 3)
        self.assertEqual(payload['meta']['sentiment']['articles'], 3)
        self.assertEqual(payload['meta']['warnings'], {'x': 'down'})
        self.assertNotIn('sentiment', plain['data'][0])
        self.assertNotIn('sentiment', plain['meta'])
        self.assertIsNone(plain['data'][2]['published'])

    async def test_market_news_empty_and_failures(self):
        with patch('finhub_mcp.tools.news.collect_news', return_value=([], {})):
            payload = await self.call('get_market_news', {'query': 'zzz'})
        self.assertEqual(payload['data'], [])
        self.assertNotIn('sentiment', payload['meta'])
        failure = ProviderFallbackError('All news feeds failed (a: x)', {'a': 'x'})
        with patch('finhub_mcp.tools.news.collect_news', side_effect=failure):
            failed = await self.call('get_market_news', {})
        self.assertEqual(failed['meta']['error_type'], 'provider_failure')
        bad_source = await self.call('get_market_news', {'source': 'bogus'})
        self.assertEqual(bad_source['meta']['error_type'], 'validation')

    async def test_news_sentiment_tool(self):
        with patch('finhub_mcp.tools.news.collect_news', return_value=(self.sample_articles(), {})):
            payload = await self.call('get_news_sentiment', {'query': 'x'})
        data = payload['data']
        self.assertEqual(data['overall']['articles'], 3)
        self.assertEqual(set(data['by_source']), {'CafeF', 'Vietstock'})
        self.assertEqual(data['most_positive'][0]['title'], 'Cổ phiếu tăng trần, thanh khoản kỷ lục')
        self.assertEqual(data['most_negative'][0]['source'], 'Vietstock')
        self.assertEqual(payload['meta']['query'], 'x')
        with patch('finhub_mcp.tools.news.collect_news', return_value=([], {})):
            nothing = await self.call('get_news_sentiment', {'query': 'zzz'})
        self.assertFalse(nothing['ok'])
        self.assertEqual(nothing['meta']['error_type'], 'not_found')

    # ---- indicators through the tools
    async def test_crypto_indicators_tool_supports_new_indicators(self):
        candles = [[1_700_000_000_000 + i * 86_400_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000 + i]
                   for i in range(120)]
        candles[5][2] = None  # malformed candle is dropped, not fatal
        with patch('finhub_mcp.tools.crypto.fetch_crypto_ohlcv', return_value=candles):
            payload = await self.call('get_crypto_indicators', {'symbol': 'btc', 'indicators': 'atr,vwap,signals'})
            everything = await self.call('get_crypto_indicators', {'symbol': 'btc', 'indicators': 'all'})
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(payload['data']['candles'], 119)
        self.assertEqual(set(payload['data']['indicators']), {'atr', 'vwap', 'signals'})
        self.assertEqual(set(everything['data']['indicators']), set(ind.SUPPORTED_INDICATORS))
        bad = await self.call('get_crypto_indicators', {'symbol': 'btc', 'indicators': 'nope'})
        self.assertEqual(bad['meta']['error_type'], 'validation')

    async def test_vn_indicators_tool_supports_new_indicators(self):
        days = pd.date_range('2025-01-01', periods=120, freq='D')
        df = pd.DataFrame({'time': days, 'open': 10.0, 'high': [11 + i * 0.1 for i in range(120)],
                           'low': 9.0, 'close': [10 + i * 0.1 for i in range(120)],
                           'volume': [1000 + i for i in range(120)]})
        with patch('finhub_mcp.tools.vn_stock.fetch_vn_history', return_value=df):
            payload = await self.call('get_vn_stock_indicators',
                                      {'symbol': 'fpt', 'indicators': 'stochastic,support_resistance,volume_ma'})
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(set(payload['data']['indicators']), {'stochastic', 'support_resistance', 'volume_ma'})
        self.assertEqual(payload['data']['candles'], 120)


class HttpClientTests(unittest.TestCase):
    def test_size_cap_and_json(self):
        from finhub_mcp import http

        class FakeResponse:
            def __init__(self, chunks):
                self.chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                return iter(self.chunks)

        with patch.object(http._session, 'get', return_value=FakeResponse([b'{"a":', b'1}'])):
            self.assertEqual(http.get_json('https://x'), {'a': 1})
        with patch.object(http, 'MAX_BYTES', 10), \
                patch.object(http._session, 'get', return_value=FakeResponse([b'x' * 6, b'x' * 6])):
            with self.assertRaisesRegex(ValueError, 'larger than'):
                http.get_bytes('https://x')


class NumpyJsonSafetyTests(unittest.TestCase):
    def test_board_rows_serialize_numpy_types(self):
        board = vnboard.normalize_board(raw_board())
        self.assertTrue(isinstance(board['volume'].iloc[0], (np.floating, float)))
        json.dumps(vnboard.rows_of(board))


if __name__ == '__main__':
    unittest.main()
