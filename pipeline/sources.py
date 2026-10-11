"""Source trust: decides whether a page is an acceptable origin for a signal."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .models import VC
from .names import firm_aliases

GENERIC_TAILS = ("ventures", "venture", "capital", "partners", "fund", "vc", "group", "vp", "investments")


@dataclass(frozen=True)
class SourceRules:
    allow_social: bool
    allow_unlisted: bool
    news: tuple[str, ...]
    social: tuple[str, ...]
    blocked: tuple[str, ...]

    def include_domains(self) -> list[str]:
        """Trusted outlets, for a search that can only return usable pages."""
        return list(self.news)

    def exclude_domains(self) -> list[str]:
        """Domains to keep out of the search itself (saves result slots for usable pages)."""
        return list(self.blocked) + ([] if self.allow_social else list(self.social))


def host_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


PLATFORM_HOSTS = re.compile(r"substack|medium\.com|wordpress|beehiiv|ghost\.io|notion|github|blogspot|payloadcms|linkedin|youtube|twitter|x\.com")
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def registrable(domain: str) -> str:
    """newsletter.equal.vc -> equal.vc ; admin.fundalogical.com -> fundalogical.com ; foo.co.in stays foo.co.in."""
    labels = domain.lower().split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def guess_website(items: list[tuple[str, str]]) -> str | None:
    """items = (domain, tier) pairs from verified own-site pages. Returns the most common real domain
    (never a blogging platform), or None."""
    counts: dict[str, int] = {}
    for domain, tier in items:
        if tier == "own_site" and domain and not PLATFORM_HOSTS.search(domain):
            d = registrable(domain)
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    best = sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
    return "https://" + best


def firm_slugs(firm: str) -> set[str]:
    """Names a firm's own site is likely to use: 'Dynamo Ventures' -> dynamo, dynamoventures, dynamovc..."""
    slugs: set[str] = set()
    for alias in firm_aliases(firm):
        tokens = re.findall(r"[a-z0-9]+", alias.lower())
        if not tokens:
            continue
        full = "".join(tokens)
        if len(full) >= 3:
            slugs.add(full)
        base = list(tokens)
        while len(base) > 1 and base[-1] in GENERIC_TAILS:
            base.pop()
        base_s = "".join(base)
        if len(base_s) >= 4:
            slugs.add(base_s)
            slugs.update(base_s + g for g in GENERIC_TAILS)
    return slugs


def is_own_site(host: str, vc: VC, website: str = "") -> bool:
    if website and _under(host, host_of(website if "//" in website else "//" + website)):
        return True
    slugs = firm_slugs(vc.firm)
    return any(_alnum(label) in slugs for label in host.split("."))


def classify(url: str, vc: VC, rules: SourceRules, website: str = "") -> tuple[str | None, str, str]:
    """(tier, domain, drop_reason). tier None means reject, with the reason."""
    host = host_of(url)
    if not host:
        return None, "", "untrusted_source"
    if any(_under(host, d) for d in rules.blocked):
        return None, host, "blocked_source"
    if is_own_site(host, vc, website):
        return "own_site", host, ""
    if any(_under(host, d) for d in rules.news):
        return "news", host, ""
    if any(_under(host, d) for d in rules.social):
        return ("social", host, "") if rules.allow_social else (None, host, "social_source_disabled")
    if rules.allow_unlisted:
        return "unlisted", host, ""
    return None, host, "untrusted_source"
