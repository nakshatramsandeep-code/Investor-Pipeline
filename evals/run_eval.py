"""Eval: run the real search + extraction + validation on the 10 hand-labelled VCs and
report precision, recall and invented URLs.

   EXA_API_KEY=... GEMINI_API_KEY=... python evals/run_eval.py

Label run_files/eval_set.csv first: search each firm by hand for the last 30 days and put
yes/no in expect_signal. Unlabelled rows are skipped. Re-run after any change to
context.md, the model, or the validator. Target: 0 invented URLs, precision >= 0.8.
"""
import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.config import load_run_files  # noqa: E402
from pipeline.extract import GeminiExtract  # noqa: E402
from pipeline.run import process_vc, today_local  # noqa: E402
from pipeline.search import ExaSearch  # noqa: E402
from pipeline.store import CsvStore  # noqa: E402


def main() -> int:
    rf = load_run_files()
    by_firm = {v.firm.lower(): v for v in CsvStore(ROOT / "data" / "vc_seed_list.csv").load_vcs()}
    rows = list(csv.DictReader((ROOT / "run_files" / "eval_set.csv").open(encoding="utf-8")))
    search, extractor = ExaSearch(), GeminiExtract()
    tp = fp = fn = tn = invented = 0
    for row in rows:
        label = (row["expect_signal"] or "").strip().lower()
        vc = by_firm.get(row["firm"].strip().lower())
        if vc is None:
            print(f"  ?? {row['firm']}: not in seed list")
            continue
        res = process_vc(vc, search, extractor, rf, today_local())
        invented += sum(1 for d in res.dropped if d["reason"] == "url_not_in_results")
        got = bool(res.signals)
        print(f"  {vc.firm:<30} expected={label or '-':<4} got={'yes' if got else 'no':<4}"
              f" kept={len(res.signals)} dropped={len(res.dropped)}" + (f" FAILED: {res.error}" if not res.ok else ""))
        if label not in ("yes", "no") or not res.ok:
            continue
        exp = label == "yes"
        tp += exp and got; fp += (not exp) and got; fn += exp and not got; tn += (not exp) and not got
    labelled = tp + fp + fn + tn
    if not labelled:
        print("\nNo labelled rows yet - fill expect_signal in run_files/eval_set.csv.")
        return 1
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    print(f"\nlabelled={labelled} precision={prec:.2f} recall={rec:.2f} invented_urls_caught={invented}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
