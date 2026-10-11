"""Orchestrator: load VCs -> (fit gate) -> search -> extract -> validate -> score -> write.

  python -m pipeline.run                      # real run: Neon + Exa + Gemini (needs env vars)
  python -m pipeline.run --source csv --no-write --limit 5   # live APIs, no database
  python -m pipeline.run --source csv --fixtures fixtures/demo.json   # fully offline demo
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from .config import RunFiles, load_run_files
from .extract import ExtractError, GeminiExtract
from .models import VC, Page, Person, Signal, VCResult
from .score import ACTION_ORDER, decide, fit_gate
from .search import ExaSearch, SearchError
from .store import CsvStore, PgStore
from .validate import norm_url, validate_people, validate_signals

ROOT = Path(__file__).resolve().parent.parent


PEOPLE_REFRESH_DAYS = 45     # re-discover a firm's partners this often
PEOPLE_CHECKED = 3           # check this many people per firm each run (Vikram's own contacts first)


def _needs_discovery(known: list[Person], today: date) -> bool:
    found = [p.discovered_at for p in known if not p.manual and p.discovered_at]
    return not found or (today - max(found).date()).days > PEOPLE_REFRESH_DAYS


def _discover_people(vc: VC, search, extractor, rf: RunFiles) -> list[Person]:
    pages = search.search_people(vc, rf.sources.exclude_domains())
    return validate_people(extractor.extract_people(vc, pages), pages, vc, rf.sources)


def _merge(signals: list[Signal], extra: list[Signal]) -> list[Signal]:
    """One signal per URL. If a firm-level signal is also found through a person, keep it and add the person."""
    by_url = {norm_url(s.url): s for s in signals}
    for s in extra:
        key = norm_url(s.url)
        if key not in by_url or (by_url[key].person_name is None and s.person_name):
            by_url[key] = s
    return list(by_url.values())


def process_vc(vc: VC, search, extractor, rf: RunFiles, today: date,
               known_people: list[Person] | None = None, check_people: bool = True) -> VCResult:
    w = rf.weights
    fit_ok, _, _ = fit_gate(vc, rf.fit)
    if not fit_ok or vc.status == "declined":
        # Routing rule 1: no point spending search / LLM budget.
        return VCResult(vc, True, "skipped_fit", decide(vc, [], rf.fit, w, today))
    exclude = rf.sources.exclude_domains()
    try:
        pages = search.search(vc, rf.fit.topic_keywords, today, w.window_days, exclude)
        raw = extractor.extract(rf.context, vc, pages)
    except (SearchError, ExtractError) as e:
        # A failed check is NOT "no signal": leave old values alone and flag it.
        return VCResult(vc, False, "failed", error=str(e)[:300])
    signals, dropped = validate_signals(raw, pages, vc, today, w.window_days, rf.sources)
    n_pages, found = len(pages), []

    if check_people:
        known = list(known_people or [])
        if _needs_discovery(known, today):
            try:
                found = _discover_people(vc, search, extractor, rf)
            except (SearchError, ExtractError) as e:
                dropped.append({"reason": "people_discovery_failed", "url": "", "type": str(e)[:80]})
        everyone = sorted([p for p in known if p.manual], key=lambda p: p.name) + \
            [p for p in (found + [k for k in known if not k.manual]) if p.name.lower() not in
             {m.name.lower() for m in known if m.manual}]
        seen: set[str] = set()
        for person in everyone:
            if person.name.lower() in seen or len(seen) >= PEOPLE_CHECKED:
                continue
            seen.add(person.name.lower())
            pvc = replace(vc, name=person.name)
            try:
                ppages = search.search(pvc, rf.fit.topic_keywords, today, w.window_days, exclude)
                psigs, pdrop = validate_signals(extractor.extract(rf.context, pvc, ppages), ppages, pvc,
                                                today, w.window_days, rf.sources)
            except (SearchError, ExtractError) as e:
                dropped.append({"reason": "person_check_failed", "url": "", "type": person.name[:60]})
                continue
            n_pages += len(ppages)
            dropped += [{**d, "type": f"{person.name}: {d['type']}"[:60]} for d in pdrop]
            signals = _merge(signals, [replace(s, person_name=person.name, person_role=person.role or None)
                                       for s in psigs])

    decision = decide(vc, signals, rf.fit, w, today)
    return VCResult(vc, True, "signal" if signals else "no_signal", decision, signals, dropped, n_pages,
                    people_found=found)


# ---------------------------------------------------------------- offline demo

class FixtureClients:
    """Canned search pages + canned model output, so the whole flow runs with no keys."""

    def __init__(self, path: Path, today: date):
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.today = today

    def apply_overrides(self, vc: VC) -> VC:
        ov = dict(self.data.get(vc.firm, {}).get("vc") or {})
        if not ov:
            return vc
        ago = ov.pop("last_contacted_days_ago", None)
        if ago is not None:
            ov["last_contacted_at"] = self.today - timedelta(days=ago)
        return replace(vc, **ov)

    def _entry(self, vc):
        """Firm-level fixture, or a person's own fixture when the VC carries a person's name."""
        firm = self.data.get(vc.firm, {})
        return firm.get("person_activity", {}).get(vc.name, {}) if vc.name else firm

    def _pages(self, raw_pages, today):
        return [Page(p["url"], p.get("title", ""),
                     today - timedelta(days=p["days_ago"]) if p.get("days_ago") is not None else None,
                     p["text"]) for p in raw_pages]

    def search(self, vc, keywords, today, window_days, exclude_domains=None):
        return self._pages(self._entry(vc).get("pages", []), today)

    def extract(self, context, vc, pages):
        return self._entry(vc).get("llm", {}).get("signals", [])

    def search_people(self, vc, exclude_domains=None):
        return self._pages(self.data.get(vc.firm, {}).get("people", {}).get("pages", []), self.today)

    def extract_people(self, vc, pages):
        return self.data.get(vc.firm, {}).get("people", {}).get("llm", {}).get("people", [])


# ---------------------------------------------------------------- run

def print_table(results: list[VCResult]) -> None:
    ok = [r for r in results if r.ok and r.decision]
    ok.sort(key=lambda r: (ACTION_ORDER[r.decision.action], -r.decision.score, r.vc.firm))
    print(f"\n{'ACTION':<11}{'SCORE':>5}  {'FIRM':<34} WHY")
    for r in ok:
        if r.decision.action in ("quiet_list", "skip") and len(ok) > 25 and r.decision.action != "skip":
            continue
        d = r.decision
        print(f"{d.action:<11}{d.score:>5}  {r.vc.firm[:33]:<34} {d.summary[:90]}")
    quiet = sum(1 for r in ok if r.decision.action == "quiet_list")
    print(f"\n({quiet} VCs on the quiet list, hidden above)" if quiet and len(ok) > 25 else "")
    for r in results:
        for d in r.dropped:
            print(f"  dropped [{d['reason']}] {r.vc.firm}: {d['url']}")
        if not r.ok:
            print(f"  FAILED {r.vc.firm}: {r.error}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["db", "csv"], default="db")
    ap.add_argument("--csv", type=Path, default=ROOT / "data" / "vc_seed_list.csv")
    ap.add_argument("--no-write", action="store_true", help="print results, write nothing")
    ap.add_argument("--fixtures", type=Path, help="offline demo: canned pages and model output")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-people", action="store_true", help="skip partner discovery and per-person checks")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = ap.parse_args(argv)

    rf = load_run_files()
    store = CsvStore(args.csv) if args.source == "csv" else PgStore(os.environ["SIGNAL_JOB_DATABASE_URL"])
    write = args.source == "db" and not args.no_write

    vcs = store.load_vcs()
    if args.limit:
        vcs = vcs[: args.limit]

    if args.fixtures:
        fx = FixtureClients(args.fixtures, args.today)
        vcs = [fx.apply_overrides(v) for v in vcs]
        search = extractor = fx
    else:
        search, extractor = ExaSearch(), GeminiExtract()

    people = store.load_people()
    run_id = store.start_run(len(vcs)) if write else 0
    results: list[VCResult] = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for r in pool.map(lambda v: process_vc(v, search, extractor, rf, args.today, people.get(v.id, []), not args.no_people), vcs):
                results.append(r)
                if write:
                    store.write_result(run_id, r)
    except Exception as e:  # unexpected: record it, then fail the run loudly
        if write:
            store.finish_run(run_id, "failed", len(results), len(vcs) - len(results),
                             0, 0, f"{type(e).__name__}: {e}"[:300])
        raise

    failed = sum(1 for r in results if not r.ok)
    kept = sum(len(r.signals) for r in results)
    dropped = sum(len(r.dropped) for r in results)
    status = "ok" if failed == 0 else "failed" if failed * 2 > len(results) else "partial"
    if write:
        store.finish_run(run_id, status, len(results) - failed, failed, kept, dropped)
    print_table(results)
    print(f"\nrun: {status} | {len(results)} VCs | {failed} failed | {kept} signals kept | {dropped} dropped")
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
