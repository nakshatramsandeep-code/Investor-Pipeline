"""Search API (Exa). Returns pages with the API's own publish dates. A failed search
raises SearchError - it must never be read as 'no signal'."""
from __future__ import annotations

import os
from datetime import date, timedelta

from . import net
from .models import VC, Page
from .names import firm_aliases

EXA_URL = "https://api.exa.ai/search"


class SearchError(RuntimeError):
    pass


def build_query(vc: VC, keywords: list[str]) -> str:
    firm = firm_aliases(vc.firm)[0]
    parts = [f'"{firm}"']
    if vc.name:
        parts.append(f'"{vc.name}"')
    parts.append("venture investor")
    parts.append(" OR ".join(keywords))
    return " ".join(parts)


def build_queries(vc: VC, keywords: list[str]) -> list[str]:
    """Three angles on what a fund has done lately: deals, writing, and events."""
    firm = firm_aliases(vc.firm)[0]
    topic = " OR ".join(keywords)
    return [
        f'"{firm}" invests OR led OR raised OR portfolio OR "new fund" {topic}',
        f'"{firm}" blog OR newsletter OR essay OR thesis OR "market map" {topic}',
        f'"{firm}" speaker OR panel OR keynote OR conference OR podcast OR interview {topic}',
    ]


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


class ExaSearch:
    def __init__(self, api_key: str | None = None, num_results: int = 10, max_chars: int = 8000):
        self.api_key = api_key or os.environ.get("EXA_API_KEY", "")
        if not self.api_key:
            raise SearchError("EXA_API_KEY is not set")
        self.num_results = num_results
        self.max_chars = max_chars

    def search(self, vc: VC, keywords: list[str], today: date, window_days: int,
               exclude_domains: list[str] | None = None, include_domains: list[str] | None = None) -> list[Page]:
        """A firm is searched from three angles (deals, writing, events) so one noisy angle can't
        hide the others; a single person gets one combined query. Results are merged by URL."""
        start = today - timedelta(days=window_days)
        queries = [(build_query(vc, keywords), None)] if vc.name else [(q, None) for q in build_queries(vc, keywords)]
        if include_domains:
            # One extra query limited to trusted outlets: every result is usable.
            queries.append((build_query(vc, keywords), include_domains))
        pages, seen = [], set()
        for query, only in queries:
            body = {
                "query": query,
                "numResults": self.num_results,
                "startPublishedDate": f"{start.isoformat()}T00:00:00.000Z",
                "contents": {"text": {"maxCharacters": self.max_chars}},
            }
            if only:
                body["includeDomains"] = only
            elif exclude_domains:
                body["excludeDomains"] = exclude_domains
            try:
                data = net.post_json(EXA_URL, {"x-api-key": self.api_key}, body)
            except net.HttpError as e:
                raise SearchError(str(e)) from e
            for r in data.get("results", []):
                url = r.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                pages.append(Page(url=url, title=r.get("title") or "",
                                  published=_parse_date(r.get("publishedDate")),
                                  text=r.get("text") or ""))
        return pages

    def search_people(self, vc: VC, exclude_domains: list[str] | None = None) -> list[Page]:
        """Pages naming the firm's founders and partners. No date window: team pages are evergreen."""
        firm = firm_aliases(vc.firm)[0]
        body = {
            "query": f'"{firm}" founder OR "managing partner" OR "general partner" OR partners team',
            "numResults": 8,
            "contents": {"text": {"maxCharacters": self.max_chars}},
        }
        if exclude_domains:
            body["excludeDomains"] = exclude_domains
        try:
            data = net.post_json(EXA_URL, {"x-api-key": self.api_key}, body)
        except net.HttpError as e:
            raise SearchError(str(e)) from e
        return [Page(url=r["url"], title=r.get("title") or "", published=_parse_date(r.get("publishedDate")),
                     text=r.get("text") or "") for r in data.get("results", []) if r.get("url")]
