const { neon } = require("@neondatabase/serverless");

// Writes through the dashboard_fb role, which can INSERT into signal_feedback and nothing else.
module.exports = async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  const { evidence_id, vc_id, useful } = req.body || {};
  if (!Number.isInteger(evidence_id) || !Number.isInteger(vc_id) || typeof useful !== "boolean") {
    return res.status(400).json({ error: "evidence_id, vc_id (integers) and useful (boolean) required" });
  }
  try {
    const sql = neon(process.env.DASHBOARD_FB_URL);
    await sql`INSERT INTO signal_feedback (evidence_id, vc_id, useful) VALUES (${evidence_id}, ${vc_id}, ${useful})`;
    res.status(201).json({ ok: true });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "could not save feedback" });
  }
};
