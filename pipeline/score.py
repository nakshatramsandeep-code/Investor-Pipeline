"""Fit gate, score, and the routing table. Plain code, no LLM, unit-tested per row."""
from __future__ import annotations

from datetime import date

from .config import FitRules, Weights
from .models import VC, Decision, Signal, effective_status

ACTION_ORDER = {"reach_out": 0, "nudge": 1, "hold": 2, "quiet_list": 3, "skip": 4}


def fit_gate(vc: VC, rules: FitRules) -> tuple[bool, float, str]:
    """(passes, soft multiplier 0..1, reason if it fails)."""
    flag, stage = vc.flag.lower(), vc.stage_focus.lower()
    if rules.require_investing and not vc.is_investing:
        return False, 0.0, "Not currently investing"
    for bad in rules.hard_exclude_flags:
        if bad in flag:
            return False, 0.0, f"Flagged: {vc.flag}"
    if not any(m in stage for m in rules.stage_must_match_any):
        return False, 0.0, f"Stage focus '{vc.stage_focus}' does not include Seed"
    mult = 1.0
    haystack = f"{flag} {stage}"
    for key, factor in rules.soft_penalties.items():
        if key in haystack:
            mult *= factor
    return True, mult, ""


def signal_strength(signals: list[Signal], vc: VC, w: Weights, today: date) -> float:
    """0..1. Best single signal (type x source x topic x recency) plus small bonuses. Every bonus is
    scaled by topic relevance, so five general signals can't outrank a logistics signal."""
    if not signals:
        return 0.0
    scored = []
    for s in signals:
        age = max((today - s.published).days, 0)
        tier = w.source_tiers.get(s.source_tier, 1.0)
        topic = w.relevance.get(s.relevance, 1.0)
        if s.relevance == "general" and s.type in ("interview", "blog_post", "event_appearance", "other"):
            topic *= w.general_content_factor      # off-topic talk is not a buying signal
        scored.append((w.signal_types[s.type] * tier * topic * 0.5 ** (age / w.half_life_days), topic))
    scored.sort(reverse=True)
    best, best_topic = scored[0]
    strength = best + w.extra_signal * sum(t for _, t in scored[1:4])
    newest = max(s.published for s in signals)
    if vc.last_signal_date is None or newest > vc.last_signal_date:
        strength += w.new_since_last_run * best_topic
    geo = vc.geo.lower()
    if any(g in geo for g in w.preferred_geos):
        strength += w.preferred_geo * best_topic
    return min(strength, 1.0)


def _days_since(d: date | None, today: date) -> int | None:
    return None if d is None else (today - d).days


def hot_signals(signals: list[Signal], w: Weights, today: date) -> list[Signal]:
    """The signals that justify a cold approach: about logistics, and recent."""
    return [s for s in signals if s.relevance == "logistics" and (today - s.published).days <= w.reach_out_max_age]


def route(vc: VC, signals: list[Signal], fit_ok: bool, w: Weights, today: date) -> tuple[int, str]:
    """Decision table, first match wins. Returns (rule number, action)."""
    status = effective_status(vc, today)
    newest = max((s.published for s in signals), default=None)
    since_contact = _days_since(vc.last_contacted_at, today)
    waiting = status == "waiting"
    overdue = waiting and (since_contact is None or since_contact >= w.waiting_nudge_days)

    if not fit_ok or status == "declined":
        return 1, "skip"
    if status in ("in_conversation", "approved", "closed"):
        return 2, "hold"                                   # a live conversation: don't nudge or re-pitch
    if overdue:
        return 5, "nudge"                                  # contacted a week ago, no reply: silence is a reason
    if newest and vc.last_contacted_at and vc.last_contacted_at >= newest:
        return 2, "hold"                                   # he already reached out after the signal
    if status == "to_contact" and hot_signals(signals, w, today):
        return 3, "reach_out"                              # a recent logistics signal: a reason to approach now
    if newest and waiting:
        return 4, "nudge"                                  # a fresh signal since he reached out
    if waiting:
        return 6, "hold"
    return 7, "quiet_list"


def _summarise(signals: list[Signal]) -> str:
    top = sorted(signals, key=lambda s: s.published, reverse=True)[:2]
    return " | ".join(f"{s.type.replace('_', ' ').capitalize()} ({s.published.isoformat()}): {s.summary}"
                      for s in top)


def decide(vc: VC, signals: list[Signal], rules: FitRules, w: Weights, today: date) -> Decision:
    fit_ok, fit_mult, reason = fit_gate(vc, rules)
    rule, action = route(vc, signals, fit_ok, w, today)
    newest = max((s.published for s in signals), default=None)
    waited = _days_since(vc.last_contacted_at, today)

    if action == "skip":
        why = reason or "Already declined"
        return Decision(rule, action, 0, f"Skipped: {why}", None)
    if effective_status(vc, today) == "closed" and not signals:
        return Decision(rule, action, 0, "Deal closed.", None)
    if effective_status(vc, today) == "approved" and not signals:
        return Decision(rule, action, 0, "Approved. Keep the relationship warm.", None)
    if effective_status(vc, today) == "in_conversation" and not signals:
        return Decision(rule, action, 0, "In conversation. Keep the thread going.", None)
    if signals:
        score = round(100 * signal_strength(signals, vc, w, today) * fit_mult)
        summary = _summarise(signals)
        if rule == 5:
            summary = f"Waiting {waited} days for a reply. " + summary if waited is not None else summary
        if action == "reach_out" and score < w.reach_out_min_score:
            return Decision(7, "quiet_list", score, "Weak signal. " + summary, newest)   # below the floor
        if action == "quiet_list" and effective_status(vc, today) == "to_contact":
            if not any(s.relevance == "logistics" for s in signals):
                summary = "Active, but nothing about logistics. " + summary
            else:
                summary = f"No recent logistics signal (older than {w.reach_out_max_age} days). " + summary
        return Decision(rule, action, score, summary, newest)
    if action == "nudge":
        n = waited
        text = f"{n} days" if n is not None else "an unknown time"
        urgency = max(0, (n or w.waiting_nudge_days) - w.waiting_nudge_days)
        score = min(60, w.quiet_nudge_score + 2 * urgency)
        return Decision(rule, action, round(score * fit_mult), f"No new signal. Waiting on a reply for {text}.", None)
    return Decision(rule, action, 0, f"No signal in the last {w.window_days} days.", None)
