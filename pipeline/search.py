"""Search providers. Every provider returns Pages carrying the API's own publish date (never a date the model
claims). A failed search raises SearchError - it must never be read as 'no signal'. Running out of credits raises
QuotaExhausted, which ChainSearch answers by moving to the next provider.

Providers: Tavily (default, 1 credit per basic search) and Exa (kept as the fallback). Order and choice come
from SEARCH_PROVIDERS (default "tavily,exa"); a provider without its API key is skipped.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date, timedelta
from email.utils import parsedate_to_datetime

from . import net
from .models import VC, Page
from .names import firm_aliases

EXA_URL = "https://api.exa.ai/search"
TAVILY_URL = "https://api.tavily.com/search"


class SearchError(RuntimeError):
    pass


class QuotaExhausted(Exception):
    """A provider has no credits left (or rejected the key). Deliberately NOT a SearchError: retrying other
    investors on the same provider would fail the same way. ChainSearch moves to the next provider; when all
    are exhausted the run stops with one clear message."""


# ---------------------------------------------------------------- errors

_HTTP = re.compile(r"HTTP (\d{3})")


def _quota_message(provider: str, text: str) -> str | None:
    """A message if this HTTP failure means 'out of credits / rate limited / key rejected', else None."""
    m = _HTTP.search(text)
    code = int(m.group(1)) if m else 0
    low = text.lower()
    if provider == "exa":
        if code == 402 or "no_more_credits" in low or "credits limit" in low:
            return "Exa credits are used up (top up at dashboard.exa.ai)"
    elif provider == "tavily":
        if code in (402, 429, 432, 433) or "usage limit" in low or "plan's limit" in low or "exceeds your plan" in low:
            return "Tavily credits are used up or rate-limited (check app.tavily.com)"
    if code in (401, 403):
        return f"{provider.capitalize()} rejected the API key"
    return None


def _translate(provider: str, e: "net.HttpError") -> Exception:
    text = str(e)
    msg = _quota_message(provider, text)
    return QuotaExhausted(msg) if msg else SearchError(text)


# ---------------------------------------------------------------- queries

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


def build_tavily_query(vc: VC, keywords: list[str], extra_terms: tuple[str, ...] = ()) -> str:
    """ONE short query per investor. Tested live: a short quoted name plus two topic words finds far more
    pages that actually name the firm than a long descriptive query, and adding partner names made it worse
    (0 of 10 results named the firm). extra_terms is accepted but deliberately not used. Partners are
    attributed afterwards, from the pages themselves."""
    firm = firm_aliases(vc.firm)[0]
    return f'"{firm}" {" ".join(keywords[:2])} investment'[:390]


def _parse_date(s: str | None) -> date | None:
    """ISO ('2026-10-12T...') or RFC 2822 ('Mon, 12 Oct 2026 10:00:00 GMT'); anything else is 'no date'."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(str(s)).date()
    except (TypeError, ValueError):
        return None


class _Counted:
    """Per-thread count of provider calls, so a run can report how many searches each investor cost."""

    def __init__(self):
        self._tl = threading.local()

    @property
    def call_count(self) -> int:
        return getattr(self._tl, "n", 0)

    def _tick(self) -> None:
        self._tl.n = self.call_count + 1


# ---------------------------------------------------------------- Exa (kept as the fallback provider)

class ExaSearch(_Counted):
    name = "exa"

    def __init__(self, api_key: str | None = None, num_results: int = 10, max_chars: int = 8000):
        super().__init__()
        self.api_key = api_key or os.environ.get("EXA_API_KEY", "")
        if not self.api_key:
            raise SearchError("EXA_API_KEY is not set")
        self.num_results = num_results
        self.max_chars = max_chars

    def search(self, vc: VC, keywords: list[str], today: date, window_days: int,
               exclude_domains: list[str] | None = None, include_domains: list[str] | None = None,
               extra_terms: tuple[str, ...] = ()) -> list[Page]:
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
            self._tick()
            try:
                data = net.post_json(EXA_URL, {"x-api-key": self.api_key}, body)
            except net.HttpError as e:
                raise _translate("exa", e) from e
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
        self._tick()
        try:
            data = net.post_json(EXA_URL, {"x-api-key": self.api_key}, body)
        except net.HttpError as e:
            raise _translate("exa", e) from e
        return [Page(url=r["url"], title=r.get("title") or "", published=_parse_date(r.get("publishedDate")),
                     text=r.get("text") or "") for r in data.get("results", []) if r.get("url")]


# ---------------------------------------------------------------- Tavily (default provider)

class TavilySearch(_Counted):
    """One basic search (1 credit) per investor. The news topic is used for activity because it is the mode that
    returns a publish date, and dates must come from the API. The people search uses the general topic: team
    pages need no date."""
    name = "tavily"

    def __init__(self, api_key: str | None = None, max_results: int = 10, max_chars: int = 8000):
        super().__init__()
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        if not self.api_key:
            raise SearchError("TAVILY_API_KEY is not set")
        self.max_results = max_results
        self.max_chars = max_chars

    def _post(self, body: dict) -> list[dict]:
        self._tick()
        try:
            data = net.post_json(TAVILY_URL, {"Authorization": f"Bearer {self.api_key}"}, body)
        except net.HttpError as e:
            raise _translate("tavily", e) from e
        return data.get("results", []) or []

    def _pages(self, results: list[dict]) -> list[Page]:
        pages, seen = [], set()
        for r in results:
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            text = (r.get("raw_content") or r.get("content") or "")[: self.max_chars]
            pages.append(Page(url=url, title=r.get("title") or "", published=_parse_date(r.get("published_date")), text=text))
        return pages

    def search(self, vc: VC, keywords: list[str], today: date, window_days: int,
               exclude_domains: list[str] | None = None, include_domains: list[str] | None = None,
               extra_terms: tuple[str, ...] = ()) -> list[Page]:
        body = {
            "query": build_tavily_query(vc, keywords, extra_terms),
            "topic": "news",
            "search_depth": "basic",                    # 1 credit; 'advanced' costs 2
            "time_range": "month" if window_days >= 28 else "week",
            "max_results": self.max_results,
            "include_raw_content": True,                # the full page text, so quotes can be verified
            "include_answer": False,
        }
        if exclude_domains:
            body["exclude_domains"] = exclude_domains
        return self._pages(self._post(body))

    def search_people(self, vc: VC, exclude_domains: list[str] | None = None) -> list[Page]:
        firm = firm_aliases(vc.firm)[0]
        body = {
            "query": f'"{firm}" founder OR "managing partner" OR "general partner" OR partners team',
            "topic": "general", "search_depth": "basic", "max_results": 8,
            "include_raw_content": True, "include_answer": False,
        }
        if exclude_domains:
            body["exclude_domains"] = exclude_domains
        return self._pages(self._post(body))


# ---------------------------------------------------------------- the chain

class ChainSearch:
    """Try providers in order. A provider that reports 'out of credits' (or a rejected key) is retired for the rest
    of the run and the next one takes over. Only when every provider is spent does QuotaExhausted escape."""

    def __init__(self, providers: list):
        if not providers:
            raise SearchError("No search provider configured. Set TAVILY_API_KEY and/or EXA_API_KEY.")
        self.providers = providers
        self.exhausted: dict[str, str] = {}
        self._tl = threading.local()

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    @property
    def last_provider(self) -> str | None:
        return getattr(self._tl, "provider", None)

    @property
    def call_count(self) -> int:
        return sum(p.call_count for p in self.providers)

    def _call(self, method: str, *args, **kwargs):
        for p in self.providers:
            if p.name in self.exhausted:
                continue
            try:
                out = getattr(p, method)(*args, **kwargs)
            except QuotaExhausted as e:
                self.exhausted[p.name] = str(e)
                continue
            self._tl.provider = p.name
            return out
        raise QuotaExhausted("All search providers are out of credits: " + "; ".join(self.exhausted.values()) + ". Top up, then re-run.")

    def search(self, *args, **kwargs):
        return self._call("search", *args, **kwargs)

    def search_people(self, *args, **kwargs):
        return self._call("search_people", *args, **kwargs)


def build_search(env=None) -> ChainSearch:
    """Providers named in SEARCH_PROVIDERS (default 'tavily,exa'), in that order, skipping any without a key."""
    env = os.environ if env is None else env
    makers = {"tavily": ("TAVILY_API_KEY", TavilySearch), "exa": ("EXA_API_KEY", ExaSearch)}
    providers = []
    for name in [n.strip().lower() for n in env.get("SEARCH_PROVIDERS", "tavily,exa").split(",") if n.strip()]:
        if name not in makers:
            raise SearchError(f"Unknown search provider '{name}' in SEARCH_PROVIDERS")
        key_var, cls = makers[name]
        if env.get(key_var):
            providers.append(cls(api_key=env[key_var]))
    return ChainSearch(providers)
