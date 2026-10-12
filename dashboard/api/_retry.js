// The Neon database suspends when idle and takes a few seconds to wake; the first query after a quiet spell can
// fail once. Retry reads a couple of times with a short pause. (Never used for writes: a retried INSERT could
// be applied twice.)
module.exports = async function retry(fn, tries = 3) {
  let last;
  for (let i = 0; i < tries; i++) {
    try {
      return await fn();
    } catch (e) {
      last = e;
      if (i < tries - 1) await new Promise((r) => setTimeout(r, 700 * (i + 1)));
    }
  }
  throw last;
};
