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
from .models import VC, Page, VCResult
from .score import ACTION_ORDER, decide, fit_gate
from .search import ExaSearch, SearchError
from .store import CsvStore, PgStore
from .validate import validate_signals

ROOT = Path(__file__).resolve().parent.parent


def process_vc(vc: VC, search, extractor, rf: RunFiles, today: date) -> VCResult:
    w = rf.weights
    fit_ok, _, _ = fit_gate(vc, rf.fit)
    if not fit_ok or vc.status == "declined":
        # Routing rule 1: no point spending search / LLM budget.
        return VCResult(vc, True, "skipped_fit", decide(vc, [], rf.fit, w, today))
    try:
        pages = search.search(vc, rf.fit.topic_keywords, today, w.window_days, rf.sources.exclude_domains())
        raw = extractor.extract(rf.context, vc, pages)
    except (SearchError, ExtractError) as e:
        # A failed check is NOT "no signal": leave old values alone and flag it.
        return VCResult(vc, False, "failed", error=str(e)[:300])
    signals, dropped = validate_signals(raw, pages, vc, today, w.window_days, rf.sources)
    decision = decide(vc, signals, rf.fit, w, today)
    return VCResult(vc, True, "signal" if signals else "no_signal", decision, signals, dropped, len(pages))


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

    def search(self, vc, keywords, today, window_days, exclude_domains=None):
        return [Page(p["url"], p.get("title", ""),
                     today - timedelta(days=p["days_ago"]) if p.get("days_ago") is not None else None,
                     p["text"]) for p in self.data.get(vc.firm, {}).get("pages", [])]

    def extract(self, context, vc, pages):
        return self.data.get(vc.firm, {}).get("llm", {}).get("signals", [])


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

    run_id = store.start_run(len(vcs)) if write else 0
    results: list[VCResult] = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for r in pool.map(lambda v: process_vc(v, search, extractor, rf, args.today), vcs):
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
