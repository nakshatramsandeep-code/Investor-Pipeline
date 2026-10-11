# Investor Pipeline

Twice a week (Mon + Thu, 7am IST) this checks every VC on Vikram's list for a signal from the last
30 days, scores them, and updates his Neon table, so the Vercel dashboard opens already ranked:
who to reach out to this week, who to nudge, who to leave alone, and why, with a source link for
every claim.

It follows the component map: Scheduler → Neon (read) → Exa → Gemini Flash → Python validation →
score + route → Neon (write) → dashboard → Vikram (the only one who writes messages).

```
.github/workflows/signals.yml   Scheduler (cron + manual re-run)
db/schema.sql                   tables + 3 least-privilege roles (column-level GRANTs)
run_files/                      context.md (prompt), fit_rules.yaml, weights.yaml, eval_set.csv
pipeline/                       search.py  extract.py  validate.py  score.py  store.py  run.py
dashboard/                      Vercel: static page + /api/pipeline (read) + /api/feedback (insert)
scripts/                        setup_db.py, seed_db.py
tests/  evals/                  90 unit tests; precision / recall / invented-URL eval
data/vc_seed_list.csv           the 50 VCs
```

## Try it now (no keys, no database)

```bash
pip install -r requirements.txt
python -m pytest -q
python -m pipeline.run --source csv --fixtures fixtures/demo.json --today 2026-10-11
```

The fixture run shows each routing row, plus three bad model outputs being dropped by validation
(an invented URL, a quote that is not on the page, a different firm with a similar name).

## Go live

1. **Neon**: create a project. With the owner connection string:
   `OWNER_DATABASE_URL=... SIGNAL_JOB_PASSWORD=... DASHBOARD_RO_PASSWORD=... DASHBOARD_FB_PASSWORD=... python scripts/setup_db.py`
   then `OWNER_DATABASE_URL=... python scripts/seed_db.py`.
2. **GitHub repo secrets**: `SIGNAL_JOB_DATABASE_URL` (user `signal_job`), `EXA_API_KEY`, `GEMINI_API_KEY`.
3. **Vercel**: deploy `dashboard/` with env `DASHBOARD_RO_URL` (user `dashboard_ro`),
   `DASHBOARD_FB_URL` (user `dashboard_fb`). There is no login: anyone with the URL can see the table,
   so keep the URL private (or turn on Vercel Deployment Protection). Vikram's notes are not exposed by the API.
4. **Label the eval set** (`run_files/eval_set.csv`), then `python evals/run_eval.py`.
5. Trigger the workflow manually once, check the dashboard, then let the cron take over.

Vikram edits `notes`, `contact_history`, `last_contacted_at` and `status` with the owner role
(Neon's SQL editor or any Postgres client). The job cannot see or change them.

## How it decides

Routing is a plain table, first match wins (`pipeline/score.py`, one unit test per row):

| # | Situation | Action |
|---|---|---|
| 1 | Fit fails or declined | skip (no API spend) |
| 2 | Contacted after the signal | hold |
| 3 | Fresh signal, new VC | reach out |
| 4 | Fresh signal, waiting | nudge |
| 5 | Waiting > 10 days, quiet | nudge |
| 6 | Contacted < 10 days ago, quiet | hold |
| 7 | No signal in 30 days | quiet list |

A signal survives only if: its page comes from a trusted source (below), its URL is in the returned search results, its date comes from the
Exa API and is within 30 days, its quote appears verbatim on the page, and the page names the
right firm (including old names, e.g. Sequoia Capital India for Peak XV). Page text is treated as
untrusted input. A failed search is logged and flagged, and never written as "no signal".

## Trusted sources

`run_files/sources.yaml` decides where a signal may come from. Accepted: the firm's own site, blog or
newsletter (matched by firm name, or `vc_profile.website`) and a list of reputable news, trade press and
press-release wires. Rejected: known listicles / aggregators (and excluded from the search itself), LinkedIn / X
posts (`allow_social: false` by default), and any domain not on the list (`allow_unlisted: false`).
Every kept signal records its domain and tier, shown as a badge on the dashboard, and scored
slightly lower for news than for the firm's own site. Rejections are logged per check with their reason
(`blocked_source`, `social_source_disabled`, `untrusted_source`), visible in each investor's check history.
To accept a good outlet that is missing, add its domain to `news:`.

## Investor profiles and who to contact

Click an investor's name on the dashboard. The profile shows the signals with their sources, fit and
background, relationship status and check history. **Who to reach out to** lists people named on a verified
source page (the name must appear on the page; it is never guessed) plus any contact you add:

```sql
INSERT INTO vc_contacts (vc_id, name, role, linkedin_url) VALUES (28, 'Jane Doe', 'Partner', 'https://www.linkedin.com/in/...');
```

Emails you store in `vc_contacts.email` and your notes are not returned by the public API.

## People and their activity

Each run, for firms that pass the fit gate, the job (1) finds the firm's founders and partners, (2) searches
each person's own recent activity (talks, events, posts, interviews), and (3) merges what it finds into the
firm's signals, tagged with the person. A person is stored only if a trusted page names them, with a verbatim
quote containing their name, on a page that also names the firm, and a senior role (founder, managing
partner, general partner, partner, principal). Partners are re-discovered every 45 days; up to 3 people per
firm are checked each run, your own contacts (`vc_contacts`) first. `python -m pipeline.run --no-people` skips this.

## Logging your outreach

On an investor's page, **Log activity** records that you reached out, followed up, they replied, you had a
call or met, or they declined. It updates `status` and `last_contacted_at`, which the next run uses to decide
whether to nudge or hold. A new status, `in_conversation`, always routes to hold. The "My pipeline" tab groups
every investor by status, and cards have quick buttons.

Reading the dashboard needs no login. **Saving does**: set `EDIT_KEY` (any long random string) and
`DASHBOARD_ED_URL` (user `dashboard_ed`, which can only change your four columns and add to the activity log
and contacts) in Vercel. The page asks for the key once per browser. Without them editing stays off.
Your notes and activity log are only returned to a request carrying the key.

## Assumptions to confirm

- `status` is `to_contact`, `waiting` or `declined` (the CSV only has `to_contact`).
  `last_contacted_at` is empty until Vikram fills it in; a waiting VC with no date is treated as overdue.
- The `name` column in the CSV is empty, so matching is by firm. Fill it in to add a person check.
- Fit gate: Seed/pre-seed/early/corporate stage focus and still investing. This skips Maersk Growth
  (paused) and Norwest (Series A+). India and Global get a small score bonus; edit `weights.yaml` if that is wrong.
- Gemini model defaults to `gemini-3.8-flash` (override with `GEMINI_MODEL`).
- The signal job writes score, action and summary for skipped VCs too, so the dashboard can say why.
