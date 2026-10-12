"""A weak search must never erase what an earlier search verified; re-scoring costs nothing."""
from dataclasses import replace

import pytest
from conftest import TODAY, make_page, make_signal, make_vc

from pipeline import store
from pipeline.run import process_vc, rescore_vc


def saved(days_ago=3, url="https://techcrunch.com/old", relevance="logistics", summary="Led a $5M seed round in a freight startup."):
    return replace(make_signal(days_ago=days_ago, url=url, relevance=relevance), summary=summary, source_tier="news", source_domain="techcrunch.com")


class EmptySearch:
    """A weak provider: finds nothing at all."""
    def search(self, *a, **k):
        return []

    def search_people(self, *a, **k):
        return []


class NeverCalled:
    def search(self, *a, **k):
        raise AssertionError("rescoring must not search")

    search_people = extract = extract_people = search


def test_a_search_that_finds_nothing_keeps_the_signals_already_on_file(rf):
    r = process_vc(make_vc(), EmptySearch(), type("X", (), {"extract": staticmethod(lambda *a, **k: [])})(), rf, TODAY,
                   check_people=False, existing=[saved()])
    assert [s.url for s in r.signals] == ["https://techcrunch.com/old"]
    assert r.decision.action == "reach_out" and r.detail["signals_new"] == 0 and r.detail["signals_on_file"] == 1


def test_signals_older_than_the_window_age_out(rf):
    r = process_vc(make_vc(), EmptySearch(), type("X", (), {"extract": staticmethod(lambda *a, **k: [])})(), rf, TODAY,
                   check_people=False, existing=[saved(days_ago=31, url="https://techcrunch.com/stale")])
    assert r.signals == [] and r.decision.action == "quiet_list"


def test_a_signal_found_again_is_not_counted_twice(rf):
    class Finds:
        def search(self, *a, **k):
            return [make_page("Acme Ventures led a $5M seed round in a freight startup today.", days_ago=3, url="https://techcrunch.com/old")]

        def extract(self, ctx, vc, pages):
            return [{"type": "portfolio_investment", "summary": "Led a $5M seed round in a freight startup.",
                     "quote": "Acme Ventures led a $5M seed round in a freight startup", "url": "https://techcrunch.com/old"}]
    f = Finds()
    r = process_vc(make_vc(), f, f, rf, TODAY, check_people=False, existing=[saved()])
    assert len(r.signals) == 1


def test_new_and_saved_signals_combine(rf):
    class Finds:
        def search(self, *a, **k):
            return [make_page("Acme Ventures published its thesis on warehouse automation for freight operators.", days_ago=1, url="https://acmeventures.com/post")]

        def extract(self, ctx, vc, pages):
            return [{"type": "blog_post", "summary": "Published a thesis on warehouse automation.", "relevance": "logistics",
                     "quote": "Acme Ventures published its thesis on warehouse automation", "url": "https://acmeventures.com/post"}]
    f = Finds()
    r = process_vc(make_vc(), f, f, rf, TODAY, check_people=False, existing=[saved()])
    assert {s.url for s in r.signals} == {"https://techcrunch.com/old", "https://acmeventures.com/post"}
    assert r.detail["signals_new"] == 1 and r.detail["signals_on_file"] == 1


def test_rescore_rebuilds_the_decision_from_saved_signals_without_searching(rf):
    r = rescore_vc(make_vc(), [saved()], rf, TODAY)
    assert r.ok and r.outcome == "rescored" and r.decision.action == "reach_out" and r.decision.score >= 35
    assert r.detail["rescored"] is True and len(r.signals) == 1


def test_rescore_drops_duplicates_and_aged_signals(rf):
    same_a = saved(url="https://a.com/1", summary="Led a $5M seed round for FreightCo, a freight visibility startup.")
    same_b = saved(url="https://b.com/2", summary="Participated in FreightCo's $5M seed round for freight visibility.")
    old = saved(days_ago=40, url="https://c.com/3")
    r = rescore_vc(make_vc(), [same_a, same_b, old], rf, TODAY)
    assert len(r.signals) == 1


def test_rescore_still_skips_declined_and_not_a_fit(rf):
    assert rescore_vc(make_vc(status="declined"), [saved()], rf, TODAY).decision.action == "skip"
    assert rescore_vc(make_vc(is_investing=False), [saved()], rf, TODAY).outcome == "skipped_fit"


def test_a_rescore_never_counts_as_a_fresh_check_in_the_schedule():
    # last_checked_at is built from these outcomes only; 'rescored' must not be among them
    assert "'rescored'" not in store.LOAD_SQL.split("last_checked_at")[0].split("outcome IN")[-1]
    assert "outcome IN ('signal', 'no_signal', 'skipped_fit')" in store.LOAD_SQL


def test_loading_saved_evidence_is_windowed_and_skips_unverified():
    assert "published_date >= %s" in store.EVIDENCE_LOAD_SQL and "legacy_unverified" in store.EVIDENCE_LOAD_SQL
