"""Tavily provider (mocked HTTP), the provider chain, and the check schedule."""
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from conftest import TODAY, make_vc

from pipeline.config import ConfigError, load_run_files
from pipeline.run import is_due, process_vc


# ---------------------------------------------------------------- Tavily provider (mocked HTTP)

def _tavily(monkeypatch, response=None, raises=None):
    from pipeline import search as s
    calls = []

    def fake(url, headers, body, **k):
        calls.append((url, headers, body))
        if raises:
            raise s.net.HttpError(raises)
        return response or {"results": []}
    monkeypatch.setattr(s.net, "post_json", fake)
    return s, calls


def test_tavily_sends_one_basic_news_search_with_a_short_quoted_query(monkeypatch):
    s, calls = _tavily(monkeypatch, {"results": []})
    s.TavilySearch(api_key="tvly-x").search(make_vc(firm="Acme Ventures"), ["logistics", "freight", "warehouse"], TODAY, 30,
                                            exclude_domains=["papermark.com"], extra_terms=("Priya Rao", "Sam Lee"))
    assert len(calls) == 1                                                  # ONE call per investor
    url, headers, body = calls[0]
    assert url == "https://api.tavily.com/search" and headers["Authorization"] == "Bearer tvly-x"
    assert body["search_depth"] == "basic" and body["topic"] == "news" and body["time_range"] == "month"
    assert body["include_raw_content"] is True and body["exclude_domains"] == ["papermark.com"]
    assert body["query"] == '"Acme Ventures" logistics freight investment'    # short + quoted; partner names are NOT added


def test_tavily_pages_carry_the_apis_date_and_full_text(monkeypatch):
    s, _ = _tavily(monkeypatch, {"results": [
        {"url": "https://techcrunch.com/a", "title": "A", "content": "snippet", "raw_content": "FULL TEXT", "published_date": "Mon, 12 Oct 2026 10:00:00 GMT"},
        {"url": "https://reuters.com/b", "title": "B", "content": "only a snippet", "published_date": "2026-10-09T08:00:00Z"},
        {"url": "https://x.com/c", "title": "C", "content": "undated"},
        {"url": "https://techcrunch.com/a", "title": "dup", "content": "dup"}]})
    pages = s.TavilySearch(api_key="k").search(make_vc(), ["logistics"], TODAY, 30)
    assert [(p.url, str(p.published), p.text) for p in pages] == [
        ("https://techcrunch.com/a", "2026-10-12", "FULL TEXT"),
        ("https://reuters.com/b", "2026-10-09", "only a snippet"),
        ("https://x.com/c", "None", "undated")]


def test_tavily_people_search_uses_general_topic(monkeypatch):
    s, calls = _tavily(monkeypatch, {"results": []})
    s.TavilySearch(api_key="k").search_people(make_vc(firm="Acme Ventures"))
    assert calls[0][2]["topic"] == "general" and "time_range" not in calls[0][2]


@pytest.mark.parametrize("err", ["HTTP 402: x", "HTTP 429: slow down", "HTTP 432: plan limit", "HTTP 433: paygo limit",
                                 "gave up after 4 attempts: HTTP 429", "HTTP 401: invalid key"])
def test_tavily_out_of_credits_or_bad_key_is_quota_exhausted(monkeypatch, err):
    s, _ = _tavily(monkeypatch, raises=err)
    with pytest.raises(s.QuotaExhausted):
        s.TavilySearch(api_key="k").search(make_vc(), ["logistics"], TODAY, 30)


def test_tavily_server_error_stays_a_per_investor_failure(monkeypatch):
    s, _ = _tavily(monkeypatch, raises="HTTP 503: down")
    with pytest.raises(s.SearchError):
        s.TavilySearch(api_key="k").search(make_vc(), ["logistics"], TODAY, 30)


def test_parse_date_handles_both_formats_and_junk():
    from pipeline.search import _parse_date
    assert str(_parse_date("2026-10-12T00:00:00.000Z")) == "2026-10-12"
    assert str(_parse_date("Tue, 13 Oct 2026 01:02:03 GMT")) == "2026-10-13"
    assert _parse_date("yesterday") is None and _parse_date(None) is None


# ---------------------------------------------------------------- provider chain

class _P:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, 0

    @property
    def call_count(self):
        return self.calls

    def search(self, *a, **k):
        self.calls += 1
        from pipeline.search import QuotaExhausted
        if self.fail:
            raise QuotaExhausted(f"{self.name} spent")
        return [f"pages from {self.name}"]

    search_people = search


def test_chain_uses_the_first_provider_and_falls_back_when_it_is_spent():
    from pipeline.search import ChainSearch
    t, e = _P("tavily", fail=True), _P("exa")
    chain = ChainSearch([t, e])
    assert chain.search() == ["pages from exa"] and chain.last_provider == "exa"
    chain.search()
    assert t.calls == 1                       # a spent provider is not asked again this run
    assert chain.exhausted == {"tavily": "tavily spent"}


def test_chain_raises_only_when_every_provider_is_spent():
    from pipeline.search import ChainSearch, QuotaExhausted
    with pytest.raises(QuotaExhausted, match="All search providers"):
        ChainSearch([_P("tavily", True), _P("exa", True)]).search()


def test_build_search_orders_providers_and_skips_missing_keys():
    from pipeline.search import SearchError, build_search
    assert build_search({"TAVILY_API_KEY": "t", "EXA_API_KEY": "e"}).names == ["tavily", "exa"]
    assert build_search({"EXA_API_KEY": "e"}).names == ["exa"]
    assert build_search({"TAVILY_API_KEY": "t", "EXA_API_KEY": "e", "SEARCH_PROVIDERS": "exa,tavily"}).names == ["exa", "tavily"]
    with pytest.raises(SearchError, match="No search provider"):
        build_search({})
    with pytest.raises(SearchError, match="Unknown"):
        build_search({"TAVILY_API_KEY": "t", "SEARCH_PROVIDERS": "bing"})


def test_exa_code_is_still_there_and_still_works(monkeypatch):
    from pipeline import search as s
    calls = []
    monkeypatch.setattr(s.net, "post_json", lambda url, h, body, **k: calls.append((url, h, body)) or {"results": [
        {"url": "https://techcrunch.com/a", "publishedDate": "2026-10-10T00:00:00.000Z", "text": "t"}]})
    pages = s.ExaSearch(api_key="e").search(make_vc(), ["logistics"], TODAY, 30, [], ["techcrunch.com"])
    assert len(calls) == 4 and calls[0][0] == s.EXA_URL and str(pages[0].published) == "2026-10-10"   # 3 angles + trusted query


# ---------------------------------------------------------------- the check schedule

NOW = datetime(2026, 10, 12, 1, 30, tzinfo=timezone.utc)


def _due(rf, hours_ago, **vckw):
    return is_due(make_vc(last_checked_at=NOW - timedelta(hours=hours_ago), **vckw), NOW, rf.weights, TODAY)


def test_pipeline_investors_are_checked_every_two_days(rf):
    assert _due(rf, 72, status="waiting") and _due(rf, 72, status="in_conversation")
    assert not _due(rf, 20, status="waiting")


def test_reach_out_candidates_every_two_days_dormant_weekly(rf):
    assert _due(rf, 72, action="reach_out") and not _due(rf, 20, action="reach_out")
    assert not _due(rf, 72, action="quiet_list")            # Monday -> Thursday: dormant waits
    assert not _due(rf, 5 * 24, action="quiet_list")
    assert _due(rf, 7 * 24, action="quiet_list")            # a week later: due
    assert _due(rf, 6.6 * 24, action="quiet_list")          # 12h slack so a Monday/Thursday cadence lands weekly


def test_never_checked_is_always_due_and_declined_or_not_a_fit_cost_nothing(rf):
    assert is_due(make_vc(), NOW, rf.weights, TODAY)
    assert _due(rf, 1, status="declined") and _due(rf, 1, action="skip")


def test_force_checks_everyone(rf):
    assert is_due(make_vc(last_checked_at=NOW, action="quiet_list"), NOW, rf.weights, TODAY, force=True)


def test_schedule_settings_are_validated(tmp_path):
    d = tmp_path / "rf"
    shutil.copytree(Path(__file__).resolve().parent.parent / "run_files", d)
    w = yaml.safe_load((d / "weights.yaml").read_text())
    w["schedule"]["dormant_days"] = 0
    (d / "weights.yaml").write_text(yaml.dump(w))
    with pytest.raises(ConfigError, match="dormant_days"):
        load_run_files(d)


def test_detail_records_the_provider_and_how_many_searches_it_cost(rf):
    from pipeline.search import ChainSearch

    class Counting(_P):
        def search(self, *a, **k):
            self.calls += 1
            return []

    class Extractor:
        @staticmethod
        def extract(*a, **k):
            return []
    r = process_vc(make_vc(), ChainSearch([Counting("tavily")]), Extractor(), rf, TODAY, check_people=False)
    assert r.detail["searches"] == 1 and r.detail["provider"] == "tavily"
