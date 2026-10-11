-- Investor pipeline schema (Neon / Postgres). Run as the OWNER role (Vikram's).
-- Idempotent: safe to re-run.

-- ---------------------------------------------------------------- tables

CREATE TABLE IF NOT EXISTS vcs (
    id                  integer PRIMARY KEY,
    name                text NOT NULL DEFAULT '',
    firm                text NOT NULL,
    thesis              text NOT NULL DEFAULT '',
    status              text NOT NULL DEFAULT 'to_contact'
                        CHECK (status IN ('to_contact', 'waiting', 'in_conversation', 'declined')),

    -- Vikram's columns. The signal job can never touch these.
    notes               text,
    contact_history     text,
    last_contacted_at   date,

    -- Signal-derived columns. The ONLY four the signal job may UPDATE.
    last_signal_date    date,
    signal_summary      text,
    follow_up_score     integer CHECK (follow_up_score BETWEEN 0 AND 100),
    recommended_action  text
                        CHECK (recommended_action IN ('reach_out', 'nudge', 'hold', 'quiet_list', 'skip'))
);

CREATE TABLE IF NOT EXISTS vc_profile (
    vc_id         integer PRIMARY KEY REFERENCES vcs(id) ON DELETE CASCADE,
    geo           text NOT NULL DEFAULT 'Unknown',
    stage_focus   text NOT NULL DEFAULT '',
    is_investing  boolean NOT NULL DEFAULT true,
    flag          text NOT NULL DEFAULT '',
    background    text NOT NULL DEFAULT '',
    source_url    text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run_log (
    id            bigserial PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    vcs_total     integer NOT NULL DEFAULT 0,
    vcs_checked   integer NOT NULL DEFAULT 0,
    vcs_failed    integer NOT NULL DEFAULT 0,
    signals_kept  integer NOT NULL DEFAULT 0,
    signals_dropped integer NOT NULL DEFAULT 0,
    error         text
);

CREATE TABLE IF NOT EXISTS signal_evidence (
    id              bigserial PRIMARY KEY,
    vc_id           integer NOT NULL REFERENCES vcs(id) ON DELETE CASCADE,
    run_id          bigint REFERENCES run_log(id),
    type            text NOT NULL,
    summary         text NOT NULL,
    quote           text NOT NULL,
    url             text NOT NULL,
    published_date  date NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (vc_id, url)
);

CREATE TABLE IF NOT EXISTS vc_check_log (
    id            bigserial PRIMARY KEY,
    vc_id         integer NOT NULL REFERENCES vcs(id) ON DELETE CASCADE,
    run_id        bigint REFERENCES run_log(id),
    checked_at    timestamptz NOT NULL DEFAULT now(),
    ok            boolean NOT NULL,
    outcome       text NOT NULL,           -- signal | no_signal | skipped_fit | failed
    n_pages       integer NOT NULL DEFAULT 0,
    n_signals     integer NOT NULL DEFAULT 0,
    n_dropped     integer NOT NULL DEFAULT 0,
    error         text,
    dropped       jsonb NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS vc_check_log_vc_time ON vc_check_log (vc_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS signal_feedback (
    id           bigserial PRIMARY KEY,
    evidence_id  bigint NOT NULL REFERENCES signal_evidence(id) ON DELETE CASCADE,
    vc_id        integer NOT NULL REFERENCES vcs(id) ON DELETE CASCADE,
    useful       boolean NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Source trust + people (added after v1; idempotent upgrades for existing databases)
ALTER TABLE signal_evidence ADD COLUMN IF NOT EXISTS source_domain text NOT NULL DEFAULT '';
ALTER TABLE signal_evidence ADD COLUMN IF NOT EXISTS source_tier text NOT NULL DEFAULT '';
ALTER TABLE signal_evidence ADD COLUMN IF NOT EXISTS person_name text;
ALTER TABLE signal_evidence ADD COLUMN IF NOT EXISTS person_role text;
ALTER TABLE vc_profile ADD COLUMN IF NOT EXISTS website text NOT NULL DEFAULT '';

-- Contacts Vikram enters himself (owner role only; the job never reads or writes this).
CREATE TABLE IF NOT EXISTS vc_contacts (
    id            serial PRIMARY KEY,
    vc_id         integer NOT NULL REFERENCES vcs(id) ON DELETE CASCADE,
    name          text NOT NULL,
    role          text NOT NULL DEFAULT '',
    linkedin_url  text NOT NULL DEFAULT '',
    email         text NOT NULL DEFAULT '',
    note          text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Vikram's activity log, and the people found at each firm.
CREATE TABLE IF NOT EXISTS vc_interactions (
    id          bigserial PRIMARY KEY,
    vc_id       integer NOT NULL REFERENCES vcs(id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('reached_out','follow_up','replied','call','meeting','declined','note')),
    note        text NOT NULL DEFAULT '',
    occurred_on date NOT NULL DEFAULT current_date,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vc_interactions_vc ON vc_interactions (vc_id, occurred_on DESC);

CREATE TABLE IF NOT EXISTS vc_people (
    id            serial PRIMARY KEY,
    vc_id         integer NOT NULL REFERENCES vcs(id) ON DELETE CASCADE,
    name          text NOT NULL,
    role          text NOT NULL DEFAULT '',
    source_url    text NOT NULL DEFAULT '',
    source_domain text NOT NULL DEFAULT '',
    source_tier   text NOT NULL DEFAULT '',
    quote         text NOT NULL DEFAULT '',
    discovered_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (vc_id, name)
);

-- ---------------------------------------------------------------- roles
-- Three least-privilege roles. Passwords are set by scripts/setup_db.py.
-- Vikram keeps the owner role (the one that ran this file) for his notes.

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'signal_job') THEN
        CREATE ROLE signal_job NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        CREATE ROLE dashboard_ro NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard_fb') THEN
        CREATE ROLE dashboard_fb NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard_ed') THEN
        CREATE ROLE dashboard_ed NOLOGIN;
    END IF;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM signal_job, dashboard_ro, dashboard_fb, dashboard_ed;
GRANT USAGE ON SCHEMA public TO signal_job, dashboard_ro, dashboard_fb, dashboard_ed;

-- signal_job: reads only what it needs (never notes / contact_history) ...
GRANT SELECT (id, name, firm, thesis, status, last_contacted_at, last_signal_date) ON vcs TO signal_job;
GRANT SELECT ON vc_profile TO signal_job;
-- ... and updates exactly four columns. Postgres refuses anything else.
GRANT UPDATE (last_signal_date, signal_summary, follow_up_score, recommended_action) ON vcs TO signal_job;
-- ... plus its own bookkeeping tables.
GRANT SELECT, INSERT, UPDATE ON run_log TO signal_job;
GRANT INSERT ON signal_evidence, vc_check_log TO signal_job;
-- ON CONFLICT (vc_id, url) needs to read the two conflict columns, nothing more.
GRANT SELECT (vc_id, url, run_id, source_domain, source_tier, person_name, person_role) ON signal_evidence TO signal_job;
-- A re-seen URL refreshes only its provenance fields (summary / quote / url never change).
GRANT UPDATE (run_id, source_domain, source_tier, person_name, person_role) ON signal_evidence TO signal_job;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO signal_job;

-- dashboard_ro: read everything, write nothing.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_ro;

-- dashboard_fb: INSERT on one table, nothing else.
GRANT INSERT ON signal_feedback TO dashboard_fb;
GRANT USAGE ON SEQUENCE signal_feedback_id_seq TO dashboard_fb;

-- signal_job also records the people it finds at each firm.
GRANT SELECT, INSERT ON vc_people TO signal_job;
GRANT UPDATE (role, source_url, source_domain, source_tier, quote, discovered_at) ON vc_people TO signal_job;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO signal_job;

-- dashboard_ed: Vikram's edit role, used only by the key-protected /api/edit endpoint.
-- It can change the four columns that are his, and add to his activity log and contacts.
GRANT SELECT ON vcs, vc_interactions, vc_contacts TO dashboard_ed;
GRANT UPDATE (status, last_contacted_at, notes, contact_history) ON vcs TO dashboard_ed;
GRANT INSERT ON vc_interactions, vc_contacts TO dashboard_ed;
GRANT USAGE ON SEQUENCE vc_interactions_id_seq, vc_contacts_id_seq TO dashboard_ed;
