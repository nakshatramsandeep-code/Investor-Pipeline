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

    def search(self, vc, kw, today, window, exclude=None):
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
