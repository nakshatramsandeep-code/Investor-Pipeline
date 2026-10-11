const { neon } = require("@neondatabase/serverless");
const isEditor = require("./_edit");

// One investor's profile. Public data comes through dashboard_ro (SELECT only).
// Vikram's private notes, contact history and activity log are returned only with a valid edit key,
// and contact emails are never returned.
module.exports = async function handler(req, res) {
  const id = Number(req.query.id);
  if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: "id must be a positive integer" });
  try {
    const sql = neon(process.env.DASHBOARD_RO_URL);
    const [vcs, evidence, checks, contacts, people] = await Promise.all([
      sql`SELECT v.id, v.name, v.firm, v.thesis, v.status, v.last_contacted_at::text AS last_contacted_at, v.last_signal_date::text AS last_signal_date,
                 v.signal_summary, v.follow_up_score, v.recommended_action,
                 p.geo, p.stage_focus, p.is_investing, p.flag, p.background, p.source_url, p.website
          FROM vcs v JOIN vc_profile p ON p.vc_id = v.id WHERE v.id = ${id}`,
      sql`SELECT e.id, e.vc_id, e.type, e.summary, e.quote, e.url, e.published_date::text AS published_date,
                 e.source_domain, e.source_tier, e.person_name, e.person_role,
                 (SELECT f.useful FROM signal_feedback f WHERE f.evidence_id = e.id
                  ORDER BY f.created_at DESC LIMIT 1) AS useful
          FROM signal_evidence e WHERE e.vc_id = ${id}
          ORDER BY e.published_date DESC LIMIT 50`,
      sql`SELECT checked_at, ok, outcome, n_pages, n_signals, n_dropped, error, dropped
          FROM vc_check_log WHERE vc_id = ${id} ORDER BY checked_at DESC LIMIT 10`,
      sql`SELECT name, role, linkedin_url, note FROM vc_contacts WHERE vc_id = ${id} ORDER BY id`,
      sql`SELECT name, role, source_url, source_domain, source_tier, quote, discovered_at
          FROM vc_people WHERE vc_id = ${id} ORDER BY id`,
    ]);
    if (!vcs.length) return res.status(404).json({ error: "not found" });

    const out = { vc: vcs[0], evidence, checks, contacts, people, editor: false, now: new Date().toISOString() };
    if (isEditor(req) && process.env.DASHBOARD_ED_URL) {
      const ed = neon(process.env.DASHBOARD_ED_URL);
      const [priv, interactions] = await Promise.all([
        ed`SELECT notes, contact_history FROM vcs WHERE id = ${id}`,
        ed`SELECT id, kind, note, occurred_on::text AS occurred_on FROM vc_interactions WHERE vc_id = ${id} ORDER BY occurred_on DESC, id DESC LIMIT 30`,
      ]);
      out.editor = true;
      out.notes = priv[0] ? priv[0].notes : null;
      out.contact_history = priv[0] ? priv[0].contact_history : null;
      out.interactions = interactions;
    }
    res.setHeader("Cache-Control", "no-store");
    res.status(200).json(out);
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "could not read profile" });
  }
};
