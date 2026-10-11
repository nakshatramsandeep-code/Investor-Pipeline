"""Fit gate, score, and the routing table. Plain code, no LLM, unit-tested per row."""
from __future__ import annotations

from datetime import date

from .config import FitRules, Weights
from .models import VC, Decision, Signal

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
    """0..1. Best single signal (type weight x recency decay) + small bonus per extra one."""
    if not signals:
        return 0.0
    scored = []
    for s in signals:
        age = max((today - s.published).days, 0)
        scored.append(w.signal_types[s.type] * 0.5 ** (age / w.half_life_days))
    scored.sort(reverse=True)
    strength = scored[0] + w.extra_signal * min(len(scored) - 1, 3)
    newest = max(s.published for s in signals)
    if vc.last_signal_date is None or newest > vc.last_signal_date:
        strength += w.new_since_last_run
    geo = vc.geo.lower()
    if any(g in geo for g in w.preferred_geos):
        strength += w.preferred_geo
    return min(strength, 1.0)


def _days_since(d: date | None, today: date) -> int | None:
    return None if d is None else (today - d).days


def route(vc: VC, signals: list[Signal], fit_ok: bool, w: Weights, today: date) -> tuple[int, str]:
    """Decision table, first match wins. Returns (rule number, action)."""
    newest = max((s.published for s in signals), default=None)
    since_contact = _days_since(vc.last_contacted_at, today)
    waiting = vc.status == "waiting"

    if not fit_ok or vc.status == "declined":
        return 1, "skip"
    if newest and vc.last_contacted_at and vc.last_contacted_at >= newest:
        return 2, "hold"                                   # he already reached out after the signal
    if newest and vc.status == "to_contact":
        return 3, "reach_out"
    if newest and waiting:
        return 4, "nudge"
    if not newest and waiting and (since_contact is None or since_contact > w.waiting_nudge_days):
        return 5, "nudge"
    if not newest and waiting:
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

    if action == "skip":
        why = reason or "Already declined"
        return Decision(rule, action, 0, f"Skipped: {why}", None)
    if signals:
        score = round(100 * signal_strength(signals, vc, w, today) * fit_mult)
        return Decision(rule, action, score, _summarise(signals), newest)
    if action == "nudge":
        n = _days_since(vc.last_contacted_at, today)
        waited = f"{n} days" if n is not None else "an unknown time"
        return Decision(rule, action, round(w.quiet_nudge_score * fit_mult),
                        f"No new signal. Waiting on a reply for {waited}.", None)
    return Decision(rule, action, 0, f"No signal in the last {w.window_days} days.", None)
