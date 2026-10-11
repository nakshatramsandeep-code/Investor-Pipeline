const { neon } = require("@neondatabase/serverless");

// Reads through the dashboard_ro role (SELECT only).
module.exports = async function handler(req, res) {
  try {
    const sql = neon(process.env.DASHBOARD_RO_URL);
    const [vcs, evidence, runs] = await Promise.all([
      sql`SELECT v.id, v.name, v.firm, v.status, v.last_contacted_at::text AS last_contacted_at,
                 v.last_signal_date::text AS last_signal_date, v.signal_summary, v.follow_up_score, v.recommended_action,
                 p.geo, p.stage_focus, p.flag,
                 c.checked_at, c.ok AS check_ok, c.outcome, c.error AS check_error
          FROM vcs v
          JOIN vc_profile p ON p.vc_id = v.id
          LEFT JOIN LATERAL (
            SELECT checked_at, ok, outcome, error FROM vc_check_log
            WHERE vc_id = v.id ORDER BY checked_at DESC LIMIT 1) c ON true`,
      sql`SELECT e.id, e.vc_id, e.type, e.summary, e.quote, e.url, e.published_date::text AS published_date,
                 e.source_domain, e.source_tier, e.relevance,
                 (SELECT f.useful FROM signal_feedback f WHERE f.evidence_id = e.id
                  ORDER BY f.created_at DESC LIMIT 1) AS useful
          FROM signal_evidence e
          WHERE e.published_date >= current_date - 30
          ORDER BY e.published_date DESC`,
      sql`SELECT id, started_at, finished_at, status, vcs_total, vcs_checked, vcs_failed,
                 signals_kept, signals_dropped, error
          FROM run_log ORDER BY started_at DESC LIMIT 1`,
    ]);
    res.setHeader("Cache-Control", "no-store");
    res.status(200).json({ vcs, evidence, run: runs[0] || null, now: new Date().toISOString() });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "could not read pipeline" });
  }
};
