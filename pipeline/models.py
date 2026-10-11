from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class VC:
    id: int
    name: str
    firm: str
    thesis: str
    status: str                      # to_contact | waiting | declined
    last_contacted_at: date | None
    last_signal_date: date | None    # what Vikram was last shown
    geo: str
    stage_focus: str
    is_investing: bool
    flag: str
    website: str = ""


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    published: date | None           # from the search API, never from the model
    text: str


@dataclass(frozen=True)
class Signal:
    type: str
    summary: str
    quote: str
    url: str
    published: date                  # from the search API
    source_domain: str = ""
    source_tier: str = ""            # own_site | news | social | unlisted
    person_name: str | None = None   # a person at the firm, named on the page
    person_role: str | None = None
    relevance: str = "general"        # logistics | general


@dataclass(frozen=True)
class Person:
    name: str
    role: str = ""
    source_url: str = ""
    source_domain: str = ""
    source_tier: str = ""             # own_site | news | manual (added by Vikram)
    quote: str = ""
    discovered_at: datetime | None = None
    manual: bool = False


@dataclass(frozen=True)
class Decision:
    rule: int                        # which row of the routing table matched
    action: str                      # reach_out | nudge | hold | quiet_list | skip
    score: int                       # 0..100
    summary: str
    last_signal_date: date | None    # None = leave the stored value alone


@dataclass
class VCResult:
    vc: VC
    ok: bool                         # False = search/LLM failed; do NOT write "no signal"
    outcome: str                     # signal | no_signal | skipped_fit | failed
    decision: Decision | None = None
    signals: list[Signal] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    n_pages: int = 0
    error: str | None = None
    people_found: list[Person] = field(default_factory=list)   # new / refreshed, to upsert
    detail: dict = field(default_factory=dict)                 # what the check did, for the history view
