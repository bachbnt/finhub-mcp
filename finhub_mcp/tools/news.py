# Copyright (c) 2026 bachbnt. All rights reserved.

from finhub_mcp.news import collect_news, public_article, score_article, summarize_sentiment
from finhub_mcp.providers import clamp
from finhub_mcp.responses import error, success
from finhub_mcp.tooling import register_tool


def _scored(source: str, query: str, hours: int):
    articles, warnings = collect_news(source, query, hours)
    for article in articles:
        article['sentiment'] = score_article(article)
    return articles, warnings


def register(mcp) -> None:
    tool = register_tool(mcp)

    @tool
    def get_market_news(
        source: str = 'auto',
        query: str = '',
        limit: int = 15,
        hours: int = 48,
        include_sentiment: bool = True,
    ) -> str:
        """Get recent market news from RSS feeds (CafeF, Vietstock, VnExpress, Cointelegraph, Decrypt), newest first.

        source: "auto" (all feeds), a group ("vn", "crypto"), or comma-separated feed keys from news_feeds in
        config.json. query: optional words that must all appear in the title or summary (accent-insensitive,
        e.g. "FPT" or "co phieu ngan hang"). Each article gets a keyword-based sentiment score in [-1, 1]
        (a rough heuristic, not a model).
        """
        limit = clamp(limit, 1, 50)
        hours = clamp(hours, 1, 24 * 14)
        articles, warnings = _scored(source, query, hours)
        shown = articles[:limit]
        return success(
            [public_article(a, include_sentiment) for a in shown],
            matched=len(articles),
            sentiment=summarize_sentiment(articles) if include_sentiment and articles else None,
            warnings=warnings or None,
        )

    @tool
    def get_news_sentiment(query: str = '', source: str = 'auto', hours: int = 48) -> str:
        """Summarize news sentiment for a topic, ticker or the whole market.

        Aggregates the keyword-based sentiment of recent RSS articles (optionally filtered by query such as
        "FPT" or "bitcoin") overall and per source, with the most positive and most negative headlines.
        This is a rough heuristic, so treat it as a signal of tone, not a forecast.
        """
        hours = clamp(hours, 1, 24 * 14)
        articles, warnings = _scored(source, query, hours)
        if not articles:
            return error(f"no news matched query={query!r} in the last {hours}h", error_type='not_found')
        by_source = {}
        for name in dict.fromkeys(a['source'] for a in articles):
            by_source[name] = summarize_sentiment([a for a in articles if a['source'] == name])
        ranked = sorted(articles, key=lambda a: a['sentiment']['score'])
        most_negative = [a for a in ranked[:3] if a['sentiment']['score'] < 0]
        most_positive = [a for a in reversed(ranked[-3:]) if a['sentiment']['score'] > 0]
        return success(
            {
                'overall': summarize_sentiment(articles),
                'by_source': by_source,
                'most_positive': [public_article(a, True) for a in most_positive],
                'most_negative': [public_article(a, True) for a in most_negative],
            },
            query=query or None,
            hours=hours,
            method='keyword lexicon (Vietnamese + English)',
            warnings=warnings or None,
        )
