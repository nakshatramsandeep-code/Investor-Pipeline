"""Loads and schema-checks the run files. context.md is prompt text only; the two
YAML files are read by code, so a typo in them must fail loudly before any API spend."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .sources import SourceRules

SIGNAL_TYPES = (
    "portfolio_investment", "fund_announcement", "interview",
    "blog_post", "event_appearance", "other",
)

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "run_files"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FitRules:
    topic_keywords: list[str]
    require_investing: bool
    stage_must_match_any: list[str]
    hard_exclude_flags: list[str]
    soft_penalties: dict[str, float]
    fit_factors: dict[str, float]
    growth_stage_words: list[str]
    competitors: list[str]


@dataclass(frozen=True)
class Weights:
    signal_types: dict[str, float]
    window_days: int
    half_life_days: float
    extra_signal: float
    new_since_last_run: float
    preferred_geo: float
    preferred_geos: list[str]
    waiting_nudge_days: int
    quiet_nudge_score: int
    reach_out_min_score: int
    reach_out_max_age: int
    general_content_factor: float
    content_only_cap: float
    sched_pipeline_days: int
    sched_reach_days: int
    sched_dormant_days: int
    source_tiers: dict[str, float]
    relevance: dict[str, float]


@dataclass(frozen=True)
class RunFiles:
    context: str
    fit: FitRules
    weights: Weights
    sources: SourceRules


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"missing run file: {path}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping")
    if data.get("version") != 1:
        raise ConfigError(f"{path.name}: unsupported or missing version (expected 1)")
    return data


def _get(d: dict, key: str, typ, where: str):
    if key not in d:
        raise ConfigError(f"{where}: missing '{key}'")
    v = d[key]
    if typ is float and isinstance(v, int) and not isinstance(v, bool):
        v = float(v)
    if not isinstance(v, typ) or (typ is not bool and isinstance(v, bool)):
        raise ConfigError(f"{where}: '{key}' must be {typ.__name__}, got {type(v).__name__}")
    return v


def _str_list(d: dict, key: str, where: str, lower: bool = True) -> list[str]:
    items = _get(d, key, list, where)
    if not all(isinstance(i, str) and i.strip() for i in items):
        raise ConfigError(f"{where}: '{key}' must be a list of non-empty strings")
    return [i.strip().lower() if lower else i.strip() for i in items]


def _unit(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise ConfigError(f"{where}: must be a number between 0 and 1, got {v!r}")
    return float(v)


def _pos_int(v, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ConfigError(f"schedule.{name} must be a whole number of days, 1 or more")
    return v


def _fit_factors(d: dict, w: str) -> dict[str, float]:
    f = _get(d, "fit_factors", dict, w)
    missing = {"growth_stage", "generalist_thesis", "conflict"} - set(f)
    if missing:
        raise ConfigError(f"{w}: fit_factors is missing {sorted(missing)}")
    return {k: _unit(v, f"{w}: fit_factors[{k}]") for k, v in f.items()}


def _str_list_or_empty(d: dict, key: str, where: str) -> list[str]:
    items = d.get(key) or []
    if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
        raise ConfigError(f"{where}: '{key}' must be a list of non-empty strings")
    return [i.strip() for i in items]


def load_fit_rules(path: Path) -> FitRules:
    d = _load_yaml(path)
    w = path.name
    penalties = _get(d, "soft_penalties", dict, w)
    return FitRules(
        topic_keywords=_str_list(d, "topic_keywords", w, lower=False),
        require_investing=_get(d, "require_investing", bool, w),
        stage_must_match_any=_str_list(d, "stage_must_match_any", w),
        hard_exclude_flags=_str_list(d, "hard_exclude_flags", w),
        soft_penalties={str(k).lower(): _unit(v, f"{w}: soft_penalties[{k}]") for k, v in penalties.items()},
        fit_factors=_fit_factors(d, w),
        growth_stage_words=_str_list(d, "growth_stage_words", w),
        competitors=_str_list_or_empty(d, "competitors", w),
    )


def load_weights(path: Path) -> Weights:
    d = _load_yaml(path)
    w = path.name
    types = _get(d, "signal_types", dict, w)
    unknown = set(types) ^ set(SIGNAL_TYPES)
    if unknown:
        raise ConfigError(f"{w}: signal_types must be exactly {list(SIGNAL_TYPES)}; mismatch: {sorted(unknown)}")
    rec = _get(d, "recency", dict, w)
    bon = _get(d, "bonuses", dict, w)
    rou = _get(d, "routing", dict, w)
    window = _get(rec, "window_days", int, f"{w}: recency")
    half = _get(rec, "half_life_days", float, f"{w}: recency")
    if window <= 0 or half <= 0:
        raise ConfigError(f"{w}: recency values must be positive")
    return Weights(
        signal_types={k: _unit(v, f"{w}: signal_types[{k}]") for k, v in types.items()},
        window_days=window,
        half_life_days=half,
        extra_signal=_unit(_get(bon, "extra_signal", float, f"{w}: bonuses"), "extra_signal"),
        new_since_last_run=_unit(_get(bon, "new_since_last_run", float, f"{w}: bonuses"), "new_since_last_run"),
        preferred_geo=_unit(_get(bon, "preferred_geo", float, f"{w}: bonuses"), "preferred_geo"),
        preferred_geos=_str_list(d, "preferred_geos", w),
        waiting_nudge_days=_get(rou, "waiting_nudge_days", int, f"{w}: routing"),
        quiet_nudge_score=_get(rou, "quiet_nudge_score", int, f"{w}: routing"),
        reach_out_max_age=_get(rou, "reach_out_max_signal_age_days", int, f"{w}: routing"),
        reach_out_min_score=_get(rou, "reach_out_min_score", int, f"{w}: routing"),
        sched_pipeline_days=_pos_int(_get(_get(d, "schedule", dict, w), "pipeline_days", int, f"{w}: schedule"), "pipeline_days"),
        sched_reach_days=_pos_int(_get(_get(d, "schedule", dict, w), "reach_days", int, f"{w}: schedule"), "reach_days"),
        sched_dormant_days=_pos_int(_get(_get(d, "schedule", dict, w), "dormant_days", int, f"{w}: schedule"), "dormant_days"),
        general_content_factor=_unit(_get(d, "general_content_factor", float, w), "general_content_factor"),
        content_only_cap=_unit(_get(d, "content_only_cap", float, w), "content_only_cap"),
        relevance={k: _unit(v, f"{w}: relevance[{k}]") for k, v in _get(d, "relevance", dict, w).items()},
        source_tiers={k: _unit(v, f"{w}: source_tiers[{k}]") for k, v in _get(d, "source_tiers", dict, w).items()},
    )


def load_sources(path: Path) -> SourceRules:
    d = _load_yaml(path)
    w = path.name
    return SourceRules(
        allow_social=_get(d, "allow_social", bool, w),
        allow_unlisted=_get(d, "allow_unlisted", bool, w),
        news=tuple(_str_list(d, "news", w)),
        social=tuple(_str_list(d, "social", w)),
        blocked=tuple(_str_list(d, "blocked", w)),
    )


def load_run_files(directory: Path = DEFAULT_DIR) -> RunFiles:
    ctx_path = directory / "context.md"
    try:
        context = ctx_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"missing run file: {ctx_path}") from None
    if not context.strip():
        raise ConfigError("context.md is empty")
    return RunFiles(
        context=context,
        fit=load_fit_rules(directory / "fit_rules.yaml"),
        weights=load_weights(directory / "weights.yaml"),
        sources=load_sources(directory / "sources.yaml"),
    )
