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
tests/  evals/                  119 unit tests; precision / recall / invented-URL eval
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
| 2 | In talks or approved; or contacted after the signal, less than 7 days ago | hold |
| 3 | Prospect with a logistics signal from the last 14 days and a score of at least 35 | reach out |
| 4 | Pitched, and a fresh signal arrived after you reached out | follow up |
| 5 | Pitched 7+ days ago with no reply (signal or not: silence is a reason) | follow up |
| 6 | Pitched less than 7 days ago | hold (shown as Waiting, with the follow-up date) |
| 7 | No signal in 30 days, or a weak one below the score floor | dormant |

The dashboard re-buckets from your status and dates in the browser, so logging an action moves a card at once;
scores and signals still come from the last run. Settings live in `run_files/weights.yaml` (`waiting_nudge_days`,
`reach_out_min_score`, `reach_out_max_signal_age_days`, `general_content_factor`). Deals outrank talk: interviews, posts and talks weigh less than investments, and a fund whose only signals are content is capped at `content_only_cap` of the maximum. General interviews, posts and talks that are not about logistics
count for a third as much as a deal, so off-topic appearances don't lift a score.

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

There is no login. Saving needs `DASHBOARD_ED_URL` in Vercel (user `dashboard_ed`, which can only change your
four columns and add to the activity log and contacts). Because anyone who opens the URL could then read your
notes and change your statuses, turn on **Vercel Deployment Protection** (Settings -> Deployment Protection) so
only you can open the site. Statuses: Prospect, Contacted, In talks, Approved, Closed, Declined. Approved and In talks always
route to hold. A declined investor can be given a revisit date (6 months) or have the decline undone; when the
revisit date arrives they are treated as a prospect again. In talks and Approved cards show a short "next step" you set.

## Assumptions to confirm

- `status` is `to_contact`, `waiting` or `declined` (the CSV only has `to_contact`).
  `last_contacted_at` is empty until Vikram fills it in; a waiting VC with no date is treated as overdue.
- The `name` column in the CSV is empty, so matching is by firm. Fill it in to add a person check.
- Fit gate: Seed/pre-seed/early/corporate stage focus and still investing. This skips Maersk Growth
  (paused) and Norwest (Series A+). India and Global get a small score bonus; edit `weights.yaml` if that is wrong.
- Gemini model defaults to `gemini-3.8-flash` (override with `GEMINI_MODEL`).
- The signal job writes score, action and summary for skipped VCs too, so the dashboard can say why.

## One signal per event

The same event reported by two articles is one signal: two signals are merged when they are within 14 days,
share a name or amount, and say mostly the same thing; the firm's own site wins over press. Evidence the latest
run did not keep (duplicates, pages that vanished) is marked inactive, never deleted.

## Undo, delete and dates

Every logged action (including "They approved" and "They declined") shows a confirmation toast with an **Undo**
button for about nine seconds; Undo deletes that entry and restores the previous status and last-contacted date.
Each entry in an investor's history has a delete button (last contacted is recalculated from what remains), and
"Last contacted" has a Clear button. Approved cards offer Mark closed, Next step and Undo approval.

Dates are your local calendar days. The browser sends its own "today" with every save, so a save at 1 a.m. IST is
dated today (the server runs on UTC and would say yesterday). The scheduled run also uses IST for "days since
contact" (`PIPELINE_TZ` to change it).

## Search providers and cost

Search is a chain: **Tavily first, Exa as the fallback** (`SEARCH_PROVIDERS`, default `tavily,exa`; a provider
without its key is skipped, and a provider that runs out of credits is retired for the rest of the run). Both are
kept in `pipeline/search.py`. Secrets: `TAVILY_API_KEY`, `EXA_API_KEY`.

To keep a run affordable the pipeline spends a search only when an investor is **due** (`schedule` in
`weights.yaml`): contacted / in talks / approved and reach-out candidates every 2 days, dormant investors every 7,
declined and not-a-fit investors never. An investor is searched **once** (one basic Tavily search = 1 credit);
known partners cost no extra search, because a signal is attributed to a partner when the page names them.
Only contacts you add yourself get a dedicated search. Partners are re-discovered every 45 days. Expect roughly
300 searches a month. `python -m pipeline.run --all` ignores the schedule.

Tavily returns a publish date only for its news topic, and dates must come from the API, so the activity search
uses that topic. In testing it surfaced far fewer pages that name a specific fund than Exa did, so expect fewer
signals; check "Skipped untrusted sources" in an investor's check history and add good outlets to
`run_files/sources.yaml`.

## Fit is part of the score

Beyond the flag penalties, `run_files/fit_rules.yaml` scores how well a fund matches a Seed logistics pitch:
a stage focus that stretches to growth/late (x0.85), a thesis that never mentions your topic (x0.8, "generalist"),
and a "check seed cheque fit" flag (x0.7). The multiplier and each reason are stored with every check and shown on
the investor's profile ("How fit changes the score"), so a big or off-topic fund can't rank "strong" on freshness
alone. **Fill in `competitors:`** with the names of companies that compete with yours: a fund whose recent signals
mention one is flagged "possible conflict" and scored down (x0.5). It is empty because the pipeline can't know them.

## What the dashboard shows

The top panel shows when the last search run happened and how many investors it covered. Signal counts on cards are
logistics signals only ("1 logistics signal, +4 other"); a signal that is about to leave the 14-day reach-out
window shows "Cools in N days". A partner's activity is shown only if it is about logistics.

**Reach out** needs a logistics signal from the last 14 days. **Warm** holds funds whose logistics signal is 15-30
days old (not urgent, not nothing), best score first. **Dormant** means no logistics signal in the last 30 days.
The My pipeline table has a **Next search** column: the next Monday or Thursday run on which that investor will be
searched (contacted / in-talks / reach-out investors every 2 days, everyone else weekly; declined and not-a-fit
investors need no search). Setting an investor back to Prospect clears "Last contacted", so a mis-click leaves no
ghost date.

**By action** is the to-do list for today: Do this first, Reach out, Follow up due, Contacted, Declined, plus the
Dormant and Not-a-fit lists. **My pipeline** is every investor and where they stand: stage columns, and a table of
all of them to search, filter and log.

My pipeline has a searchable table of every investor (name, partner, thesis, region; filter by stage; sort by
score, last contact, or least recently searched), so any fund, including dormant ones, can be opened or logged.
"Mark reached out" asks who, how (email / LinkedIn / warm intro / ...) and when, and stores it with the log entry.
Run kinds: a free re-score is recorded as kind 'rescore' and never shows as the "last run".

## Signals persist; re-scoring is free

A signal stays valid for its 30-day window no matter what a later search returns. Each run merges the signals it
finds with the ones already on file, removes duplicates, and scores the combined set, so a weak or empty search can
never erase what an earlier one verified. (An earlier version treated each run as the whole truth and deactivated
115 saved signals after a Tavily sweep found little; this fixed it.)

`python -m pipeline.run --rescore` rebuilds every investor's score and recommendation from the saved signals with
**no search and no model call**, so it costs nothing. Use it after changing `weights.yaml` or the rules. It is
logged as "Re-scored from saved signals" and does not count as a check in the schedule.

## If a run stops with "out of credits"

When every provider is out of credits (or a key is rejected) the run stops at once (exit code 2) instead of
failing every investor one by one, writes the reason to the run log (the dashboard shows it), and keeps every
investor's previous results. Top up, then re-run the workflow from the Actions tab.
