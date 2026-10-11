const crypto = require("crypto");

// Writes (and Vikram's private notes) need the edit key. Reading the pipeline stays open.
// With EDIT_KEY unset, editing is switched off entirely.
module.exports = function isEditor(req) {
  const expected = process.env.EDIT_KEY || "";
  const got = String(req.headers["x-edit-key"] || "");
  if (!expected || got.length !== expected.length) return false;
  return crypto.timingSafeEqual(Buffer.from(got), Buffer.from(expected));
};
