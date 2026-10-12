const { neon } = require("@neondatabase/serverless");

// Vikram's edits, through the dashboard_ed role: it can change status, last_contacted_at, notes, next step and
// revisit date, and add to / delete from his own activity log and contacts. Nothing else.
// There is no login: keep the site private with Vercel Deployment Protection.
//
// Dates: the browser sends the user's LOCAL date ("today" / "occurred_on"). The server never decides what
// day it is, because the server runs on UTC and that is the wrong calendar day for Vikram in the early hours.
const STATUSES = ["to_contact", "waiting", "in_conversation", "approved", "closed", "declined"];
const KINDS = ["reached_out", "follow_up", "replied", "call", "meeting", "approved", "closed", "declined", "note"];
const CHANNELS = ["", "email", "linkedin", "warm_intro", "phone", "event", "other"];
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const utcDay = () => new Date().toISOString().slice(0, 10);
const okDay = (d) => DATE.test(d) && d <= new Date(Date.now() + 864e5 * 1.5).toISOString().slice(0, 10);   // allow any timezone's "today"
const nullableDay = (d) => d == null || (typeof d === "string" && DATE.test(d));

module.exports = async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  if (!process.env.DASHBOARD_ED_URL) return res.status(403).json({ error: "editing is not enabled" });

  const b = req.body || {};
  const id = b.vc_id;
  if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: "vc_id required" });
  const sql = neon(process.env.DASHBOARD_ED_URL);
  const today = b.today || b.occurred_on || utcDay();
  if (!okDay(today)) return res.status(400).json({ error: "bad date" });

  try {
    if (b.action === "log") {
      const { kind } = b;
      const note = String(b.note || "").slice(0, 2000);
      const person = String(b.person || "").trim().slice(0, 120);
      const channel = b.channel || "";
      if (!CHANNELS.includes(channel)) return res.status(400).json({ error: "unknown channel" });
      const day = b.occurred_on || today;
      if (!KINDS.includes(kind)) return res.status(400).json({ error: "unknown kind" });
      if (!okDay(day)) return res.status(400).json({ error: "bad date" });
      if (b.status && !STATUSES.includes(b.status)) return res.status(400).json({ error: "bad status" });

      // Remember where this investor was, so the Undo toast can put it back exactly.
      const cur = (await sql`SELECT status, last_contacted_at::text AS last_contacted_at, revisit_on::text AS revisit_on FROM vcs WHERE id = ${id}`)[0];
      if (!cur) return res.status(404).json({ error: "not found" });

      const q = [sql`INSERT INTO vc_interactions (vc_id, kind, note, occurred_on, person, channel) VALUES (${id}, ${kind}, ${note}, ${day}, ${person}, ${channel}) RETURNING id`];
      if (kind !== "note") q.push(sql`UPDATE vcs SET last_contacted_at = GREATEST(COALESCE(last_contacted_at, ${day}::date), ${day}::date) WHERE id = ${id}`);
      if (kind === "reached_out" || kind === "follow_up") q.push(sql`UPDATE vcs SET status = CASE WHEN status IN ('in_conversation', 'approved', 'closed') THEN status ELSE 'waiting' END WHERE id = ${id}`);
      if (kind === "replied" || kind === "call" || kind === "meeting") q.push(sql`UPDATE vcs SET status = CASE WHEN status IN ('approved', 'closed') THEN status ELSE 'in_conversation' END WHERE id = ${id}`);
      if (kind === "approved") q.push(sql`UPDATE vcs SET status = CASE WHEN status = 'closed' THEN status ELSE 'approved' END WHERE id = ${id}`);
      if (kind === "closed") q.push(sql`UPDATE vcs SET status = 'closed' WHERE id = ${id}`);
      if (kind === "declined") q.push(sql`UPDATE vcs SET status = 'declined' WHERE id = ${id}`);
      if (b.status) q.push(sql`UPDATE vcs SET status = ${b.status} WHERE id = ${id}`);   // explicit choice wins
      const results = await sql.transaction(q);
      return res.status(201).json({ ok: true, id: results[0][0].id, prev: cur });
    }

    if (b.action === "undo_log") {
      // Delete exactly that entry and restore exactly what it changed.
      const iid = b.interaction_id, prev = b.prev || {};
      if (!Number.isInteger(iid) || !STATUSES.includes(prev.status) || !nullableDay(prev.last_contacted_at) || !nullableDay(prev.revisit_on)) {
        return res.status(400).json({ error: "bad undo" });
      }
      await sql.transaction([
        sql`DELETE FROM vc_interactions WHERE id = ${iid} AND vc_id = ${id}`,
        sql`UPDATE vcs SET status = ${prev.status}, last_contacted_at = ${prev.last_contacted_at}::date, revisit_on = ${prev.revisit_on}::date WHERE id = ${id}`,
      ]);
      return res.status(200).json({ ok: true });
    }

    if (b.action === "delete_interaction") {
      const iid = b.interaction_id;
      if (!Number.isInteger(iid)) return res.status(400).json({ error: "interaction_id required" });
      await sql.transaction([
        sql`DELETE FROM vc_interactions WHERE id = ${iid} AND vc_id = ${id}`,
        // "Last contacted" follows the log: it becomes the latest remaining contact, or blank if there is none.
        sql`UPDATE vcs SET last_contacted_at = (SELECT max(occurred_on) FROM vc_interactions WHERE vc_id = ${id} AND kind <> 'note') WHERE id = ${id}`,
      ]);
      return res.status(200).json({ ok: true });
    }

    if (b.action === "clear_contacted") {
      await sql`UPDATE vcs SET last_contacted_at = NULL WHERE id = ${id}`;
      return res.status(200).json({ ok: true });
    }

    if (b.action === "revisit") {
      await sql.transaction([
        sql`UPDATE vcs SET revisit_on = (${today}::date + interval '6 months')::date WHERE id = ${id}`,
        sql`INSERT INTO vc_interactions (vc_id, kind, note, occurred_on) VALUES (${id}, 'note', 'Will revisit in 6 months', ${today}::date)`,
      ]);
      return res.status(200).json({ ok: true });
    }
    if (b.action === "undo_decline") {
      await sql.transaction([
        sql`UPDATE vcs SET status = 'to_contact', revisit_on = NULL WHERE id = ${id} AND status = 'declined'`,
        sql`INSERT INTO vc_interactions (vc_id, kind, note, occurred_on) VALUES (${id}, 'note', 'Undid the decline: back to prospect', ${today}::date)`,
      ]);
      return res.status(200).json({ ok: true });
    }
    if (b.action === "next_step") {
      await sql`UPDATE vcs SET next_step = ${String(b.next_step || "").slice(0, 200)} WHERE id = ${id}`;
      return res.status(200).json({ ok: true });
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
