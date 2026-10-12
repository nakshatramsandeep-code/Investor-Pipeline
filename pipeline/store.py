"""Neon write boundary. The job connects as signal_job, which Postgres limits to:
SELECT on 7 vcs columns (not notes), UPDATE on exactly 4, INSERT into its own log tables."""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from .models import VC, Person, VCResult

# Explicit column list on purpose: never SELECT * (notes / contact_history are off limits).
LOAD_SQL = """
SELECT v.id, v.name, v.firm, v.thesis, v.status, v.last_contacted_at, v.last_signal_date,
       p.geo, p.stage_focus, p.is_investing, p.flag, p.website, v.revisit_on,
       (SELECT max(c.checked_at) FROM vc_check_log c
         WHERE c.vc_id = v.id AND c.ok AND c.outcome IN ('signal', 'no_signal', 'skipped_fit')) AS last_checked_at,
       v.recommended_action
FROM vcs v JOIN vc_profile p ON p.vc_id = v.id
ORDER BY v.id
"""

# The only UPDATE the job ever issues.
UPDATE_SQL = """
UPDATE vcs SET last_signal_date = COALESCE(%s, last_signal_date),
               signal_summary = %s, follow_up_score = %s, recommended_action = %s
WHERE id = %s
"""

EVIDENCE_SQL = """
INSERT INTO signal_evidence (vc_id, run_id, type, summary, quote, url, published_date,
                             source_domain, source_tier, person_name, person_role, relevance)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (vc_id, url) DO UPDATE SET run_id = EXCLUDED.run_id, source_domain = EXCLUDED.source_domain,
    source_tier = EXCLUDED.source_tier, person_name = EXCLUDED.person_name, person_role = EXCLUDED.person_role,
    relevance = EXCLUDED.relevance
"""

ACTIVE_SQL = "UPDATE signal_evidence SET active = (url = ANY(%s)) WHERE vc_id = %s AND source_tier <> 'legacy_unverified'"

WEBSITE_SQL = "UPDATE vc_profile SET website = %s WHERE vc_id = %s AND website = ''"

CHECK_SQL = """
INSERT INTO vc_check_log (vc_id, run_id, ok, outcome, n_pages, n_signals, n_dropped, error, dropped, detail)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


PEOPLE_SQL = """
SELECT vc_id, name, role, source_url, source_domain, source_tier, quote, discovered_at, false
FROM vc_people
UNION ALL
SELECT vc_id, name, role, '', '', 'manual', '', NULL, true FROM vc_contacts
"""

PERSON_UPSERT_SQL = """
INSERT INTO vc_people (vc_id, name, role, source_url, source_domain, source_tier, quote)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (vc_id, name) DO UPDATE SET role = EXCLUDED.role, source_url = EXCLUDED.source_url,
    source_domain = EXCLUDED.source_domain, source_tier = EXCLUDED.source_tier,
    quote = EXCLUDED.quote, discovered_at = now()
"""


class PgStore:
    def __init__(self, dsn: str):
        import psycopg  # imported lazily so offline demo needs no database driver
        self.conn = psycopg.connect(dsn)

    def load_vcs(self) -> list[VC]:
        with self.conn.cursor() as cur:
            cur.execute(LOAD_SQL)
            rows = cur.fetchall()
        self.conn.rollback()
        return [VC(*r) for r in rows]

    def load_people(self) -> dict[int, list[Person]]:
        with self.conn.cursor() as cur:
            cur.execute(PEOPLE_SQL)
            rows = cur.fetchall()
        self.conn.rollback()
        out: dict[int, list[Person]] = {}
        for vc_id, name, role, url, dom, tier, quote, at, manual in rows:
            out.setdefault(vc_id, []).append(Person(name, role, url, dom, tier, quote, at, manual))
        return out

    def start_run(self, total: int) -> int:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO run_log (vcs_total) VALUES (%s) RETURNING id", (total,))
            run_id = cur.fetchone()[0]
        self.conn.commit()
        return run_id

    def write_result(self, run_id: int, r: VCResult) -> None:
        """One transaction per VC, so a crash mid-run leaves every VC either fully
        updated or untouched. A failed check writes the log row only - the old
        score and action stay as they were."""
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                if r.ok and r.decision:
                    d = r.decision
                    cur.execute(UPDATE_SQL, (d.last_signal_date, d.summary, d.score, d.action, r.vc.id))
                    for s in r.signals:
                        cur.execute(EVIDENCE_SQL, (r.vc.id, run_id, s.type, s.summary, s.quote, s.url, s.published,
                                              s.source_domain, s.source_tier, s.person_name, s.person_role, s.relevance))
                for p in r.people_found:
                    cur.execute(PERSON_UPSERT_SQL, (r.vc.id, p.name, p.role, p.source_url,
                                                    p.source_domain, p.source_tier, p.quote))
                if r.ok:
                    # The latest run is the truth: evidence it did not keep (duplicates, vanished pages) goes inactive.
                    cur.execute(ACTIVE_SQL, ([sig.url for sig in r.signals], r.vc.id))
                if r.ok and r.website:
                    cur.execute(WEBSITE_SQL, (r.website, r.vc.id))     # only fills a blank; never overwrites
                cur.execute(CHECK_SQL, (r.vc.id, run_id, r.ok, r.outcome, r.n_pages, len(r.signals),
                                        len(r.dropped), r.error, json.dumps(r.dropped), json.dumps(r.detail)))

    def finish_run(self, run_id: int, status: str, checked: int, failed: int,
                   kept: int, dropped: int, error: str | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE run_log SET finished_at = now(), status = %s, vcs_checked = %s, vcs_failed = %s,"
                " signals_kept = %s, signals_dropped = %s, error = %s WHERE id = %s",
                (status, checked, failed, kept, dropped, error, run_id))
        self.conn.commit()


class CsvStore:
    """Read-only source for trying the pipeline before a database exists."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load_vcs(self) -> list[VC]:
        out = []
        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if not row.get("id"):
                    continue
                flag = row.get("flag", "")
                out.append(VC(id=int(row["id"]), name=row.get("name", "").strip(), firm=row["firm"].strip(),
                              thesis=row.get("thesis", ""), status=row.get("status") or "to_contact",
                              last_contacted_at=None, last_signal_date=None,
                              geo=row.get("region") or "Unknown", stage_focus=row.get("stage_focus", ""),
                              is_investing="not currently investing" not in flag.lower(), flag=flag))
        return out

    def load_people(self) -> dict[int, list[Person]]:
        return {}

    def start_run(self, total: int) -> int:
        return 0

    def write_result(self, run_id: int, r: VCResult) -> None:
        pass

    def finish_run(self, *a, **k) -> None:
        pass
