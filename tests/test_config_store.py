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
