import re
import shutil
from pathlib import Path

import pytest
import yaml

from pipeline import store
from pipeline.config import ConfigError, load_run_files
from pipeline.run import process_vc
from pipeline.search import SearchError
from conftest import TODAY, make_vc


def copy_run_files(tmp_path) -> Path:
    d = tmp_path / "rf"
    shutil.copytree(Path(__file__).resolve().parent.parent / "run_files", d)
    return d


def test_shipped_run_files_load(rf):
    assert rf.weights.window_days == 30 and rf.fit.stage_must_match_any


def test_missing_signal_type_weight_fails_loudly(tmp_path):
    d = copy_run_files(tmp_path)
    w = yaml.safe_load((d / "weights.yaml").read_text())
    del w["signal_types"]["blog_post"]
    (d / "weights.yaml").write_text(yaml.dump(w))
    with pytest.raises(ConfigError, match="blog_post"):
        load_run_files(d)


def test_out_of_range_weight_fails(tmp_path):
    d = copy_run_files(tmp_path)
    w = yaml.safe_load((d / "weights.yaml").read_text())
    w["signal_types"]["blog_post"] = 7
    (d / "weights.yaml").write_text(yaml.dump(w))
    with pytest.raises(ConfigError):
        load_run_files(d)


def test_wrong_version_fails(tmp_path):
    d = copy_run_files(tmp_path)
    (d / "fit_rules.yaml").write_text("version: 2\n")
    with pytest.raises(ConfigError, match="version"):
        load_run_files(d)


# ---------------------------------------------------------------- write boundary

def test_job_sql_never_references_vikrams_columns():
    sql = " ".join([store.LOAD_SQL, store.UPDATE_SQL, store.EVIDENCE_SQL, store.CHECK_SQL]).lower()
    assert "notes" not in sql and "contact_history" not in sql
    assert "select *" not in sql


def test_update_touches_exactly_the_four_signal_columns():
    assigned = re.search(r"set (.*?) where", store.UPDATE_SQL.lower().replace("\n", " ")).group(1)
    cols = set(re.findall(r"(\w+)\s*=\s*", assigned))
    assert cols == {"last_signal_date", "signal_summary", "follow_up_score", "recommended_action"}


def test_schema_grants_match_the_four_columns():
    schema = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text()
    m = re.search(r"GRANT UPDATE \(([^)]*)\) ON vcs TO signal_job", schema)
    assert {c.strip() for c in m.group(1).split(",")} == {
        "last_signal_date", "signal_summary", "follow_up_score", "recommended_action"}
    assert "notes" not in re.search(r"GRANT SELECT \(([^)]*)\) ON vcs TO signal_job", schema).group(1)


# ---------------------------------------------------------------- failure handling

class BoomSearch:
    def search(self, *a, **k):
        raise SearchError("HTTP 503")


class NeverCalled:
    def search(self, *a, **k):
        raise AssertionError("search must not run for skipped VCs")

    def extract(self, *a, **k):
        raise AssertionError("extract must not run for skipped VCs")


def test_failed_search_is_a_failure_not_no_signal(rf):
    r = process_vc(make_vc(), BoomSearch(), None, rf, TODAY)
    assert not r.ok and r.outcome == "failed" and r.decision is None


def test_skipped_vc_costs_no_api_calls(rf):
    r = process_vc(make_vc(is_investing=False), NeverCalled(), NeverCalled(), rf, TODAY)
    assert r.ok and r.outcome == "skipped_fit" and r.decision.action == "skip"


# ---------------------------------------------------------------- person-level checks

class PeopleClients:
    """Firm search finds nothing; the partner's own search finds a conference talk."""

    def __init__(self):
        self.person_searches, self.discoveries = [], 0
        from conftest import make_page
        self.page = make_page("At the summit, Jane Partner of Acme Ventures said freight visibility is underfunded in software.",
                              url="https://techcrunch.com/summit")

    def search(self, vc, kw, today, window, exclude=None, include=None):
        if vc.name:
            self.person_searches.append(vc.name)
            return [self.page]
        return []

    def extract(self, ctx, vc, pages):
        if not vc.name or not pages:
            return []
        return [{"type": "event_appearance", "summary": "Spoke at a summit.",
                 "quote": "Jane Partner of Acme Ventures said freight visibility is underfunded", "url": pages[0].url}]

    def search_people(self, vc, exclude=None):
        self.discoveries += 1
        from conftest import make_page
        return [make_page("Jane Partner is a General Partner at Acme Ventures.", url="https://acmeventures.com/team")]

    def extract_people(self, vc, pages):
        return [{"name": "Jane Partner", "role": "General Partner", "url": pages[0].url,
                 "quote": "Jane Partner is a General Partner at Acme Ventures"}]


def test_partner_activity_becomes_a_signal_tagged_with_the_person(rf):
    c = PeopleClients()
    r = process_vc(make_vc(), c, c, rf, TODAY)
    assert r.outcome == "signal" and r.signals[0].person_name == "Jane Partner"
    assert [p.name for p in r.people_found] == ["Jane Partner"] and c.person_searches == ["Jane Partner"]


def test_known_recent_people_are_not_rediscovered(rf):
    from datetime import datetime
    from pipeline.models import Person
    c = PeopleClients()
    known = [Person("Jane Partner", "General Partner", discovered_at=datetime(2026, 10, 1))]
    r = process_vc(make_vc(), c, c, rf, TODAY, known)
    assert c.discoveries == 0 and r.people_found == [] and r.signals


def test_manual_contacts_are_always_checked_first(rf):
    from pipeline.models import Person
    c = PeopleClients()
    process_vc(make_vc(), c, c, rf, TODAY, [Person("Zed Contact", "Partner", source_tier="manual", manual=True)])
    assert c.person_searches[0] == "Zed Contact"


def test_people_checks_can_be_switched_off(rf):
    c = PeopleClients()
    r = process_vc(make_vc(), c, c, rf, TODAY, check_people=False)
    assert c.discoveries == 0 and c.person_searches == [] and r.outcome == "no_signal"


def test_person_search_failure_does_not_fail_the_vc(rf):
    class Boom(PeopleClients):
        def search(self, vc, *a, **k):
            if vc.name:
                raise SearchError("503")
            return []
    c = Boom()
    r = process_vc(make_vc(), c, c, rf, TODAY)
    assert r.ok and any(d["reason"] == "person_check_failed" for d in r.dropped)


# ---------------------------------------------------------------- more signals + check detail

class ManyPages:
    """Search returns trusted, untrusted and undated pages; the model proposes a signal per good page."""

    def __init__(self, good=8):
        from conftest import make_page
        self.pages = [make_page(f"Acme Ventures backed startup number {i} in freight visibility today.",
                                days_ago=i + 1, url=f"https://techcrunch.com/p{i}") for i in range(good)]
        self.pages += [make_page("Acme Ventures post", url="https://www.linkedin.com/posts/a"),
                       make_page("Acme Ventures list", url="https://fundraiseinsider.com/x"),
                       make_page("Acme Ventures undated", days_ago=None, url="https://reuters.com/undated")]
        self.sent = []

    def search(self, vc, kw, today, window, exclude=None, include=None):
        return self.pages

    def extract(self, ctx, vc, pages):
        self.sent = [p.url for p in pages]
        return [{"type": "portfolio_investment", "summary": f"Backed startup {i}.",
                 "quote": f"Acme Ventures backed startup number {i} in freight visibility", "url": p.url}
                for i, p in enumerate(pages)]


def test_only_usable_pages_reach_the_model_and_skips_are_counted(rf):
    c = ManyPages(good=2)
    r = process_vc(make_vc(), c, c, rf, TODAY, check_people=False)
    assert all("linkedin" not in u and "fundraiseinsider" not in u and "undated" not in u for u in c.sent)
    assert r.detail["pages_found"] == 5 and r.detail["pages_usable"] == 2
    assert r.detail["skipped"] == {"linkedin.com": 1, "fundraiseinsider.com": 1}   # untrusted
    assert r.detail["undated"] == 1                                                  # trusted source, no API date


def test_signals_capped_and_newest_first(rf):
    c = ManyPages(good=8)
    r = process_vc(make_vc(), c, c, rf, TODAY, check_people=False)
    assert len(r.signals) == 6
    assert [s.published for s in r.signals] == sorted((s.published for s in r.signals), reverse=True)


def test_detail_records_people_checked(rf):
    c = PeopleClients()
    r = process_vc(make_vc(), c, c, rf, TODAY)
    assert r.detail["people_checked"] == ["Jane Partner"] and r.detail["signals_people"] == 1


def test_firm_is_searched_from_three_angles():
    from pipeline.search import build_queries
    qs = build_queries(make_vc(firm="Acme Ventures"), ["logistics"])
    assert len(qs) == 3 and all('"Acme Ventures"' in q for q in qs)
    assert any("conference" in q for q in qs) and any("blog" in q for q in qs) and any("invests" in q for q in qs)


def test_empty_gemini_candidate_is_retried_once(monkeypatch):
    from pipeline import extract
    calls = []

    def fake_post(url, headers, body, **k):
        calls.append(1)
        return {"candidates": [{"content": {}}]} if len(calls) == 1 else {"candidates": [{"content": {"parts": [{"text": "{\"signals\": []}"}]}}]}
    monkeypatch.setattr(extract.net, "post_json", fake_post)
    g = extract.GeminiExtract(api_key="k")
    from conftest import make_page
    assert g.extract("ctx", make_vc(), [make_page("Acme Ventures text")]) == [] and len(calls) == 2


def test_gemini_empty_twice_is_a_failure(monkeypatch):
    import pytest
    from pipeline import extract
    monkeypatch.setattr(extract.net, "post_json", lambda *a, **k: {"candidates": [{"content": {}}]})
    from conftest import make_page
    with pytest.raises(extract.ExtractError):
        extract.GeminiExtract(api_key="k").extract("ctx", make_vc(), [make_page("Acme Ventures text")])


# ---------------------------------------------------------------- website learning

def test_registrable_domain():
    from pipeline.sources import registrable
    assert registrable("newsletter.equal.vc") == "equal.vc"
    assert registrable("admin.fundalogical.com") == "fundalogical.com"
    assert registrable("dynamo.vc") == "dynamo.vc"
    assert registrable("www.example.co.in") == "example.co.in"


def test_guess_website_prefers_the_common_real_domain_and_skips_platforms():
    from pipeline.sources import guess_website
    items = [("dynamo.substack.com", "own_site"), ("dynamo.substack.com", "own_site"), ("blog.dynamo.vc", "own_site"),
             ("dynamo.vc", "own_site"), ("techcrunch.com", "news")]
    assert guess_website(items) == "https://dynamo.vc"
    assert guess_website([("fieldventures.substack.com", "own_site"), ("techcrunch.com", "news")]) is None


def test_website_is_learned_only_when_blank(rf):
    from dataclasses import replace
    from conftest import make_page
    c = ManyPages(good=2)
    c.pages = [replace(p, url=p.url.replace("techcrunch.com", "acmeventures.com")) for p in c.pages[:2]]
    r = process_vc(make_vc(), c, c, rf, TODAY, check_people=False)
    assert r.website == "https://acmeventures.com"
    assert process_vc(make_vc(website="https://acme.vc"), c, c, rf, TODAY, check_people=False).website is None


# ---------------------------------------------------------------- one signal per event

def _sig(summary, days_ago=3, tier="news", url=None):
    from dataclasses import replace
    from conftest import make_signal
    return replace(make_signal(days_ago=days_ago, url=url or f"https://t.com/{abs(hash(summary)) % 10**6}"), summary=summary, source_tier=tier)


def test_same_event_from_two_articles_is_one_signal():
    from pipeline.run import dedupe_events
    a = _sig("Endiya Partners led a $2.5M seed round for VerifAIX, an AI verification startup.", 25)
    b = _sig("Endiya Partners participated in VerifAIX's seed round to build AI verification.", 23)
    assert len(dedupe_events([a, b], "Endiya Partners")) == 1


def test_different_events_stay_separate():
    from pipeline.run import dedupe_events
    a = _sig("Endiya Partners led a $2.5M seed round for VerifAIX.", 25)
    b = _sig("Endiya Partners backed DeepVerse, a deep tech platform, in a pre-seed round.", 5)
    c = _sig("Endiya Partners published a blog post about the Indian deep tech policy forum.", 3)
    assert len(dedupe_events([a, b, c], "Endiya Partners")) == 3


def test_dedupe_prefers_the_firms_own_site_then_the_newest():
    from pipeline.run import dedupe_events
    press = _sig("Gallatin AI raised $50M in a Series A led by 8VC for military logistics.", 4, "news")
    own = _sig("8VC participated in Gallatin AI's $50M Series A for supply chain software.", 5, "own_site")
    out = dedupe_events([press, own], "8VC")
    assert len(out) == 1 and out[0].source_tier == "own_site"


def test_generic_summaries_about_different_things_are_not_merged():
    from pipeline.run import dedupe_events
    a = _sig("Dynamo Ventures published a blog post on warehouse robotics.", 3)
    b = _sig("Dynamo Ventures published a blog post on freight forwarding software.", 4)
    assert len(dedupe_events([a, b], "Dynamo Ventures")) == 2


def test_store_marks_only_the_latest_runs_evidence_active():
    from pipeline import store
    assert "ANY" in store.ACTIVE_SQL and "legacy_unverified" in store.ACTIVE_SQL


# ---------------------------------------------------------------- out-of-credits handling

def test_exa_402_becomes_quota_exhausted_not_a_per_investor_failure(monkeypatch):
    from pipeline import search as s
    def boom(*a, **k):
        raise s.net.HttpError('HTTP 402: {"error":"You have exceeded your credits limit","tag":"NO_MORE_CREDITS"}')
    monkeypatch.setattr(s.net, "post_json", boom)
    with pytest.raises(s.QuotaExhausted, match="dashboard.exa.ai"):
        s.ExaSearch(api_key="k").search(make_vc(), ["logistics"], TODAY, 30)
    assert not issubclass(s.QuotaExhausted, s.SearchError)      # so process_vc does not swallow it


def test_other_search_errors_stay_per_investor(monkeypatch):
    from pipeline import search as s
    monkeypatch.setattr(s.net, "post_json", lambda *a, **k: (_ for _ in ()).throw(s.net.HttpError("HTTP 503: down")))
    with pytest.raises(s.SearchError):
        s.ExaSearch(api_key="k").search(make_vc(), ["logistics"], TODAY, 30)


def test_quota_exhausted_propagates_out_of_process_vc(rf):
    from pipeline.search import QuotaExhausted

    class NoCredits:
        def search(self, *a, **k):
            raise QuotaExhausted("out")
    with pytest.raises(QuotaExhausted):
        process_vc(make_vc(), NoCredits(), None, rf, TODAY)


def test_a_run_stops_with_exit_code_2_when_credits_run_out(monkeypatch, capsys):
    from pipeline import run as runmod
    from pipeline.search import QuotaExhausted

    class NoCredits:
        def search(self, *a, **k):
            raise QuotaExhausted("Exa credits are used up. Top up at dashboard.exa.ai, then re-run.")

        def search_people(self, *a, **k):
            raise QuotaExhausted("x")

    monkeypatch.setattr(runmod, "ExaSearch", NoCredits)
    monkeypatch.setattr(runmod, "GeminiExtract", lambda: object())
    code = runmod.main(["--source", "csv", "--no-write", "--limit", "6", "--workers", "2"])
    assert code == 2 and "STOPPED" in capsys.readouterr().out


# ---------------------------------------------------------------- partner checks cost control

def test_quiet_investors_get_one_partner_check_and_pipeline_investors_get_three(rf):
    from pipeline.models import Person

    class Many(PeopleClients):
        def search_people(self, vc, exclude=None):
            return [self.page]

        def extract_people(self, vc, pages):
            return []
    people = [Person(f"Person {i} Name", "Partner", discovered_at=__import__("datetime").datetime(2026, 10, 1)) for i in range(5)]
    c = Many()
    process_vc(make_vc(status="to_contact"), c, c, rf, TODAY, people)
    assert len(c.person_searches) == 1                       # nothing logistics, not in the pipeline: just the top person
    c2 = Many()
    process_vc(make_vc(status="waiting", last_contacted_at=TODAY), c2, c2, rf, TODAY, people)
    assert len(c2.person_searches) == 3                      # already in your pipeline: full check
