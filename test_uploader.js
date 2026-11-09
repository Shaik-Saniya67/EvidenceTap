// Tests for static/uploader.js (retry, recovery, no silent loss). Run: node test_uploader.js
const assert = require("node:assert/strict");
const { createUploader } = require("./static/uploader.js");

const netErr = () => new Error("Failed to fetch"); // no .status = network failure
const httpErr = (status, msg = "HTTP " + status) => Object.assign(new Error(msg), { status });
const blob = (n) => new Uint8Array([n, n, n]);

function setup(sendImpl, extra = {}) {
  const delays = [];
  const log = [];
  let active = 0, maxActive = 0;
  const send = async (part) => {
    active++; maxActive = Math.max(maxActive, active);
    log.push(part.seq);
    try { await Promise.resolve(); return await sendImpl(part, log.filter((s) => s === part.seq).length); }
    finally { active--; }
  };
  const up = createUploader({ send, sleep: (ms) => { delays.push(ms); return Promise.resolve(); }, ...extra });
  return { up, delays, log, maxActive: () => maxActive };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("a temporary network failure is retried with exponential backoff and then succeeds", async () => {
  const t = setup(async (p, attempt) => { if (attempt < 3) throw netErr(); });
  t.up.add({ seq: 0, blob: blob(0) });
  await t.up.settled();
  assert.deepEqual(t.up.counts(), { pending: 0, uploading: 0, uploaded: 1, failed: 0, total: 1 });
  assert.deepEqual(t.log, [0, 0, 0]);
  assert.deepEqual(t.delays, [1000, 2000]);
});

test("HTTP 5xx and 429 are treated as temporary", async () => {
  const t = setup(async (p, attempt) => { if (attempt === 1) throw httpErr(p.seq === 0 ? 503 : 429); });
  t.up.add({ seq: 0, blob: blob(0) });
  t.up.add({ seq: 1, blob: blob(1) });
  await t.up.settled();
  assert.equal(t.up.counts().uploaded, 2);
  assert.equal(t.up.counts().failed, 0);
});

test("a permanent refusal (409) is not retried, is kept, and does not block later parts", async () => {
  const t = setup(async (p) => { if (p.seq === 1) throw httpErr(409, "conflicting part"); });
  [0, 1, 2].forEach((s) => t.up.add({ seq: s, blob: blob(s) }));
  await t.up.settled();
  assert.deepEqual(t.log, [0, 1, 2], "part 1 must be tried exactly once");
  assert.deepEqual(t.up.counts(), { pending: 0, uploading: 0, uploaded: 2, failed: 1, total: 3 });
  assert.deepEqual(t.up.failed(), [{ seq: 1, error: "conflicting part" }]);
  assert.equal(t.up.hasUnsaved(), true);
});

test("running out of attempts keeps the part and its audio data; retryFailed recovers it", async () => {
  let online = false;
  const t = setup(async () => { if (!online) throw netErr(); }, { maxAttempts: 3 });
  const original = blob(7);
  t.up.add({ seq: 0, blob: original });
  await t.up.settled();
  assert.equal(t.log.length, 3, "stops after maxAttempts");
  assert.equal(t.up.counts().failed, 1);
  assert.equal(t.up.hasUnsaved(), true, "a failed part still counts as unsaved");
  online = true; // the connection comes back
  t.up.retryFailed();
  await t.up.settled();
  assert.deepEqual(t.up.counts(), { pending: 0, uploading: 0, uploaded: 1, failed: 0, total: 1 });
  assert.equal(t.up.hasUnsaved(), false);
});

test("a failed part keeps its original bytes until it is uploaded", async () => {
  let seen = null, online = false;
  const t = setup(async (p) => { seen = p.blob; if (!online) throw httpErr(500); }, { maxAttempts: 1 });
  const original = blob(9);
  t.up.add({ seq: 0, blob: original });
  await t.up.settled();
  assert.equal(seen, original, "the same data is offered on each attempt");
  online = true;
  t.up.retryFailed();
  await t.up.settled();
  assert.equal(seen, original, "and again on recovery");
});

test("uploads run one at a time and every part is sent exactly once", async () => {
  const t = setup(async () => {});
  for (let s = 0; s < 8; s++) t.up.add({ seq: s, blob: blob(s) });
  await t.up.settled();
  assert.equal(t.maxActive(), 1);
  assert.deepEqual([...t.log].sort((a, b) => a - b), [0, 1, 2, 3, 4, 5, 6, 7]);
  assert.equal(t.up.counts().uploaded, 8);
});

test("total() is the highest sequence number plus one, for the server's continuity check", async () => {
  const t = setup(async () => {});
  assert.equal(t.up.total(), 0);
  [0, 1, 2, 3].forEach((s) => t.up.add({ seq: s, blob: blob(s) }));
  await t.up.settled();
  assert.equal(t.up.total(), 4);
});

test("settled() waits for retries to finish instead of giving up after a fixed time", async () => {
  const t = setup(async (p, attempt) => { if (attempt < 5) throw netErr(); }, { maxAttempts: 6 });
  t.up.add({ seq: 0, blob: blob(0) });
  await t.up.settled();
  assert.equal(t.up.counts().uploaded, 1);
  assert.deepEqual(t.delays, [1000, 2000, 4000, 8000]);
});

test("backoff is capped", async () => {
  const t = setup(async (p, attempt) => { if (attempt < 7) throw netErr(); }, { maxAttempts: 8, maxDelay: 5000 });
  t.up.add({ seq: 0, blob: blob(0) });
  await t.up.settled();
  assert.ok(Math.max(...t.delays) <= 5000);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log("PASS  " + name); }
    catch (e) { failed++; console.log("FAIL  " + name + "\n      " + e.message.split("\n").join("\n      ")); }
  }
  console.log(failed ? `\n${failed} of ${tests.length} failed` : `\nAll ${tests.length} uploader checks passed.`);
  process.exit(failed ? 1 : 0);
})();
