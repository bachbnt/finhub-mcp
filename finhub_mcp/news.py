# Copyright (c) 2026 bachbnt. All rights reserved.
"""RSS news aggregation with a simple keyword-based sentiment score (Vietnamese and English)."""

import html
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from defusedxml import ElementTree

from finhub_mcp.cache import ttl_cache
from finhub_mcp.config import ConfigError, get_dict
from finhub_mcp.exceptions import ProviderFallbackError
from finhub_mcp.http import get_bytes
from finhub_mcp.providers import first_success, is_cooling_down

SUMMARY_CHARS = 280

# ------------------------------------------------------------------ feeds


def get_feeds() -> dict[str, dict[str, str]]:
    """Enabled feeds from config as {key: {name, url, group, lang}}."""
    feeds = {}
    for key, entry in get_dict('news_feeds').items():
        if not isinstance(entry, dict) or entry.get('enabled') is False:
            continue
        url = entry.get('url')
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            raise ConfigError(f"Invalid config value for news_feeds.{key}.url: expected an http(s) URL")
        feeds[key] = {
            'name': str(entry.get('name') or key),
            'url': url,
            'group': str(entry.get('group') or 'other').lower(),
            'lang': str(entry.get('lang') or 'en').lower(),
        }
    if not feeds:
        raise ConfigError("No news feeds are enabled in news_feeds")
    return feeds


def select_feeds(source: str) -> dict[str, dict[str, str]]:
    """Resolve 'auto'/'all', a group name (vn, crypto, ...), or comma-separated feed keys."""
    feeds = get_feeds()
    wanted = [part.strip().lower() for part in (source or 'auto').split(',') if part.strip()]
    if not wanted or wanted[0] in {'auto', 'all'}:
        return feeds
    groups = {feed['group'] for feed in feeds.values()}
    selected: dict[str, dict[str, str]] = {}
    for name in wanted:
        if name in feeds:
            selected[name] = feeds[name]
        elif name in groups:
            selected.update({k: f for k, f in feeds.items() if f['group'] == name})
        else:
            choices = ', '.join(['auto', *sorted(groups), *feeds])
            raise ValueError(f"Unknown news source: {name}. Choose from: {choices}")
    return selected


# ---------------------------------------------------------------- parsing

_TAG_RE = re.compile(r'<[^>]+>')
_SPACE_RE = re.compile(r'\s+')


def clean_text(value: str | None) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    if not value:
        return ''
    return _SPACE_RE.sub(' ', html.unescape(_TAG_RE.sub(' ', value))).strip()


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _child_text(element, *names: str) -> str | None:
    for child in element:
        if _local(child.tag) in names and (child.text or '').strip():
            return child.text.strip()
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_feed(xml_bytes: bytes) -> list[dict[str, Any]]:
    """Parse RSS 2.0 (<item>) or Atom (<entry>) into articles with UTC datetimes."""
    root = ElementTree.fromstring(xml_bytes)
    articles = []
    for element in root.iter():
        if _local(element.tag) not in {'item', 'entry'}:
            continue
        title = clean_text(_child_text(element, 'title'))
        if not title:
            continue
        link = _child_text(element, 'link')
        if link is None:  # Atom: <link href="..."/>
            for child in element:
                if _local(child.tag) == 'link' and child.get('href'):
                    link = child.get('href')
                    break
        published = _parse_time(_child_text(element, 'pubDate', 'published', 'updated', 'date'))
        summary = clean_text(_child_text(element, 'description', 'summary', 'encoded', 'content'))
        articles.append({
            'title': title,
            'link': link,
            'published': published,
            'summary': summary[:SUMMARY_CHARS],
        })
    return articles


@ttl_cache('news')
def fetch_feed(url: str) -> list[dict[str, Any]]:
    return parse_feed(get_bytes(url))


# ------------------------------------------------------------ text search


def fold(text: str) -> str:
    """Lowercase and strip diacritics (Vietnamese đ included) so 'cổ phiếu' matches 'co phieu'."""
    decomposed = unicodedata.normalize('NFD', text.lower().replace('đ', 'd'))
    return ''.join(ch for ch in decomposed if unicodedata.category(ch) != 'Mn')


def matches_query(article: dict[str, Any], query: str) -> bool:
    """True when every word of the query appears (whole-word, accent-insensitive) in title or summary."""
    words = fold(query).split()
    if not words:
        return True
    haystack = fold(f"{article['title']} {article['summary']}")
    return all(re.search(rf'(?<!\w){re.escape(word)}(?!\w)', haystack) for word in words)


# -------------------------------------------------------------- sentiment

# Keyword lexicons matched against lower-cased text *with* diacritics, so "giảm" (fall) is not
# confused with "giám" (as in "giám đốc", director). Longer phrases win over their parts:
# "giảm lỗ" (losses narrowing) is positive even though "giảm" and "lỗ" are negative, and
# "lãi suất" (interest rate) is neutral even though "lãi" (profit) is positive.
# A trailing '*' matches any word ending (liquidat* -> liquidation, liquidated).
_LEXICON: dict[str, int] = {
    # Vietnamese, positive
    'tăng mạnh': 2, 'tăng trần': 2, 'tăng điểm': 1, 'tăng vọt': 2, 'bứt phá': 2, 'khởi sắc': 2,
    'hồi phục': 2, 'phục hồi': 2, 'kỷ lục': 2, 'vượt đỉnh': 2, 'mua ròng': 2, 'tăng trưởng': 1,
    'tích cực': 1, 'lạc quan': 1, 'lợi nhuận tăng': 2, 'lãi lớn': 2, 'giảm lỗ': 2, 'đột biến': 1,
    'tăng giá': 1, 'hút dòng tiền': 2, 'giảm lãi suất': 1, 'tăng': 1, 'lãi': 1,
    # Vietnamese, negative
    'giảm mạnh': -2, 'giảm sâu': -2, 'giảm điểm': -1, 'giảm sàn': -2, 'lao dốc': -2, 'tụt dốc': -2,
    'bán tháo': -2, 'bán ròng': -2, 'thua lỗ': -2, 'lỗ nặng': -2, 'sụt giảm': -2, 'đỏ lửa': -2,
    'rủi ro': -1, 'vi phạm': -2, 'khởi tố': -2, 'bị phạt': -1, 'phá sản': -2, 'nợ xấu': -2,
    'gián đoạn': -1, 'tiêu cực': -1, 'bi quan': -1, 'hoảng loạn': -2, 'bị bắt': -2,
    'tăng lãi suất': -1, 'giảm giá': -1, 'giảm': -1, 'lỗ': -1,
    'lãi suất': 0,
    # English, positive
    'surge*': 2, 'soar*': 2, 'rall*': 2, 'bullish': 2, 'record high': 2, 'all-time high': 2,
    'breakout': 2, 'jump*': 1, 'gain*': 1, 'rebound*': 2, 'recover*': 1, 'approv*': 1, 'adopt*': 1,
    'inflow*': 1, 'upgrade*': 1, 'growth': 1, 'profit*': 1, 'boost*': 1,
    # English, negative
    'plunge*': -2, 'crash*': -2, 'bearish': -2, 'slump*': -2, 'sell-off': -2, 'selloff': -2,
    'hack*': -2, 'exploit*': -2, 'lawsuit*': -1, 'banned': -1, 'fraud*': -2, 'liquidat*': -1,
    'outflow*': -1, 'drop*': -1, 'declin*': -1, 'loss*': -1, 'fear*': -1, 'warning*': -1,
    'bankrupt*': -2, 'investigat*': -1, 'delist*': -2, 'scam*': -2, 'dump*': -1, 'fall*': -1,
    'fell': -1, 'tumbl*': -2,
}


def _build_pattern(lexicon: dict[str, int]) -> re.Pattern:
    parts = []
    for entry in sorted(lexicon, key=len, reverse=True):  # longest first so phrases beat their parts
        parts.append(re.escape(entry.rstrip('*')) + (r'\w*' if entry.endswith('*') else ''))
    return re.compile(r'(?<!\w)(?:' + '|'.join(parts) + r')(?!\w)')


_PATTERN = _build_pattern(_LEXICON)
_EXACT = {entry: weight for entry, weight in _LEXICON.items() if not entry.endswith('*')}
_WILDCARDS = [(entry[:-1], weight) for entry, weight in _LEXICON.items() if entry.endswith('*')]


def _weight(token: str) -> int:
    if token in _EXACT:
        return _EXACT[token]
    for prefix, weight in _WILDCARDS:
        if token.startswith(prefix):
            return weight
    return 0


def score_text(text: str) -> tuple[int, int]:
    """(net weight, total weight) of lexicon hits in the text."""
    net = total = 0
    for match in _PATTERN.finditer(unicodedata.normalize('NFC', text).lower()):
        weight = _weight(match.group(0))
        net += weight
        total += abs(weight)
    return net, total


def label_for(score: float) -> str:
    return 'positive' if score >= 0.2 else 'negative' if score <= -0.2 else 'neutral'


def score_article(article: dict[str, Any]) -> dict[str, Any]:
    """Sentiment in [-1, 1]; the headline counts double the summary."""
    t_net, t_total = score_text(article['title'])
    s_net, s_total = score_text(article['summary'])
    net, total = 2 * t_net + s_net, 2 * t_total + s_total
    score = net / total if total else 0.0
    return {'score': round(score, 2), 'label': label_for(score)}


def summarize_sentiment(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate score, label and counts over articles that already carry a 'sentiment' field."""
    scores = [a['sentiment']['score'] for a in articles]
    counts = {'positive': 0, 'neutral': 0, 'negative': 0}
    for article in articles:
        counts[article['sentiment']['label']] += 1
    average = sum(scores) / len(scores) if scores else 0.0
    return {
        'score': round(average, 2),
        'label': label_for(average) if scores else None,
        'articles': len(scores),
        **counts,
    }


# ------------------------------------------------------------ aggregation


def collect_news(source: str, query: str = '', hours: int = 48) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fetch the selected feeds in parallel and return (articles newest first, {feed: error})."""
    feeds = select_feeds(source)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    def load(key: str):
        feed = feeds[key]
        if is_cooling_down(f'news:{key}'):
            return key, None, 'skipped: cooling down after a recent network error'
        try:
            _, articles = first_success([f'news:{key}'], lambda _p: fetch_feed(feed['url']), 'feed is empty')
            return key, articles, None
        except Exception as e:
            return key, None, str(getattr(e, 'provider_errors', {}).get(f'news:{key}') or e)

    with ThreadPoolExecutor(max_workers=min(len(feeds), 8)) as pool:
        outcomes = list(pool.map(load, feeds))

    warnings = {key: err for key, articles, err in outcomes if err}
    if len(warnings) == len(feeds):
        details = '; '.join(f'{key}: {err}' for key, err in warnings.items())
        raise ProviderFallbackError(f"All news feeds failed ({details})", warnings)
    seen: set[str] = set()
    result = []
    for key, articles, _ in outcomes:
        for article in articles or []:
            ident = article['link'] or article['title']
            published = article['published']
            if ident in seen or (published and published < cutoff) or not matches_query(article, query):
                continue
            seen.add(ident)
            result.append({**article, 'source': feeds[key]['name'], 'feed': key, 'lang': feeds[key]['lang']})
    result.sort(key=lambda a: a['published'] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return result, warnings


def public_article(article: dict[str, Any], sentiment: bool) -> dict[str, Any]:
    """JSON-friendly article (ISO time), optionally with sentiment attached."""
    published = article['published']
    row = {
        'title': article['title'],
        'link': article['link'],
        'published': published.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if published else None,
        'source': article['source'],
        'summary': article['summary'],
    }
    if sentiment:
        row['sentiment'] = article['sentiment']
    return row
