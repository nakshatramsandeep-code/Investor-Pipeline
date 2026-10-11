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
    vc = make_vc(status="waiting", last_contacted_at=ago(5))        # pitched 5 days ago, signal 2 days ago
    assert r(vc, [make_signal(days_ago=2)], rf) == (4, "nudge")


def test_overdue_waiting_nudges_even_when_he_contacted_after_the_signal(rf):
    vc = make_vc(status="waiting", last_contacted_at=ago(9))        # old signal, contacted after it, 9 days of silence
    assert r(vc, [make_signal(days_ago=15)], rf) == (5, "nudge")


def test_overdue_waiting_nudges_with_a_fresh_signal_too(rf):
    assert r(make_vc(status="waiting", last_contacted_at=ago(20)), [make_signal(days_ago=2)], rf) == (5, "nudge")


def test_rule5_waiting_over_a_week_quiet_nudges(rf):
    assert r(make_vc(status="waiting", last_contacted_at=ago(11)), [], rf) == (5, "nudge")


def test_rule5_waiting_unknown_contact_date_nudges(rf):
    assert r(make_vc(status="waiting"), [], rf) == (5, "nudge")


def test_waiting_6_days_still_holds_and_7_days_nudges(rf):
    assert r(make_vc(status="waiting", last_contacted_at=ago(6)), [], rf) == (6, "hold")
    assert r(make_vc(status="waiting", last_contacted_at=ago(7)), [], rf) == (5, "nudge")


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


def test_weak_signal_below_the_floor_is_not_reach_out(rf):
    from dataclasses import replace
    weak = [replace(make_signal(days_ago=14, type="other"), relevance="logistics", source_tier="news")]   # recent, on-topic, but faint
    d = decide(make_vc(), weak, rf.fit, rf.weights, TODAY)
    assert d.score < rf.weights.reach_out_min_score and d.action == "quiet_list" and d.summary.startswith("Weak signal")


def test_strong_signal_clears_the_floor(rf):
    from dataclasses import replace
    strong = [replace(make_signal(days_ago=2), relevance="logistics", source_tier="own_site")]
    d = decide(make_vc(), strong, rf.fit, rf.weights, TODAY)
    assert d.action == "reach_out" and d.score >= rf.weights.reach_out_min_score


def test_general_interview_counts_far_less_than_a_general_deal(rf):
    from dataclasses import replace
    deal = decide(make_vc(), [replace(make_signal(days_ago=2, type="portfolio_investment"), relevance="general")], rf.fit, rf.weights, TODAY).score
    talk = decide(make_vc(), [replace(make_signal(days_ago=2, type="interview"), relevance="general")], rf.fit, rf.weights, TODAY).score
    assert deal > talk


def test_overdue_nudge_score_grows_with_the_wait(rf):
    short = decide(make_vc(status="waiting", last_contacted_at=ago(8)), [], rf.fit, rf.weights, TODAY).score
    long = decide(make_vc(status="waiting", last_contacted_at=ago(20)), [], rf.fit, rf.weights, TODAY).score
    assert long > short


def test_overdue_nudge_summary_says_how_long(rf):
    d = decide(make_vc(status="waiting", last_contacted_at=ago(9)), [make_signal(days_ago=2)], rf.fit, rf.weights, TODAY)
    assert d.action == "nudge" and d.summary.startswith("Waiting 9 days")


def test_reach_out_needs_a_logistics_signal(rf):
    assert r(make_vc(), [make_signal(relevance="general")], rf) == (7, "quiet_list")
    assert r(make_vc(), [make_signal(relevance="logistics")], rf) == (3, "reach_out")


def test_reach_out_needs_a_recent_signal(rf):
    assert r(make_vc(), [make_signal(days_ago=14)], rf) == (3, "reach_out")
    assert r(make_vc(), [make_signal(days_ago=15)], rf) == (7, "quiet_list")


def test_dormant_summary_says_why(rf):
    general = decide(make_vc(), [make_signal(relevance="general")], rf.fit, rf.weights, TODAY)
    old = decide(make_vc(), [make_signal(days_ago=20)], rf.fit, rf.weights, TODAY)
    assert general.action == "quiet_list" and general.summary.startswith("Active, but nothing about logistics")
    assert old.action == "quiet_list" and "older than 14 days" in old.summary


def test_declined_investor_comes_back_when_the_revisit_date_arrives(rf):
    due = make_vc(status="declined", revisit_on=ago(1))
    later = make_vc(status="declined", revisit_on=TODAY + __import__("datetime").timedelta(days=30))
    assert r(due, [make_signal()], rf) == (3, "reach_out")
    assert r(later, [make_signal()], rf) == (1, "skip")
    assert r(make_vc(status="declined"), [make_signal()], rf) == (1, "skip")


def test_closed_deal_always_holds(rf):
    assert r(make_vc(status="closed"), [make_signal()], rf) == (2, "hold")
    d = decide(make_vc(status="closed"), [], rf.fit, rf.weights, TODAY)
    assert d.action == "hold" and d.summary == "Deal closed."


def test_today_is_the_local_date_not_the_runners(monkeypatch):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from pipeline import run as runmod

    class Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc).astimezone(tz)   # 01:30 IST on the 12th
    monkeypatch.setattr(runmod, "datetime", Fake)
    assert str(runmod.today_local()) == "2026-10-12"
    monkeypatch.setenv("PIPELINE_TZ", "UTC")
    assert str(runmod.today_local()) == "2026-10-11"
