"""Fit is part of the score: a fund that fits badly cannot rank 'strong' on freshness alone."""
from dataclasses import replace

import pytest
import yaml
from conftest import TODAY, make_signal, make_vc

from pipeline.config import ConfigError, load_fit_rules
from pipeline.score import decide, fit_report


def strong(url="https://t.com/a"):
    return [replace(make_signal(days_ago=2, url=url), relevance="logistics", source_tier="own_site")]


def test_a_logistics_first_seed_fund_has_no_penalty(rf):
    mult, reasons = fit_report(make_vc(thesis="Supply chain and mobility", stage_focus="Pre-seed, Seed"), rf.fit, [])
    assert mult == 1.0 and reasons == []


def test_a_growth_stage_generalist_big_fund_scores_far_below_a_focused_seed_fund(rf):
    focused = make_vc(thesis="Supply chain and mobility", stage_focus="Pre-seed, Seed")
    big = make_vc(thesis="Logistics as a core thesis", stage_focus="Seed to growth", flag="Large fund; check seed cheque fit")
    a = decide(focused, strong(), rf.fit, rf.weights, TODAY)
    b = decide(big, strong(), rf.fit, rf.weights, TODAY)
    assert a.score > b.score and b.fit["multiplier"] < 0.65
    assert any("growth" in r.lower() for r in b.fit["reasons"]) and any("seed cheque" in r.lower() for r in b.fit["reasons"])


def test_a_generalist_thesis_is_scored_down(rf):
    mult, reasons = fit_report(make_vc(thesis="India seed generalist"), rf.fit, [])
    assert mult == pytest.approx(rf.fit.fit_factors["generalist_thesis"]) and "Generalist" in reasons[0]


def test_a_fund_with_a_poor_fit_does_not_rank_strong(rf):
    big = make_vc(thesis="Logistics as a core thesis", stage_focus="Seed to growth", flag="Large fund; check seed cheque fit")
    assert decide(big, strong(), rf.fit, rf.weights, TODAY).score < 70            # below the 'strong' band


def test_a_competitor_in_a_signal_flags_a_conflict(rf):
    from dataclasses import replace as rep
    rules = rep(rf.fit, competitors=["RivalCo"])
    sig = [replace(make_signal(days_ago=2), summary="Led a seed round in RivalCo, a freight visibility startup.", relevance="logistics")]
    mult, reasons = fit_report(make_vc(thesis="logistics"), rules, sig)
    assert mult == pytest.approx(rules.fit_factors["conflict"]) and "RivalCo" in reasons[0]
    assert fit_report(make_vc(thesis="logistics"), rules, strong())[0] == 1.0          # no mention, no conflict


def test_conflict_matching_is_whole_word(rf):
    from dataclasses import replace as rep
    rules = rep(rf.fit, competitors=["Flex"])
    sig = [replace(make_signal(), summary="Backed Flexport's new venture arm.")]
    assert fit_report(make_vc(thesis="logistics"), rules, sig)[0] == 1.0


def test_decision_carries_the_fit_report_for_the_dashboard(rf):
    d = decide(make_vc(thesis="India seed generalist"), strong(), rf.fit, rf.weights, TODAY)
    assert d.fit["multiplier"] < 1 and d.fit["reasons"]


def test_fit_factors_are_validated(tmp_path):
    from pathlib import Path
    src = yaml.safe_load((Path(__file__).resolve().parent.parent / "run_files" / "fit_rules.yaml").read_text())
    del src["fit_factors"]["conflict"]
    p = tmp_path / "fit_rules.yaml"
    p.write_text(yaml.dump(src))
    with pytest.raises(ConfigError, match="conflict"):
        load_fit_rules(p)
    src["fit_factors"]["conflict"] = 3
    p.write_text(yaml.dump(src))
    with pytest.raises(ConfigError):
        load_fit_rules(p)
