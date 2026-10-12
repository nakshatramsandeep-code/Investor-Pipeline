const { neon } = require("@neondatabase/serverless");
const retry = require("./_retry");

// Reads through the dashboard_ro role (SELECT only).
module.exports = async function handler(req, res) {
  try {
    const sql = neon(process.env.DASHBOARD_RO_URL);
    const [vcs, evidence, runs, people, contacts, events] = await retry(() => Promise.all([
      sql`SELECT v.id, v.name, v.firm, v.status, v.next_step, v.revisit_on::text AS revisit_on, v.last_contacted_at::text AS last_contacted_at,
                 v.last_signal_date::text AS last_signal_date, v.signal_summary, v.follow_up_score, v.recommended_action,
                 v.thesis, p.geo, p.stage_focus, p.flag, p.background,
                 c.checked_at, c.ok AS check_ok, c.outcome, c.error AS check_error,
                 sr.searched_at AS last_searched_at,
                 f.level AS fit_level, f.points AS fit_points, f.good AS fit_good, f.concerns AS fit_concerns, f.badges AS fit_badges
          FROM vcs v
          JOIN vc_profile p ON p.vc_id = v.id
          LEFT JOIN vc_fit f ON f.vc_id = v.id
          LEFT JOIN LATERAL (
            SELECT checked_at, ok, outcome, error FROM vc_check_log
            WHERE vc_id = v.id ORDER BY checked_at DESC LIMIT 1) c ON true
          LEFT JOIN LATERAL (
            SELECT max(checked_at) AS searched_at FROM vc_check_log
            WHERE vc_id = v.id AND ok AND outcome IN ('signal', 'no_signal')) sr ON true`,
      sql`SELECT e.id, e.vc_id, e.type, e.summary, e.quote, e.url, e.published_date::text AS published_date,
                 e.source_domain, e.source_tier, e.relevance, e.person_name, e.person_role,
                 (SELECT f.useful FROM signal_feedback f WHERE f.evidence_id = e.id
                  ORDER BY f.created_at DESC LIMIT 1) AS useful
          FROM signal_evidence e
          WHERE e.published_date >= current_date - 30 AND e.active
          ORDER BY e.published_date DESC`,
      sql`SELECT id, started_at, finished_at, status, vcs_total, vcs_checked, vcs_failed,
                 signals_kept, signals_dropped, error
          FROM run_log WHERE kind = 'search' ORDER BY started_at DESC LIMIT 1`,
      sql`SELECT vc_id, name, role FROM vc_people ORDER BY vc_id, id`,
      sql`SELECT vc_id, name, role FROM vc_contacts ORDER BY vc_id, id`,
      sql`SELECT id, vc_id, on_date::text AS on_date, kind, title, note, done FROM vc_events WHERE on_date >= current_date - 45 ORDER BY on_date, id`,
    ]));
    res.setHeader("Cache-Control", "no-store");
    res.status(200).json({ vcs, evidence, run: runs[0] || null, people, contacts, events, now: new Date().toISOString() });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "could not read pipeline" });
  }
};
