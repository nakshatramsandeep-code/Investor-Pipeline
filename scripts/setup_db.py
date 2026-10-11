"""One-time setup, run as the OWNER (Vikram's) role:
   1. applies db/schema.sql (tables + column-level GRANTs)
   2. gives the three least-privilege roles a login password

   OWNER_DATABASE_URL=...  SIGNAL_JOB_PASSWORD=...  DASHBOARD_RO_PASSWORD=...  DASHBOARD_FB_PASSWORD=... \
       python scripts/setup_db.py

Then build each role's connection string from your Neon host (same host/db, user = role name).
"""
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROLES = {"signal_job": "SIGNAL_JOB_PASSWORD", "dashboard_ro": "DASHBOARD_RO_PASSWORD",
         "dashboard_fb": "DASHBOARD_FB_PASSWORD"}


def main() -> int:
    missing = [v for v in ["OWNER_DATABASE_URL", *ROLES.values()] if not os.environ.get(v)]
    if missing:
        print("missing env vars:", ", ".join(missing))
        return 1
    schema = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(os.environ["OWNER_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(schema)
        for role, var in ROLES.items():
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(os.environ[var])))
            print(f"role ready: {role}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
