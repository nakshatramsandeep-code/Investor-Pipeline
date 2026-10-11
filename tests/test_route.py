"""One test per row of the routing table, plus the edges between rows."""
import pytest
from conftest import TODAY, ago, make_signal, make_vc

from pipeline.score import decide, fit_gate, route


def r(vc, signals, rf, fit_ok=True):
    return route(vc, signals, fit_ok, rf.weights, TODAY)


def test_rule1_fit_fails(rf):
    assert r(make_vc(), [make_signal()], rf, fit_ok=False) == (1, "skip")


def test_rule1_declined_even_with_signal(rf):
    assert r(make_vc(status="declined"), [make_signal()], rf) == (1, "skip")


def test_rule2_contacted_after_signal_holds(rf):
    vc = make_vc(status="waiting", last_contacted_at=ago(1))
    assert r(vc, [make_signal(days_ago=5)], rf) == (2, "hold")


def test_rule2_same_day_counts_as_contacted_after(rf):
    vc = make_vc(status="waiting", last_contacted_at=ago(5))
    assert r(vc, [make_signal(days_ago=5)], rf) == (2, "hold")


def test_rule3_fresh_signal_new_vc_reaches_out(rf):
    assert r(make_vc(), [make_signal()], rf) == (3, "reach_out")


def test_rule4_fresh_signal_after_outreach_nudges(rf):
    vc = make_vc(status="waiting", last_contacted_at=ago(20))
    assert r(vc, [make_signal(days_ago=2)], rf) == (4, "nudge")


def test_rule5_waiting_over_10_days_quiet_nudges(rf):
    assert r(make_vc(status="waiting", last_contacted_at=ago(11)), [], rf) == (5, "nudge")


def test_rule5_waiting_unknown_contact_date_nudges(rf):
    assert r(make_vc(status="waiting"), [], rf) == (5, "nudge")


def test_rule6_waiting_exactly_10_days_still_holds(rf):
    assert r(make_vc(status="waiting", last_contacted_at=ago(10)), [], rf) == (6, "hold")


def test_rule6_recently_contacted_quiet_holds(rf):
    assert r(make_vc(status="waiting", last_contacted_at=ago(3)), [], rf) == (6, "hold")


def test_rule7_no_signal_goes_to_quiet_list(rf):
    assert r(make_vc(), [], rf) == (7, "quiet_list")


# ---------------------------------------------------------------- fit gate

def test_fit_not_investing(rf):
    ok, _, why = fit_gate(make_vc(is_investing=False, flag="NOT CURRENTLY INVESTING"), rf.fit)
    assert not ok and "investing" in why.lower()


def test_fit_too_late_stage(rf):
    assert not fit_gate(make_vc(stage_focus="Series A+"), rf.fit)[0]


@pytest.mark.parametrize("stage", ["Seed", "Pre-seed, Seed", "Seed to growth", "Seed to Series A", "Seed+", "Corporate"])
def test_fit_stages_that_pass(rf, stage):
    assert fit_gate(make_vc(stage_focus=stage), rf.fit)[0]


def test_fit_soft_penalty_lowers_multiplier_but_passes(rf):
    ok, mult, _ = fit_gate(make_vc(flag="Verify geography"), rf.fit)
    assert ok and mult < 1


# ---------------------------------------------------------------- score + decision

def test_fresh_beats_stale_signal(rf):
    fresh = decide(make_vc(), [make_signal(days_ago=2)], rf.fit, rf.weights, TODAY)
    stale = decide(make_vc(), [make_signal(days_ago=28)], rf.fit, rf.weights, TODAY)
    assert fresh.score > stale.score > 0


def test_stronger_signal_type_scores_higher(rf):
    a = decide(make_vc(), [make_signal(type="portfolio_investment")], rf.fit, rf.weights, TODAY)
    b = decide(make_vc(), [make_signal(type="event_appearance")], rf.fit, rf.weights, TODAY)
    assert a.score > b.score


def test_score_is_capped_at_100(rf):
    sigs = [make_signal(days_ago=0, url=f"https://x.com/{i}") for i in range(6)]
    assert decide(make_vc(geo="India"), sigs, rf.fit, rf.weights, TODAY).score <= 100


def test_already_shown_signal_scores_lower_than_new_one(rf):
    sig = [make_signal(days_ago=4)]
    new = decide(make_vc(last_signal_date=None), sig, rf.fit, rf.weights, TODAY)
    seen = decide(make_vc(last_signal_date=ago(4)), sig, rf.fit, rf.weights, TODAY)
    assert new.score > seen.score


def test_skip_never_overwrites_last_signal_date(rf):
    d = decide(make_vc(is_investing=False), [], rf.fit, rf.weights, TODAY)
    assert d.action == "skip" and d.score == 0 and d.last_signal_date is None


def test_quiet_does_not_clear_last_signal_date(rf):
    d = decide(make_vc(), [], rf.fit, rf.weights, TODAY)
    assert d.last_signal_date is None   # None -> COALESCE keeps the stored value


def test_signal_sets_last_signal_date_to_newest(rf):
    d = decide(make_vc(), [make_signal(days_ago=9, url="u1"), make_signal(days_ago=2, url="u2")], rf.fit, rf.weights, TODAY)
    assert d.last_signal_date == ago(2)


def test_news_source_scores_below_own_site_for_same_signal(rf):
    from dataclasses import replace
    own = replace(make_signal(days_ago=3), source_tier="own_site")
    news = replace(make_signal(days_ago=3), source_tier="news")
    a = decide(make_vc(), [own], rf.fit, rf.weights, TODAY).score
    b = decide(make_vc(), [news], rf.fit, rf.weights, TODAY).score
    assert a > b


def test_in_conversation_holds_even_with_a_fresh_signal(rf):
    vc = make_vc(status="in_conversation", last_contacted_at=ago(20))
    assert r(vc, [make_signal(days_ago=1)], rf) == (2, "hold")


def test_in_conversation_without_signal_says_so(rf):
    d = decide(make_vc(status="in_conversation"), [], rf.fit, rf.weights, TODAY)
    assert d.action == "hold" and "In conversation" in d.summary


def test_declined_still_beats_in_conversation_rule(rf):
    assert r(make_vc(status="declined"), [], rf) == (1, "skip")


def test_approved_always_holds(rf):
    assert r(make_vc(status="approved"), [make_signal()], rf) == (2, "hold")
    d = decide(make_vc(status="approved"), [], rf.fit, rf.weights, TODAY)
    assert d.action == "hold" and "Approved" in d.summary


def test_logistics_activity_outscores_general_activity(rf):
    from dataclasses import replace
    logi = decide(make_vc(), [replace(make_signal(), relevance="logistics")], rf.fit, rf.weights, TODAY).score
    gen = decide(make_vc(), [replace(make_signal(), relevance="general")], rf.fit, rf.weights, TODAY).score
    assert logi > gen > 0


def test_many_general_signals_do_not_outrank_a_logistics_signal(rf):
    from dataclasses import replace
    logi = [replace(make_signal(days_ago=3), relevance="logistics")]
    gen = [replace(make_signal(days_ago=3, url=f"https://t.com/{i}"), relevance="general") for i in range(5)]
    assert decide(make_vc(), logi, rf.fit, rf.weights, TODAY).score > decide(make_vc(), gen, rf.fit, rf.weights, TODAY).score
