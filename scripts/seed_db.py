"""Load the VC list into Neon (owner role). Safe to re-run: existing VCs only get their
profile refreshed; Vikram's notes, contact history, status and signal columns are never touched.

   OWNER_DATABASE_URL=... python scripts/seed_db.py [path/to/list.csv]
"""
import csv
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    dsn = os.environ.get("OWNER_DATABASE_URL")
    if not dsn:
        print("OWNER_DATABASE_URL is not set")
        return 1
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "vc_seed_list.csv"
    n = 0
    with psycopg.connect(dsn) as conn, path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("id"):
                continue
            flag = row.get("flag", "")
            conn.execute(
                "INSERT INTO vcs (id, name, firm, thesis, status) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO UPDATE SET firm = EXCLUDED.firm, thesis = EXCLUDED.thesis",
                (int(row["id"]), row.get("name", "").strip(), row["firm"].strip(),
                 row.get("thesis", ""), row.get("status") or "to_contact"))
            conn.execute(
                "INSERT INTO vc_profile (vc_id, geo, stage_focus, is_investing, flag, background, source_url)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (vc_id) DO UPDATE SET geo = EXCLUDED.geo,"
                " stage_focus = EXCLUDED.stage_focus, is_investing = EXCLUDED.is_investing,"
                " flag = EXCLUDED.flag, background = EXCLUDED.background, source_url = EXCLUDED.source_url",
                (int(row["id"]), row.get("region") or "Unknown", row.get("stage_focus", ""),
                 "not currently investing" not in flag.lower(), flag,
                 row.get("evidence", ""), row.get("source_url", "")))
            n += 1
    print(f"seeded {n} VCs from {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
