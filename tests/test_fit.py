"""Fit is a rating (high / medium / low), separate from timing. Each concern counts once."""
from dataclasses import replace

import pytest
import yaml
from conftest import TODAY, make_signal, make_vc

from pipeline.config import ConfigError, load_fit_rules
from pipeline.score import decide, fit_assess


def strong(url="https://t.com/a", days_ago=2):
    return [replace(make_signal(days_ago=days_ago, url=url), relevance="logistics", source_tier="own_site")]


def level(rf, **kw):
    return fit_assess(make_vc(**kw), rf.fit, [])


def test_a_logistics_first_seed_fund_is_high_fit(rf):
    f = level(rf, thesis="Supply chain and mobility", stage_focus="Pre-seed, Seed")
    assert f["level"] == "high" and f["concerns"] == []


def test_a_focused_fund_with_no_news_is_still_high_fit(rf):
    """Fit does not depend on signals: a logistics-native seed fund rates high even in a quiet month."""
    f = fit_assess(make_vc(thesis="Logistics tech", stage_focus="Seed", geo="India"), rf.fit, [])
    assert f["level"] == "high"


def test_a_big_fund_is_medium_and_the_cheque_worry_counts_once(rf):
    f = level(rf, thesis="Logistics as a core thesis", stage_focus="Seed to growth", geo="US",
              flag="Large fund; check seed cheque fit", background="$998M Fund VI names logistics as core thesis")
    assert f["level"] == "medium"
    assert sum("seed cheque may not" in c for c in f["concerns"]) == 1      # growth stage + cheque flag = one concern


def test_a_generalist_early_stage_fund_with_track_record_is_medium(rf):
    f = level(rf, thesis="India early-stage", stage_focus="Seed", geo="India", background="Backed Porter")
    assert f["level"] == "medium" and "Logistics track record" in f["good"]


def test_a_generalist_with_nothing_else_is_low(rf):
    assert level(rf, thesis="Consumer brands", stage_focus="Seed to Series B", geo="US")["level"] == "low"


def test_unverified_data_is_a_badge_not_a_penalty(rf):
    clean = level(rf, thesis="Dedicated logistics fund", stage_focus="Seed", geo="Global")
    flagged = level(rf, thesis="Dedicated logistics fund", stage_focus="Seed", geo="Global", flag="Generic name; verify identity")
    assert flagged["points"] == clean["points"] and flagged["level"] == clean["level"] and flagged["badges"] and not clean["badges"]


def test_a_competitor_in_a_signal_is_a_concern(rf):
    rules = replace(rf.fit, competitors=["RivalCo"])
    sig = [replace(make_signal(days_ago=2), summary="Led a seed round in RivalCo, a freight visibility startup.", relevance="logistics")]
    f = fit_assess(make_vc(thesis="logistics"), rules, sig)
    assert any("RivalCo" in c for c in f["concerns"])
    assert not any("conflict" in c.lower() for c in fit_assess(make_vc(thesis="logistics"), rules, strong())["concerns"])


def test_conflict_matching_is_whole_word(rf):
    rules = replace(rf.fit, competitors=["Flex"])
    sig = [replace(make_signal(), summary="Backed Flexport's new venture arm.")]
    assert not any("conflict" in c.lower() for c in fit_assess(make_vc(thesis="logistics"), rules, sig)["concerns"])


def test_timing_score_ignores_fit(rf):
    """Same signals, very different funds: the timing score is identical. Fit is shown beside it, not multiplied in."""
    focused = make_vc(thesis="Supply chain and mobility", stage_focus="Pre-seed, Seed")
    big = make_vc(thesis="Consumer", stage_focus="Seed to growth", flag="Large fund; check seed cheque fit")
    a, b = decide(focused, strong(), rf.fit, rf.weights, TODAY), decide(big, strong(), rf.fit, rf.weights, TODAY)
    assert a.score == b.score and a.fit["level"] == "high" and b.fit["level"] == "low"


def test_a_low_fit_fund_is_not_a_reach_out_even_with_a_fresh_logistics_signal(rf):
    big = make_vc(thesis="Consumer", stage_focus="Seed to growth", flag="Large fund; check seed cheque fit")
    assert decide(big, strong(), rf.fit, rf.weights, TODAY).action == "quiet_list"


def test_a_high_fit_fund_with_a_fresh_logistics_signal_is_a_reach_out(rf):
    d = decide(make_vc(thesis="Supply chain", stage_focus="Seed"), strong(), rf.fit, rf.weights, TODAY)
    assert d.action == "reach_out" and d.fit["level"] == "high"


def test_every_decision_carries_a_fit_even_with_no_signals(rf):
    """A fund with no news must still have a fit (this was the profile crash: an empty fit object)."""
    d = decide(make_vc(thesis="Logistics tech", stage_focus="Seed"), [], rf.fit, rf.weights, TODAY)
    assert d.fit["level"] == "high" and d.fit["good"]


def test_a_skipped_fund_is_excluded_with_a_reason(rf):
    d = decide(make_vc(stage_focus="Growth equity"), [], rf.fit, rf.weights, TODAY)
    assert d.action == "skip" and d.fit["level"] == "excluded" and d.fit["concerns"]


def test_the_score_does_not_depend_on_what_was_shown_before(rf):
    vc_seen = make_vc(last_signal_date=TODAY)
    vc_new = make_vc(last_signal_date=None)
    assert decide(vc_seen, strong(), rf.fit, rf.weights, TODAY).score == decide(vc_new, strong(), rf.fit, rf.weights, TODAY).score


def test_a_fund_with_only_content_cannot_outrank_one_with_a_deal(rf):
    vc = make_vc(thesis="Supply chain and mobility", stage_focus="Seed", geo="India")
    posts = [replace(make_signal(days_ago=1, url=f"https://t.com/p{i}", type="blog_post"), relevance="logistics", source_tier="own_site") for i in range(4)]
    deal = [replace(make_signal(days_ago=9, type="portfolio_investment"), relevance="logistics", source_tier="own_site")]
    a = decide(vc, posts, rf.fit, rf.weights, TODAY).score
    b = decide(vc, deal, rf.fit, rf.weights, TODAY).score
    assert a <= round(100 * rf.weights.content_only_cap) and b > a


def test_a_live_relationship_shows_no_pitch_score(rf):
    d = decide(make_vc(status="in_conversation"), strong(), rf.fit, rf.weights, TODAY)
    assert d.action == "hold" and d.score == 0


def test_fit_points_are_validated(tmp_path):
    from pathlib import Path
    src = yaml.safe_load((Path(__file__).resolve().parent.parent / "run_files" / "fit_rules.yaml").read_text(encoding="utf-8"))
    del src["fit_points"]["conflict"]
    p = tmp_path / "fit_rules.yaml"
    p.write_text(yaml.dump(src))
    with pytest.raises(ConfigError, match="conflict"):
        load_fit_rules(p)
    src["fit_points"]["conflict"] = 99
    p.write_text(yaml.dump(src))
    with pytest.raises(ConfigError):
        load_fit_rules(p)


def test_unrelated_news_cannot_outrank_logistics_activity(rf):
    """DCVC-style: several deals, none about logistics, must score below a single logistics blog post."""
    vc = make_vc(thesis="Deep tech", stage_focus="Seed")
    deals = [replace(make_signal(days_ago=2, url=f"https://t.com/d{i}", type="portfolio_investment"), relevance="general", source_tier="news") for i in range(4)]
    post = [replace(make_signal(days_ago=7, type="blog_post"), relevance="logistics", source_tier="own_site")]
    assert decide(vc, deals, rf.fit, rf.weights, TODAY).score <= round(100 * rf.weights.no_logistics_cap)
    assert decide(vc, post, rf.fit, rf.weights, TODAY).score > decide(vc, deals, rf.fit, rf.weights, TODAY).score


def test_the_formula_is_frozen(rf):
    """Golden scores. If this fails you changed the formula: that is allowed, but bump score_version in weights.yaml
    and update the numbers here on purpose, so a score never changes silently."""
    assert rf.weights.score_version == 3
    vc = make_vc(thesis="Supply chain", stage_focus="Seed", geo="US")
    deal = [replace(make_signal(days_ago=3, type="portfolio_investment"), relevance="logistics", source_tier="news")]
    post = [replace(make_signal(days_ago=7, type="blog_post"), relevance="logistics", source_tier="own_site")]
    gen = [replace(make_signal(days_ago=2, type="portfolio_investment"), relevance="general", source_tier="news")]
    got = [decide(vc, s, rf.fit, rf.weights, TODAY).score for s in (deal, post, gen)]
    assert got == [82, 32, 30], got
