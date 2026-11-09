// Reliable chunk uploader for EvidenceTap.
//
// Each audio part moves through: pending -> uploading -> uploaded, or -> failed.
// - Temporary problems (network drop, HTTP 408/425/429/5xx) are retried with exponential backoff, up to maxAttempts.
// - Permanent problems (other 4xx, such as a refused conflicting part) are not retried.
// - A part that runs out of attempts, or fails permanently, is KEPT in memory as "failed". It is never discarded;
//   retryFailed() queues it again, for example after the connection comes back.
// Parts are uploaded one at a time, lowest sequence number first.
//
// Limit: parts live in this page's memory. Closing the tab loses parts that were not yet uploaded.
(function (root) {
  function createUploader(opts) {
    const send = opts.send; // async (part) => resolves when the server accepted it; rejects with an Error (err.status = HTTP code, if any)
    const maxAttempts = opts.maxAttempts ?? 6;
    const baseDelay = opts.baseDelay ?? 1000;
    const maxDelay = opts.maxDelay ?? 15000;
    const sleep = opts.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
    const onChange = opts.onChange || (() => {});
    const parts = new Map(); // seq -> { seq, blob, pos, state, attempts, error }
    let loop = null;

    const isRetryable = (err) => {
      const s = err && err.status;
      return !s || s === 408 || s === 425 || s === 429 || s >= 500; // no status = network failure
    };
    const list = () => [...parts.values()];
    const counts = () => {
      const c = { pending: 0, uploading: 0, uploaded: 0, failed: 0, total: parts.size };
      list().forEach((p) => { c[p.state]++; });
      return c;
    };
    const hasWork = () => list().some((p) => p.state === "pending" || p.state === "uploading");

    async function run() {
      for (;;) {
        const next = list().filter((p) => p.state === "pending").sort((a, b) => a.seq - b.seq)[0];
        if (!next) return;
        next.state = "uploading";
        next.attempts++;
        onChange(counts());
        try {
          await send(next);
          next.state = "uploaded";
          next.error = null;
          next.blob = null; // free memory once the server has it
        } catch (err) {
          next.error = (err && err.message) || String(err);
          if (isRetryable(err) && next.attempts < maxAttempts) {
            next.state = "pending";
            onChange(counts());
            await sleep(Math.min(maxDelay, baseDelay * 2 ** (next.attempts - 1)));
          } else {
            next.state = "failed"; // kept, with its data, until the user decides
          }
        }
        onChange(counts());
      }
    }
    function kick() {
      if (loop) return;
      loop = run().finally(() => { loop = null; if (hasWork()) kick(); });
    }

    return {
      add(part) {
        parts.set(part.seq, { seq: part.seq, blob: part.blob, pos: part.pos || null, state: "pending", attempts: 0, error: null });
        onChange(counts());
        kick();
      },
      retryFailed() {
        list().filter((p) => p.state === "failed").forEach((p) => { p.state = "pending"; p.attempts = 0; });
        onChange(counts());
        kick();
      },
      // Resolves when nothing is pending or uploading. Failed parts do not block it; check failed().
      async settled() {
        while (hasWork() || loop) await (loop || sleep(20));
      },
      counts,
      failed: () => list().filter((p) => p.state === "failed").map((p) => ({ seq: p.seq, error: p.error })),
      total: () => (parts.size ? Math.max(...parts.keys()) + 1 : 0),
      hasUnsaved: () => list().some((p) => p.state !== "uploaded"),
    };
  }

  const api = { createUploader };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ChunkUploader = api;
})(typeof window !== "undefined" ? window : globalThis);
