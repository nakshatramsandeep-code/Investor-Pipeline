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
    thesis_keywords: list[str]
    logistics_portfolio: list[str]
    fit_points: dict[str, int]
    levels: dict[str, int]
    preferred_geos: list[str]
    growth_stage_words: list[str]
    concern_flags: list[str]
    scale_flags: list[str]
    confidence_flags: list[str]
    competitors: list[str]


@dataclass(frozen=True)
class Weights:
    signal_types: dict[str, float]
    window_days: int
    half_life_days: float
    extra_signal: float
    waiting_nudge_days: int
    quiet_nudge_score: int
    reach_out_min_score: int
    reach_out_max_age: int
    general_content_factor: float
    content_only_cap: float
    no_logistics_cap: float
    sched_pipeline_days: int
    sched_reach_days: int
    sched_dormant_days: int
    source_tiers: dict[str, float]
    relevance: dict[str, float]
    score_version: int = 2


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


POINT_KEYS = {"logistics_thesis", "seed_stage", "early_stage", "preferred_geo", "logistics_track_record",
              "concern", "scale_concern", "conflict"}


def _fit_points(d: dict, w: str) -> dict[str, int]:
    f = _get(d, "fit_points", dict, w)
    missing = POINT_KEYS - set(f)
    if missing:
        raise ConfigError(f"{w}: fit_points is missing {sorted(missing)}")
    out = {}
    for k, v in f.items():
        if isinstance(v, bool) or not isinstance(v, int) or not -10 <= v <= 10:
            raise ConfigError(f"{w}: fit_points[{k}] must be a whole number between -10 and 10, got {v!r}")
        out[k] = v
    return out


def _levels(d: dict, w: str) -> dict[str, int]:
    lv = _get(d, "levels", dict, w)
    if set(lv) != {"high", "medium"} or not all(isinstance(v, int) and not isinstance(v, bool) for v in lv.values()):
        raise ConfigError(f"{w}: levels must be exactly high and medium, as whole numbers")
    if lv["high"] <= lv["medium"]:
        raise ConfigError(f"{w}: levels.high must be above levels.medium")
    return dict(lv)


def _str_list_or_empty(d: dict, key: str, where: str) -> list[str]:
    items = d.get(key) or []
    if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
        raise ConfigError(f"{where}: '{key}' must be a list of non-empty strings")
    return [i.strip() for i in items]


def load_fit_rules(path: Path) -> FitRules:
    d = _load_yaml(path)
    w = path.name
    return FitRules(
        topic_keywords=_str_list(d, "topic_keywords", w, lower=False),
        require_investing=_get(d, "require_investing", bool, w),
        stage_must_match_any=_str_list(d, "stage_must_match_any", w),
        hard_exclude_flags=_str_list(d, "hard_exclude_flags", w),
        thesis_keywords=_str_list(d, "thesis_keywords", w),
        logistics_portfolio=_str_list(d, "logistics_portfolio", w, lower=False),
        fit_points=_fit_points(d, w),
        levels=_levels(d, w),
        preferred_geos=_str_list(d, "preferred_geos", w),
        growth_stage_words=_str_list(d, "growth_stage_words", w),
        concern_flags=_str_list(d, "concern_flags", w),
        scale_flags=_str_list(d, "scale_flags", w),
        confidence_flags=_str_list(d, "confidence_flags", w),
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
        waiting_nudge_days=_get(rou, "waiting_nudge_days", int, f"{w}: routing"),
        quiet_nudge_score=_get(rou, "quiet_nudge_score", int, f"{w}: routing"),
        reach_out_max_age=_get(rou, "reach_out_max_signal_age_days", int, f"{w}: routing"),
        reach_out_min_score=_get(rou, "reach_out_min_score", int, f"{w}: routing"),
        sched_pipeline_days=_pos_int(_get(_get(d, "schedule", dict, w), "pipeline_days", int, f"{w}: schedule"), "pipeline_days"),
        sched_reach_days=_pos_int(_get(_get(d, "schedule", dict, w), "reach_days", int, f"{w}: schedule"), "reach_days"),
        sched_dormant_days=_pos_int(_get(_get(d, "schedule", dict, w), "dormant_days", int, f"{w}: schedule"), "dormant_days"),
        general_content_factor=_unit(_get(d, "general_content_factor", float, w), "general_content_factor"),
        content_only_cap=_unit(_get(d, "content_only_cap", float, w), "content_only_cap"),
        no_logistics_cap=_unit(_get(d, "no_logistics_cap", float, w), "no_logistics_cap"),
        relevance={k: _unit(v, f"{w}: relevance[{k}]") for k, v in _get(d, "relevance", dict, w).items()},
        score_version=_get(d, "score_version", int, w),
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
