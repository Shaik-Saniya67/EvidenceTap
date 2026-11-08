"""End-to-end check of the EvidenceTap flow. Run: python test_flow.py"""
import hashlib
import io
import os
import tempfile

os.environ["EVIDENCETAP_DATA"] = tempfile.mkdtemp()
os.environ["EVIDENCETAP_DEMO"] = "1"

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import app as appmod  # noqa: E402

c = TestClient(appmod.app)


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


# pages load
for path in ("/", "/verify", "/record?id=x", "/s/abc", "/static/style.css", "/static/common.js", "/health"):
    ok(c.get(path).status_code == 200, f"GET {path}")

# 1. create + chunks + finish
r = c.post("/api/records", json={"device_time": "2020-01-01T00:00:00Z", "lat": 13.2, "lon": 79.1,
                                  "accuracy": 12, "mime": "audio/webm"}).json()
rid, tok, share = r["id"], r["owner_token"], r["share_token"]
H = {"X-Token": tok}
ok(len(rid) == 10, "record created")

parts = [os.urandom(2000) for _ in range(3)]
for i, p in enumerate(parts):
    res = c.post(f"/api/records/{rid}/chunk?seq={i}&mime=audio/webm&lat=13.2{i}&lon=79.1", content=p, headers=H)
    ok(res.status_code == 200 and res.json()["sha256"] == hashlib.sha256(p).hexdigest(), f"chunk {i} uploaded + hashed")
ok(c.post(f"/api/records/{rid}/chunk?seq=9", content=b"x").status_code == 403, "chunk without token rejected")

fin = c.post(f"/api/records/{rid}/finish", headers=H).json()
ok(fin["status"] == "sealed" and fin["has_audio"], "finish seals and builds combined audio")
audio = c.get(f"/api/records/{rid}/audio", headers=H)
ok(audio.content == b"".join(parts), "combined audio equals chunks in order")
ok(c.post(f"/api/records/{rid}/chunk?seq=10", content=b"late", headers=H).status_code == 409, "no chunks after sealing")

# 2. photo + details
buf = io.BytesIO(); Image.new("RGB", (60, 40), (200, 80, 90)).save(buf, "PNG"); png = buf.getvalue()
ph = c.post(f"/api/records/{rid}/photo", files={"file": ("shot.png", png, "image/png")}, headers=H)
ok(ph.status_code == 200, "photo uploaded")
ok(c.post(f"/api/records/{rid}/photo", files={"file": ("x.exe", b"MZ", "application/octet-stream")}, headers=H).status_code == 400, "non-photo rejected")
for v in (1, 2):
    d = c.post(f"/api/records/{rid}/details", json={"incident_type": "Stalking or following", "description": f"v{v} <b>x</b>",
                                                     "people": "bike KA01", "safety_status": "I am safe now"}, headers=H).json()
    ok(d["version"] == v, f"details saved as version {v}")

# 3. integrity intact
ig = c.get(f"/api/records/{rid}/integrity", headers=H).json()
ok(ig["ok"], f"integrity intact ({ig['checked']} items)")

# 4. sharing: scope location hides evidence, full shows it
ok(c.get(f"/api/s/{share}").status_code == 404, "share link inactive before sharing")
c.post(f"/api/records/{rid}/share", json={"scope": "location", "contact_name": "Amma"}, headers=H)
s = c.get(f"/api/s/{share}").json()
ok("description" not in s and "items" not in s, "location-only share hides notes and files")
ok(c.get(f"/api/records/{rid}/audio?t={share}").status_code == 403, "location-only share cannot fetch audio")
c.post(f"/api/records/{rid}/share", json={"scope": "full", "contact_name": "Amma"}, headers=H)
s = c.get(f"/api/s/{share}").json()
ok(s["description"].startswith("v2") and any(i["kind"] == "photo" for i in s["items"]), "full share shows latest notes and photo")
ok(c.get(f"/api/records/{rid}/audio?t={share}").status_code == 200, "full share can play audio")
ok(c.get(f"/api/records/{rid}", headers={"X-Token": share}).json().get("access") == "share", "share token is read-only access")
ok(c.delete(f"/api/records/{rid}", headers={"X-Token": share}).status_code == 403, "share token cannot delete")
ok(c.post(f"/api/s/{share}/ack", json={"status": "responding"}).status_code == 200, "contact acknowledgement")
ok(c.get(f"/api/records/{rid}", headers=H).json()["ack_status"] == "responding", "owner sees acknowledgement")

# 5. PDF
pdf = c.get(f"/api/records/{rid}/pdf", headers=H)
ok(pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and len(pdf.content) > 3000, f"PDF generated ({len(pdf.content)} bytes)")
ok(c.get(f"/api/records/{rid}/pdf?t={share}").status_code == 403, "PDF needs the owner token")

# 6. verify by hash
good = hashlib.sha256(png).hexdigest()
m = c.get(f"/api/verify/hash/{good}").json()["matches"]
ok(len(m) == 1 and m[0]["kind"] == "photo" and m[0]["record_id"] == rid, "original photo verifies")
edited = bytearray(png); edited[-5] ^= 1
ok(c.get(f"/api/verify/hash/{hashlib.sha256(bytes(edited)).hexdigest()}").json()["matches"] == [], "edited photo does not match")
fp = ig["fingerprint"]
ok(c.get(f"/api/verify/record/{rid}?fp={fp}").json()["fingerprint_matches_pdf"] is True, "PDF fingerprint matches record")
ok(c.get(f"/api/verify/record/{rid}?fp=deadbeef").json()["fingerprint_matches_pdf"] is False, "wrong fingerprint is flagged")

# 7. tamper detection on the server's stored file
ok(c.post(f"/api/demo/tamper/{rid}").status_code == 200, "demo tamper applied")
bad = c.get(f"/api/records/{rid}/integrity", headers=H).json()
ok(bad["ok"] is False and bad["reason"] == "file content changed", f"tampering detected: {bad['reason']}")

# 8. delete
ok(c.delete(f"/api/records/{rid}", headers=H).status_code == 200, "owner deletes record")
ok(c.get(f"/api/records/{rid}", headers=H).status_code == 404, "record gone")
ok(c.get(f"/api/s/{share}").status_code == 404, "share link dead after delete")
print("\nAll checks passed.")
