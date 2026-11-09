"""Audio-reliability regression tests for EvidenceTap (Phases 2-3).

Run:  python -m unittest -v test_audio_reliability

Every test is independent, so one failure never hides another. Tests check stored
bytes, database rows and the hash chain, not only HTTP status codes.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

DATA_DIR = tempfile.mkdtemp(prefix="et_audio_")
os.environ["EVIDENCETAP_DATA"] = DATA_DIR
os.environ.pop("EVIDENCETAP_DEMO", None)

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402


def new_client():
    # 500s must come back as responses so a server crash is a test failure, not a test error.
    return TestClient(appmod.app, raise_server_exceptions=False)


def blob(n: int, size: int = 300) -> bytes:
    """Distinct, deterministic bytes for chunk n."""
    return (hashlib.sha256(f"chunk-{n}".encode()).digest() * (size // 32 + 1))[:size]


def joined(*seqs) -> bytes:
    return b"".join(blob(s) for s in seqs)


class Rec:
    """One record plus helpers that read the real database and files."""

    def __init__(self, mime="audio/webm"):
        self.c = new_client()
        self.mime = mime
        r = self.c.post("/api/records", json={
            "device_time": "2026-11-09T00:00:00Z", "lat": 13.2, "lon": 79.1,
            "accuracy": 10, "mime": mime}).json()
        self.id, self.tok, self.share = r["id"], r["owner_token"], r["share_token"]
        self.H = {"X-Token": self.tok}

    def chunk(self, seq, data=None, client=None, mime=None):
        data = blob(seq) if data is None else data
        return (client or self.c).post(
            f"/api/records/{self.id}/chunk",
            params={"seq": seq, "mime": mime or self.mime}, content=data, headers=self.H)

    def finish(self, total=None, force=False, client=None):
        params = {}
        if total is not None:
            params["total"] = total
        if force:
            params["force"] = "true"
        return (client or self.c).post(f"/api/records/{self.id}/finish", params=params, headers=self.H)

    def audio(self):
        r = self.c.get(f"/api/records/{self.id}/audio", headers=self.H)
        return r.content if r.status_code == 200 else None

    def view(self):
        return self.c.get(f"/api/records/{self.id}", headers=self.H).json()

    def integrity(self):
        return self.c.get(f"/api/records/{self.id}/integrity", headers=self.H).json()

    def rows(self, kind=None):
        con = sqlite3.connect(os.path.join(DATA_DIR, "evidencetap.db"))
        con.row_factory = sqlite3.Row
        q = "SELECT id,kind,filename,sha256,meta FROM items WHERE record_id=?"
        args = [self.id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        out = con.execute(q + " ORDER BY id", args).fetchall()
        con.close()
        return out

    def seqs(self):
        return sorted(json.loads(r["meta"])["seq"] for r in self.rows("audio_chunk"))

    def chunk_path(self, seq):
        return appmod.FILES / self.id / f"chunk_{seq:05d}.part"


class DuplicateAndConflictingChunks(unittest.TestCase):
    def test_identical_retry_creates_no_new_evidence_entry(self):
        r = Rec()
        self.assertEqual(r.chunk(0).status_code, 200)
        self.assertEqual(r.chunk(1).status_code, 200)
        before = len(r.rows())
        retry = r.chunk(1)
        self.assertEqual(retry.status_code, 200, "an identical retry must be accepted")
        self.assertEqual(len(r.rows()), before, "retry must not add a row to the hash chain")
        self.assertEqual(r.seqs(), [0, 1])

    def test_identical_retry_does_not_duplicate_audio(self):
        r = Rec()
        r.chunk(0)
        r.chunk(1)
        r.chunk(1)  # the retry
        self.assertEqual(r.finish(total=2).status_code, 200)
        self.assertEqual(r.audio(), joined(0, 1), "audio must be exactly chunk0+chunk1, with no repeated segment")
        self.assertTrue(r.integrity()["ok"])

    def test_conflicting_chunk_is_rejected(self):
        r = Rec()
        r.chunk(0)
        bad = r.chunk(0, data=b"DIFFERENT CONTENT" * 10)
        self.assertEqual(bad.status_code, 409, "different bytes for an accepted seq must be rejected")

    def test_conflicting_chunk_never_overwrites_original(self):
        r = Rec()
        r.chunk(0)
        r.chunk(0, data=b"DIFFERENT CONTENT" * 10)
        self.assertEqual(r.chunk_path(0).read_bytes(), blob(0), "original file must be untouched")
        self.assertTrue(r.integrity()["ok"], "original evidence must still verify")
        self.assertEqual(len(r.rows("audio_chunk")), 1)
        self.assertEqual(r.finish(total=1).status_code, 200)
        self.assertEqual(r.audio(), blob(0))

    def test_concurrent_identical_uploads_create_one_row(self):
        r = Rec()
        bar = threading.Barrier(6)

        def go(_):
            cl = new_client()
            bar.wait()
            return r.chunk(0, client=cl).status_code

        with ThreadPoolExecutor(6) as ex:
            codes = list(ex.map(go, range(6)))
        self.assertEqual(set(codes), {200}, f"all identical uploads should succeed: {codes}")
        self.assertEqual(len(r.rows("audio_chunk")), 1, "exactly one evidence row for seq 0")
        self.assertTrue(r.integrity()["ok"])

    def test_concurrent_conflicting_uploads_keep_exactly_one_version(self):
        r = Rec()
        versions = [b"version-%d-" % i * 20 for i in range(4)]
        bar = threading.Barrier(4)

        def go(i):
            cl = new_client()
            bar.wait()
            return r.chunk(0, data=versions[i], client=cl).status_code

        with ThreadPoolExecutor(4) as ex:
            codes = list(ex.map(go, range(4)))
        self.assertEqual(codes.count(200), 1, f"exactly one version may win: {codes}")
        self.assertEqual(len(r.rows("audio_chunk")), 1)
        stored = r.chunk_path(0).read_bytes()
        self.assertIn(stored, versions)
        self.assertEqual(hashlib.sha256(stored).hexdigest(), r.rows("audio_chunk")[0]["sha256"])
        self.assertTrue(r.integrity()["ok"])

    def test_negative_or_huge_sequence_numbers_are_rejected(self):
        r = Rec()
        self.assertEqual(r.chunk(-1, data=b"x").status_code, 422)
        self.assertEqual(r.chunk(10_000_000, data=b"x").status_code, 422)
        self.assertEqual(len(r.rows("audio_chunk")), 0)


class MissingAndOutOfOrder(unittest.TestCase):
    def test_gap_blocks_finish_and_names_the_missing_sequence(self):
        r = Rec()
        r.chunk(0)
        r.chunk(2)
        f = r.finish(total=3)
        self.assertEqual(f.status_code, 409, "a recording with a gap must not be sealed")
        detail = f.json()["detail"]
        self.assertEqual(detail["missing"], [1])
        self.assertEqual(r.view()["status"], "recording", "record must stay open for recovery")
        self.assertEqual(r.rows("audio"), [], "no combined audio may be built from a gap")

    def test_gap_is_detected_even_without_a_declared_total(self):
        r = Rec()
        r.chunk(0)
        r.chunk(2)
        f = r.finish()
        self.assertEqual(f.status_code, 409)
        self.assertEqual(f.json()["detail"]["missing"], [1])

    def test_lost_final_chunk_is_detected_when_total_is_declared(self):
        r = Rec()
        r.chunk(0)
        r.chunk(1)
        f = r.finish(total=3)
        self.assertEqual(f.status_code, 409)
        self.assertEqual(f.json()["detail"]["missing"], [2])

    def test_incomplete_evidence_is_preserved_and_can_be_completed_later(self):
        r = Rec()
        r.chunk(0)
        r.chunk(2)
        self.assertEqual(r.finish(total=3).status_code, 409)
        self.assertEqual(r.seqs(), [0, 2], "received chunks must be preserved after a failed finish")
        self.assertEqual(r.chunk(1).status_code, 200, "recovery: the missing chunk can still be uploaded")
        f = r.finish(total=3)
        self.assertEqual(f.status_code, 200)
        self.assertEqual(r.view()["status"], "sealed")
        self.assertEqual(r.audio(), joined(0, 1, 2))
        self.assertTrue(r.integrity()["ok"])

    def test_explicit_force_seals_as_incomplete_with_only_the_verified_prefix(self):
        r = Rec()
        r.chunk(0)
        r.chunk(2)
        f = r.finish(total=3, force=True)
        self.assertEqual(f.status_code, 200)
        self.assertEqual(r.view()["status"], "incomplete", "must never be labelled complete")
        self.assertEqual(r.audio(), blob(0), "audio contains only the contiguous verified part")
        audio_meta = json.loads(r.rows("audio")[0]["meta"])
        self.assertIs(audio_meta["complete"], False)
        self.assertEqual(audio_meta["missing"], [1])
        self.assertEqual(r.seqs(), [0, 2], "the out-of-prefix chunk is kept as evidence")
        self.assertTrue(r.integrity()["ok"])

    def test_out_of_order_uploads_are_reconstructed_by_sequence(self):
        r = Rec()
        for s in (2, 0, 1):
            self.assertEqual(r.chunk(s).status_code, 200)
        self.assertEqual(r.finish(total=3).status_code, 200)
        self.assertEqual(r.audio(), joined(0, 1, 2), "audio must follow sequence numbers, not arrival order")
        self.assertTrue(r.integrity()["ok"])

    def test_finish_with_no_chunks_is_not_a_complete_recording(self):
        r = Rec()
        f = r.finish()
        self.assertEqual(f.status_code, 409)
        self.assertEqual(r.view()["status"], "recording")
        forced = r.finish(force=True)
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(r.view()["status"], "incomplete")
        self.assertFalse(r.view()["has_audio"])


class CorruptionDetection(unittest.TestCase):
    def test_corrupted_chunk_file_blocks_finish(self):
        r = Rec()
        r.chunk(0)
        r.chunk(1)
        r.chunk_path(1).write_bytes(b"\x00" * len(blob(1)))  # same length, wrong content
        f = r.finish(total=2)
        self.assertEqual(f.status_code, 409, "a chunk that no longer matches its hash must block sealing")
        self.assertEqual(f.json()["detail"]["damaged"], [1])
        self.assertEqual(r.rows("audio"), [])
        self.assertEqual(r.view()["status"], "recording")

    def test_missing_chunk_file_blocks_finish(self):
        r = Rec()
        r.chunk(0)
        r.chunk(1)
        r.chunk_path(0).unlink()
        f = r.finish(total=2)
        self.assertEqual(f.status_code, 409)
        self.assertEqual(f.json()["detail"]["damaged"], [0])
        self.assertEqual(r.rows("audio"), [])

    def test_identical_retry_repairs_a_damaged_chunk_file(self):
        r = Rec()
        r.chunk(0)
        r.chunk(1)
        r.chunk_path(1).write_bytes(b"\x00" * len(blob(1)))
        self.assertEqual(r.chunk(1).status_code, 200)
        self.assertEqual(r.chunk_path(1).read_bytes(), blob(1))
        self.assertEqual(len(r.rows("audio_chunk")), 2, "repair must not add a row")
        self.assertEqual(r.finish(total=2).status_code, 200)
        self.assertEqual(r.audio(), joined(0, 1))
        self.assertTrue(r.integrity()["ok"])


class FinalisationSafety(unittest.TestCase):
    def test_repeated_finish_is_safe_and_builds_audio_once(self):
        r = Rec()
        for s in range(3):
            r.chunk(s)
        first = r.finish(total=3)
        second = r.finish(total=3)
        third = r.finish()
        self.assertEqual([first.status_code, second.status_code, third.status_code], [200, 200, 200])
        self.assertEqual(len(r.rows("audio")), 1, "audio must be reconstructed exactly once")
        self.assertEqual(r.audio(), joined(0, 1, 2))
        self.assertTrue(r.integrity()["ok"])

    def test_simultaneous_finish_requests_build_audio_once(self):
        r = Rec()
        for s in range(6):
            r.chunk(s, data=blob(s, 50_000))
        bar = threading.Barrier(8)

        def go(_):
            cl = new_client()
            bar.wait()
            return r.finish(total=6, client=cl).status_code

        with ThreadPoolExecutor(8) as ex:
            codes = list(ex.map(go, range(8)))
        self.assertEqual(set(codes), {200}, f"all finish calls should succeed: {codes}")
        self.assertEqual(len(r.rows("audio")), 1, "exactly one combined audio row")
        self.assertEqual(r.audio(), b"".join(blob(s, 50_000) for s in range(6)))
        self.assertEqual(r.view()["status"], "sealed")
        self.assertTrue(r.integrity()["ok"])

    def test_upload_after_seal_is_rejected_and_changes_nothing(self):
        r = Rec()
        r.chunk(0)
        r.finish(total=1)
        before = len(r.rows())
        self.assertEqual(r.chunk(1).status_code, 409)
        self.assertEqual(len(r.rows()), before)
        self.assertEqual(r.audio(), blob(0))

    def test_uploads_racing_finish_never_land_after_the_seal(self):
        r = Rec()
        for s in range(5):
            r.chunk(s)
        codes = {}
        bar = threading.Barrier(2)

        def uploader():
            cl = new_client()
            bar.wait()
            for s in range(5, 45):
                codes[s] = r.chunk(s, client=cl).status_code

        result = {}

        def finisher():
            cl = new_client()
            bar.wait()
            time.sleep(0.02)
            result["finish"] = r.finish(client=cl).status_code

        ts = [threading.Thread(target=uploader), threading.Thread(target=finisher)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(result["finish"], 200)
        self.assertEqual(r.view()["status"], "sealed")
        stored = r.seqs()
        n = len(stored)
        self.assertEqual(stored, list(range(n)), "stored chunks must be a contiguous prefix")
        self.assertEqual(r.audio(), joined(*range(n)),
                         "sealed audio must contain every accepted chunk and nothing else")
        for s, code in codes.items():
            self.assertIn(code, (200, 409))
            self.assertEqual(code == 200, s < n, f"chunk {s}: HTTP {code} but sealed with {n} chunks")
        self.assertTrue(r.integrity()["ok"])


class FailureAndRecovery(unittest.TestCase):
    def test_server_failure_mid_upload_leaves_no_partial_state_and_retry_works(self):
        r = Rec()
        r.chunk(0)

        def explode(*a, **k):
            raise RuntimeError("simulated crash while sealing the chunk")

        with mock.patch.object(appmod, "chain_step", explode):
            bad = r.chunk(1)
        self.assertEqual(bad.status_code, 500)
        self.assertEqual(r.seqs(), [0], "a failed upload must leave no database row")
        self.assertEqual(r.chunk(1).status_code, 200, "the client's retry must succeed")
        self.assertEqual(r.seqs(), [0, 1])
        self.assertEqual(r.finish(total=2).status_code, 200)
        self.assertEqual(r.audio(), joined(0, 1))
        self.assertTrue(r.integrity()["ok"])

    def test_interrupted_recording_stays_recoverable(self):
        r = Rec()
        for s in range(3):
            r.chunk(s)
        # the phone vanished: no finish call. The record is still open and readable.
        self.assertEqual(r.view()["status"], "recording")
        self.assertEqual(r.seqs(), [0, 1, 2])
        self.assertEqual(r.finish().status_code, 200)
        self.assertEqual(r.audio(), joined(0, 1, 2))

    def test_normal_flow_with_and_without_declared_total(self):
        for total in (None, 3):
            with self.subTest(total=total):
                r = Rec()
                for s in range(3):
                    r.chunk(s)
                self.assertEqual(r.finish(total=total).status_code, 200)
                self.assertEqual(r.view()["status"], "sealed")
                self.assertEqual(r.audio(), joined(0, 1, 2))
                self.assertTrue(r.integrity()["ok"])

    def test_mime_type_is_preserved(self):
        r = Rec(mime="audio/mp4")
        r.chunk(0)
        r.chunk(1)
        self.assertEqual(r.finish(total=2).status_code, 200)
        audio_row = r.rows("audio")[0]
        self.assertTrue(audio_row["filename"].endswith(".mp4"))
        resp = r.c.get(f"/api/records/{r.id}/audio", headers=r.H)
        self.assertEqual(resp.headers["content-type"], "audio/mp4")
        self.assertEqual(json.loads(audio_row["meta"])["mime"], "audio/mp4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
