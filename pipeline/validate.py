"""The Python validation gate. Nothing the model says reaches the database unless it
passes every check here. Dates and URLs are taken from the search API, not the model."""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urldefrag

from .config import SIGNAL_TYPES
from .models import VC, Page, Signal
from .names import firm_aliases, mentions

MIN_QUOTE_CHARS = 20


def norm_url(u: str) -> str:
    return urldefrag(u.strip())[0].rstrip("/").lower()


def norm_text(s: str) -> str:
    s = (s.replace("’", "'").replace("‘", "'")
          .replace("“", '"').replace("”", '"')
          .replace("–", "-").replace("—", "-").replace(" ", " "))
    return re.sub(r"\s+", " ", s).strip().lower()


def validate_signals(raw: list[dict], pages: list[Page], vc: VC, today: date,
                     window_days: int) -> tuple[list[Signal], list[dict]]:
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
        kept.append(Signal(type=stype, summary=summary.strip()[:300],
                           quote=quote.strip(), url=page.url, published=page.published))
    return kept, dropped
