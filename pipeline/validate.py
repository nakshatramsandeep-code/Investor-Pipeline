"""The Python validation gate. Nothing the model says reaches the database unless it
passes every check here. Dates and URLs are taken from the search API, not the model."""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urldefrag

from .config import SIGNAL_TYPES
from .models import VC, Page, Person, Signal
from .names import firm_aliases, mentions
from .sources import SourceRules, classify

MIN_QUOTE_CHARS = 20


def norm_url(u: str) -> str:
    return urldefrag(u.strip())[0].rstrip("/").lower()


def norm_text(s: str) -> str:
    s = (s.replace("’", "'").replace("‘", "'")
          .replace("“", '"').replace("”", '"')
          .replace("–", "-").replace("—", "-").replace(" ", " "))
    return re.sub(r"\s+", " ", s).strip().lower()


def _person(raw, page_text: str) -> tuple[str | None, str | None]:
    """A person is kept only if their full name appears on the page. Otherwise it is
    discarded (the signal itself survives)."""
    if not isinstance(raw, dict):
        return None, None
    name, role = raw.get("name"), raw.get("role")
    if not isinstance(name, str) or len(name.split()) < 2 or not mentions(page_text, name.strip()):
        return None, None
    return name.strip()[:80], (role.strip()[:80] if isinstance(role, str) and role.strip() else None)


def validate_signals(raw: list[dict], pages: list[Page], vc: VC, today: date,
                     window_days: int, sources: SourceRules) -> tuple[list[Signal], list[dict]]:
    """Returns (kept, dropped). Every drop carries a reason so it can be logged."""
    by_url = {norm_url(p.url): p for p in pages}
    oldest = today - timedelta(days=window_days)
    aliases = firm_aliases(vc.firm)
    kept: list[Signal] = []
    dropped: list[dict] = []
    seen: set[str] = set()

    def drop(reason: str, s: dict):
        dropped.append({"reason": reason, "url": str(s.get("url", ""))[:300],
                        "type": str(s.get("type", ""))[:40]})

    for s in raw:
        stype, summary = s.get("type"), s.get("summary")
        quote, url = s.get("quote"), s.get("url")
        if not all(isinstance(x, str) and x.strip() for x in (stype, summary, quote, url)):
            drop("malformed", s); continue
        if stype not in SIGNAL_TYPES:
            drop("unknown_type", s); continue
        page = by_url.get(norm_url(url))
        if page is None:
            drop("url_not_in_results", s); continue        # invented URL
        tier, domain, why = classify(page.url, vc, sources, vc.website)
        if tier is None:
            drop(why, s); continue                         # not a trusted source
        if page.published is None:
            drop("no_api_date", s); continue
        if page.published < oldest or page.published > today:
            drop("outside_window", s); continue
        q = norm_text(quote)
        if len(q) < MIN_QUOTE_CHARS or q not in norm_text(page.text):
            drop("quote_not_on_page", s); continue
        if not any(mentions(page.text, a) for a in aliases):
            drop("wrong_firm", s); continue
        if vc.name and not mentions(page.text, vc.name):
            drop("wrong_person", s); continue
        key = norm_url(page.url)
        if key in seen:
            drop("duplicate_url", s); continue
        seen.add(key)
        pname, prole = _person(s.get("person"), page.text)
        kept.append(Signal(type=stype, summary=summary.strip()[:300], quote=quote.strip(),
                           url=page.url, published=page.published, source_domain=domain,
                           source_tier=tier, person_name=pname, person_role=prole))
    return kept, dropped


SENIOR_ROLE = re.compile(r"founder|managing|general partner|\bpartner\b|principal|chair|\bceo\b|president", re.I)
ROLE_RANK = (("founder", 0), ("managing", 1), ("general partner", 2), ("chair", 3), ("ceo", 3), ("partner", 4), ("principal", 5))


def _rank(role: str) -> int:
    r = role.lower()
    return min((rank for key, rank in ROLE_RANK if key in r), default=9)


def validate_people(raw: list[dict], pages: list[Page], vc: VC, sources: SourceRules,
                    limit: int = 5) -> list[Person]:
    """Keep a person only if a trusted page names them, with a verbatim quote that contains their
    name, on a page that also names the firm, and a senior role. Never a guess."""
    by_url = {norm_url(p.url): p for p in pages}
    aliases = firm_aliases(vc.firm)
    out: dict[str, Person] = {}
    for r in raw:
        name, role, quote, url = (r.get("name"), r.get("role"), r.get("quote"), r.get("url"))
        if not all(isinstance(x, str) and x.strip() for x in (name, role, quote, url)):
            continue
        name, role = name.strip(), role.strip()[:80]
        page = by_url.get(norm_url(url))
        if page is None or len(name.split()) < 2 or not SENIOR_ROLE.search(role):
            continue
        tier, domain, _ = classify(page.url, vc, sources, vc.website)
        if tier is None:
            continue
        q = norm_text(quote)
        if len(q) < MIN_QUOTE_CHARS or q not in norm_text(page.text) or not mentions(quote, name):
            continue
        if not any(mentions(page.text, a) for a in aliases) or not mentions(page.text, name):
            continue
        out.setdefault(name.lower(), Person(name=name[:80], role=role, source_url=page.url,
                                            source_domain=domain, source_tier=tier, quote=quote.strip()[:300]))
    return sorted(out.values(), key=lambda p: _rank(p.role))[:limit]
