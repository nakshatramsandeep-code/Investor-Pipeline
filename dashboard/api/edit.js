const { neon } = require("@neondatabase/serverless");
const isEditor = require("./_edit");

// Vikram's edits, through the dashboard_ed role: it can change status, last_contacted_at, notes and
// contact_history, and add to the activity log and contacts. Nothing else.
const STATUSES = ["to_contact", "waiting", "in_conversation", "declined"];
const KINDS = ["reached_out", "follow_up", "replied", "call", "meeting", "declined", "note"];
const DATE = /^\d{4}-\d{2}-\d{2}$/;

module.exports = async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  if (!process.env.EDIT_KEY || !process.env.DASHBOARD_ED_URL) return res.status(403).json({ error: "editing is not enabled" });
  if (!isEditor(req)) return res.status(401).json({ error: "wrong edit key" });

  const b = req.body || {};
  const id = b.vc_id;
  if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: "vc_id required" });
  const sql = neon(process.env.DASHBOARD_ED_URL);

  try {
    if (b.action === "log") {
      const { kind } = b;
      const note = String(b.note || "").slice(0, 2000);
      const day = b.occurred_on || new Date().toISOString().slice(0, 10);
      if (!KINDS.includes(kind)) return res.status(400).json({ error: "unknown kind" });
      if (!DATE.test(day) || day > new Date(Date.now() + 864e5).toISOString().slice(0, 10)) return res.status(400).json({ error: "bad date" });
      if (b.status && !STATUSES.includes(b.status)) return res.status(400).json({ error: "bad status" });

      const q = [sql`INSERT INTO vc_interactions (vc_id, kind, note, occurred_on) VALUES (${id}, ${kind}, ${note}, ${day})`];
      if (kind !== "note") q.push(sql`UPDATE vcs SET last_contacted_at = GREATEST(COALESCE(last_contacted_at, ${day}::date), ${day}::date) WHERE id = ${id}`);
      if (kind === "reached_out" || kind === "follow_up") q.push(sql`UPDATE vcs SET status = CASE WHEN status = 'in_conversation' THEN status ELSE 'waiting' END WHERE id = ${id}`);
      if (kind === "replied" || kind === "call" || kind === "meeting") q.push(sql`UPDATE vcs SET status = 'in_conversation' WHERE id = ${id}`);
      if (kind === "declined") q.push(sql`UPDATE vcs SET status = 'declined' WHERE id = ${id}`);
      if (b.status) q.push(sql`UPDATE vcs SET status = ${b.status} WHERE id = ${id}`);   // explicit choice wins
      await sql.transaction(q);
      return res.status(201).json({ ok: true });
    }
    if (b.action === "notes") {
      await sql`UPDATE vcs SET notes = ${String(b.notes || "").slice(0, 10000)} WHERE id = ${id}`;
      return res.status(200).json({ ok: true });
    }
    if (b.action === "contact") {
      const name = String(b.name || "").trim().slice(0, 120);
      if (!name) return res.status(400).json({ error: "name required" });
      await sql`INSERT INTO vc_contacts (vc_id, name, role, linkedin_url, note)
                VALUES (${id}, ${name}, ${String(b.role || "").slice(0, 120)}, ${String(b.linkedin_url || "").slice(0, 300)}, ${String(b.note || "").slice(0, 500)})`;
      return res.status(201).json({ ok: true });
    }
    return res.status(400).json({ error: "unknown action" });
  } catch (e) {
    console.error(e);
    return res.status(500).json({ error: "could not save" });
  }
};
