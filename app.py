"""EvidenceTap backend: an append-only, hash-chained evidence record.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import hashlib
import io
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BASE = Path(__file__).parent
DATA = Path(os.environ.get("EVIDENCETAP_DATA", BASE / "data"))
FILES = DATA / "files"
DB_PATH = DATA / "evidencetap.db"
FILES.mkdir(parents=True, exist_ok=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DEMO_MODE = os.environ.get("EVIDENCETAP_DEMO", "") == "1"
MAX_UPLOAD = 25 * 1024 * 1024  # 25 MB per file

app = FastAPI(title="EvidenceTap")
_lock = threading.Lock()


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY,
  owner_hash TEXT NOT NULL,
  share_token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  device_time TEXT,
  clock_skew_s REAL,
  lat REAL, lon REAL, accuracy REAL,
  last_lat REAL, last_lon REAL, last_pos_at TEXT,
  status TEXT NOT NULL DEFAULT 'recording',
  incident_type TEXT DEFAULT '',
  description TEXT DEFAULT '',
  people TEXT DEFAULT '',
  safety_status TEXT DEFAULT '',
  contact_name TEXT DEFAULT '',
  telegram_chat TEXT DEFAULT '',
  share_scope TEXT NOT NULL DEFAULT 'none',
  ack_status TEXT DEFAULT '',
  ack_at TEXT,
  mime TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  server_time TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  chain_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_record ON items(record_id);
CREATE INDEX IF NOT EXISTS idx_items_sha ON items(sha256);
"""


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


with db() as _c:
    _c.executescript(SCHEMA)


# ---------------------------------------------------------------- helpers
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def genesis(record_id: str) -> str:
    return sha256_bytes(f"evidencetap:{record_id}".encode())


def chain_step(prev: str, sha: str, server_time: str, kind: str, meta: str) -> str:
    return sha256_bytes("|".join([prev, sha, server_time, kind, meta]).encode())


def record_dir(rid: str) -> Path:
    d = FILES / rid
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_record(con, rid: str):
    row = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404, "Record not found")
    return row


def access_level(row, token: str | None) -> str | None:
    if not token:
        return None
    if secrets.compare_digest(sha256_bytes(token.encode()), row["owner_hash"]):
        return "owner"
    if secrets.compare_digest(token, row["share_token"]):
        return "share"
    return None


def require_owner(con, rid: str, token: str | None):
    row = get_record(con, rid)
    if access_level(row, token) != "owner":
        raise HTTPException(403, "Owner token required")
    return row


def add_item(con, rid: str, kind: str, filename: str, data: bytes, meta: dict | None = None):
    """Write a file and append it to the record's hash chain. Items are never edited."""
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (25 MB max)")
    meta_s = json.dumps(meta or {}, sort_keys=True, separators=(",", ":"))
    path = record_dir(rid) / filename
    with _lock:
        path.write_bytes(data)
        sha = sha256_bytes(data)
        t = now_iso()
        last = con.execute(
            "SELECT chain_hash FROM items WHERE record_id=? ORDER BY id DESC LIMIT 1", (rid,)
        ).fetchone()
        prev = last["chain_hash"] if last else genesis(rid)
        ch = chain_step(prev, sha, t, kind, meta_s)
        cur = con.execute(
            "INSERT INTO items(record_id,kind,filename,sha256,size,server_time,meta,chain_hash)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rid, kind, filename, sha, len(data), t, meta_s, ch),
        )
        con.commit()
    return {"id": cur.lastrowid, "sha256": sha, "server_time": t, "chain_hash": ch}


def check_integrity(con, rid: str) -> dict:
    """Recompute every hash from the files on disk and compare with the stored chain."""
    items = con.execute("SELECT * FROM items WHERE record_id=? ORDER BY id", (rid,)).fetchall()
    prev = genesis(rid)
    for it in items:
        p = record_dir(rid) / it["filename"]
        if not p.exists():
            return {"ok": False, "checked": it["id"], "broken_item": it["id"], "reason": "file missing"}
        if sha256_file(p) != it["sha256"]:
            return {"ok": False, "checked": len(items), "broken_item": it["id"], "reason": "file content changed"}
        expected = chain_step(prev, it["sha256"], it["server_time"], it["kind"], it["meta"])
        if expected != it["chain_hash"]:
            return {"ok": False, "checked": len(items), "broken_item": it["id"], "reason": "chain mismatch"}
        prev = it["chain_hash"]
    return {"ok": True, "checked": len(items), "fingerprint": prev}


def item_dict(it):
    return {
        "id": it["id"], "kind": it["kind"], "sha256": it["sha256"], "size": it["size"],
        "server_time": it["server_time"], "meta": json.loads(it["meta"]), "chain_hash": it["chain_hash"],
    }


def record_view(con, row, level: str):
    rid = row["id"]
    items = con.execute("SELECT * FROM items WHERE record_id=? ORDER BY id", (rid,)).fetchall()
    visible = [i for i in items if i["kind"] != "audio_chunk"]
    scope = row["share_scope"]
    full = level == "owner" or scope == "full"
    out = {
        "id": rid, "access": level, "created_at": row["created_at"], "status": row["status"],
        "clock_skew_s": row["clock_skew_s"],
        "lat": row["lat"], "lon": row["lon"], "accuracy": row["accuracy"],
        "last_lat": row["last_lat"], "last_lon": row["last_lon"], "last_pos_at": row["last_pos_at"],
        "incident_type": row["incident_type"], "safety_status": row["safety_status"],
        "ack_status": row["ack_status"], "ack_at": row["ack_at"], "share_scope": scope,
        "chunk_count": sum(1 for i in items if i["kind"] == "audio_chunk"),
        "fingerprint": items[-1]["chain_hash"] if items else None,
        "has_audio": any(i["kind"] == "audio" for i in items),
    }
    if full:
        out.update(description=row["description"], people=row["people"],
                   items=[item_dict(i) for i in visible])
    if level == "owner":
        out.update(contact_name=row["contact_name"], telegram_chat=row["telegram_chat"],
                   share_token=row["share_token"])
    return out


def send_telegram(chat: str, text: str):
    if not (TELEGRAM_TOKEN and chat):
        return

    def _go():
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data, timeout=10)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def tok(x_token: str | None, t: str | None) -> str | None:
    return x_token or t


# ---------------------------------------------------------------- API: create / capture
@app.post("/api/records")
async def create_record(request: Request):
    body = await request.json()
    rid = secrets.token_hex(5)
    owner = secrets.token_urlsafe(24)
    share = secrets.token_urlsafe(12)
    created = now_iso()
    skew = None
    dt = body.get("device_time")
    if dt:
        try:
            d = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            skew = round((d - datetime.fromisoformat(created)).total_seconds(), 1)
        except Exception:
            pass
    with db() as con:
        con.execute(
            "INSERT INTO records(id,owner_hash,share_token,created_at,device_time,clock_skew_s,"
            "lat,lon,accuracy,last_lat,last_lon,last_pos_at,mime) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, sha256_bytes(owner.encode()), share, created, dt, skew,
             body.get("lat"), body.get("lon"), body.get("accuracy"),
             body.get("lat"), body.get("lon"), created if body.get("lat") is not None else None,
             body.get("mime", "")),
        )
        # First chain item: the opening metadata, so time and place are sealed from tap one.
        meta = {"lat": body.get("lat"), "lon": body.get("lon"), "accuracy": body.get("accuracy"),
                "device_time": dt, "user_agent": (request.headers.get("user-agent") or "")[:200]}
        add_item(con, rid, "start", "start.json", json.dumps(meta, sort_keys=True).encode(), meta)
    return {"id": rid, "owner_token": owner, "share_token": share}


@app.post("/api/records/{rid}/chunk")
async def upload_chunk(rid: str, request: Request, seq: int, mime: str = "", lat: float | None = None,
                       lon: float | None = None, x_token: str | None = Header(None)):
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty chunk")
    with db() as con:
        row = require_owner(con, rid, x_token)
        if row["status"] != "recording":
            raise HTTPException(409, "Recording already finished")
        meta = {"seq": seq, "mime": mime, "lat": lat, "lon": lon}
        res = add_item(con, rid, "audio_chunk", f"chunk_{seq:05d}.part", data, meta)
        if lat is not None and lon is not None:
            con.execute("UPDATE records SET last_lat=?,last_lon=?,last_pos_at=?,mime=? WHERE id=?",
                        (lat, lon, res["server_time"], mime, rid))
    return {"ok": True, "seq": seq, "sha256": res["sha256"]}


@app.post("/api/records/{rid}/finish")
def finish(rid: str, x_token: str | None = Header(None)):
    with db() as con:
        row = require_owner(con, rid, x_token)
        if row["status"] == "recording":
            chunks = con.execute(
                "SELECT * FROM items WHERE record_id=? AND kind='audio_chunk' ORDER BY id", (rid,)).fetchall()
            if chunks:
                mime = json.loads(chunks[0]["meta"]).get("mime", "") or row["mime"]
                ext = "mp4" if "mp4" in mime else "ogg" if "ogg" in mime else "webm"
                buf = b"".join((record_dir(rid) / c["filename"]).read_bytes() for c in chunks)
                add_item(con, rid, "audio", f"audio.{ext}", buf,
                         {"chunks": len(chunks), "mime": mime})
            con.execute("UPDATE records SET status='sealed' WHERE id=?", (rid,))
        row = get_record(con, rid)
        return record_view(con, row, "owner")


@app.post("/api/records/{rid}/photo")
async def upload_photo(rid: str, file: UploadFile = File(...), x_token: str | None = Header(None)):
    data = await file.read()
    with db() as con:
        require_owner(con, rid, x_token)
        n = con.execute("SELECT COUNT(*) c FROM items WHERE record_id=? AND kind='photo'", (rid,)).fetchone()["c"]
        ext = (Path(file.filename or "").suffix or ".jpg").lower()[:6]
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}:
            raise HTTPException(400, "Photos only (jpg, png, webp, heic, gif)")
        res = add_item(con, rid, "photo", f"photo_{n + 1}{ext}", data,
                       {"original_name": (file.filename or "")[:80]})
    return {"ok": True, **res}


@app.post("/api/records/{rid}/details")
async def save_details(rid: str, request: Request, x_token: str | None = Header(None)):
    """Details are stored as an append-only, timestamped note. Earlier versions stay in the chain."""
    body = await request.json()
    fields = {k: str(body.get(k, ""))[:2000] for k in
              ("incident_type", "description", "people", "safety_status")}
    with db() as con:
        require_owner(con, rid, x_token)
        n = con.execute("SELECT COUNT(*) c FROM items WHERE record_id=? AND kind='note'", (rid,)).fetchone()["c"]
        add_item(con, rid, "note", f"note_{n + 1}.json", json.dumps(fields, sort_keys=True).encode(),
                 {"version": n + 1})
        con.execute("UPDATE records SET incident_type=?,description=?,people=?,safety_status=? WHERE id=?",
                    (fields["incident_type"], fields["description"], fields["people"],
                     fields["safety_status"], rid))
    return {"ok": True, "version": n + 1}


@app.post("/api/records/{rid}/share")
async def share(rid: str, request: Request, x_token: str | None = Header(None)):
    body = await request.json()
    scope = body.get("scope", "location")
    if scope not in ("location", "full"):
        raise HTTPException(400, "scope must be 'location' or 'full'")
    base = str(request.base_url).rstrip("/")
    with db() as con:
        row = require_owner(con, rid, x_token)
        con.execute("UPDATE records SET share_scope=?,contact_name=?,telegram_chat=? WHERE id=?",
                    (scope, str(body.get("contact_name", ""))[:80], str(body.get("telegram_chat", ""))[:40], rid))
        url = f"{base}/s/{row['share_token']}"
        who = str(body.get("contact_name", "")).strip() or "your contact"
        send_telegram(str(body.get("telegram_chat", "")).strip(),
                      f"EvidenceTap alert: a record was shared with {who}. Open to see location"
                      f"{' and evidence' if scope == 'full' else ''}: {url}")
    return {"ok": True, "url": url, "scope": scope, "telegram_configured": bool(TELEGRAM_TOKEN)}


@app.get("/api/records/{rid}")
def get_record_api(rid: str, x_token: str | None = Header(None), t: str | None = Query(None)):
    with db() as con:
        row = get_record(con, rid)
        level = access_level(row, tok(x_token, t))
        if not level:
            raise HTTPException(403, "Access denied")
        return record_view(con, row, level)


@app.delete("/api/records/{rid}")
def delete_record(rid: str, x_token: str | None = Header(None)):
    with db() as con:
        require_owner(con, rid, x_token)
        con.execute("DELETE FROM items WHERE record_id=?", (rid,))
        con.execute("DELETE FROM records WHERE id=?", (rid,))
    d = FILES / rid
    if d.exists():
        for p in d.iterdir():
            p.unlink()
        d.rmdir()
    return {"ok": True}


@app.get("/api/records/{rid}/integrity")
def integrity(rid: str, x_token: str | None = Header(None), t: str | None = Query(None)):
    with db() as con:
        row = get_record(con, rid)
        if not access_level(row, tok(x_token, t)):
            raise HTTPException(403, "Access denied")
        return check_integrity(con, rid)


def serve_item(rid: str, kind: str, item_id: int | None, token: str | None):
    with db() as con:
        row = get_record(con, rid)
        level = access_level(row, token)
        if not level or (level == "share" and row["share_scope"] != "full"):
            raise HTTPException(403, "Access denied")
        if item_id is not None:
            it = con.execute("SELECT * FROM items WHERE id=? AND record_id=? AND kind=?",
                             (item_id, rid, kind)).fetchone()
        else:
            it = con.execute("SELECT * FROM items WHERE record_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                             (rid, kind)).fetchone()
        if not it:
            raise HTTPException(404, "Not found")
    p = record_dir(rid) / it["filename"]
    mt = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if p.suffix == ".webm":
        mt = "audio/webm"
    if p.suffix == ".mp4":
        mt = "audio/mp4"
    return FileResponse(p, media_type=mt, filename=p.name)


@app.get("/api/records/{rid}/audio")
def get_audio(rid: str, x_token: str | None = Header(None), t: str | None = Query(None)):
    return serve_item(rid, "audio", None, tok(x_token, t))


@app.get("/api/records/{rid}/photo/{item_id}")
def get_photo(rid: str, item_id: int, x_token: str | None = Header(None), t: str | None = Query(None)):
    return serve_item(rid, "photo", item_id, tok(x_token, t))


# ---------------------------------------------------------------- API: trusted contact
@app.get("/api/s/{share_token}")
def shared_view(share_token: str):
    with db() as con:
        row = con.execute("SELECT * FROM records WHERE share_token=?", (share_token,)).fetchone()
        if not row or row["share_scope"] == "none":
            raise HTTPException(404, "This link is not active")
        v = record_view(con, row, "share")
        v["integrity"] = check_integrity(con, row["id"])
        if row["share_scope"] != "full":
            v.pop("fingerprint", None)
        return v


@app.post("/api/s/{share_token}/ack")
async def ack(share_token: str, request: Request):
    body = await request.json()
    status = body.get("status")
    if status not in ("responding", "calling", "informed_authorities"):
        raise HTTPException(400, "Invalid status")
    with db() as con:
        row = con.execute("SELECT * FROM records WHERE share_token=?", (share_token,)).fetchone()
        if not row or row["share_scope"] == "none":
            raise HTTPException(404, "This link is not active")
        con.execute("UPDATE records SET ack_status=?,ack_at=? WHERE id=?", (status, now_iso(), row["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- API: verify
@app.post("/api/verify/file")
async def verify_file(file: UploadFile = File(...)):
    data = await file.read()
    sha = sha256_bytes(data)
    with db() as con:
        rows = con.execute(
            "SELECT record_id,kind,server_time FROM items WHERE sha256=? AND kind!='audio_chunk'", (sha,)).fetchall()
    return {"sha256": sha, "size": len(data), "matches": [
        {"record_id": r["record_id"], "kind": r["kind"], "recorded_at": r["server_time"]} for r in rows]}


@app.get("/api/verify/hash/{sha}")
def verify_hash(sha: str):
    """Check a SHA-256 computed in the browser, so the file never has to be uploaded."""
    sha = sha.lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise HTTPException(400, "Not a SHA-256 hash")
    with db() as con:
        rows = con.execute(
            "SELECT record_id,kind,server_time FROM items WHERE sha256=? AND kind NOT IN ('audio_chunk','start','note')",
            (sha,)).fetchall()
    return {"sha256": sha, "matches": [
        {"record_id": r["record_id"], "kind": r["kind"], "recorded_at": r["server_time"]} for r in rows]}


@app.get("/api/verify/record/{rid}")
def verify_record(rid: str, fp: str | None = None):
    with db() as con:
        row = get_record(con, rid)
        res = check_integrity(con, rid)
        return {"id": rid, "created_at": row["created_at"], "integrity": res,
                "fingerprint_matches_pdf": (fp == res.get("fingerprint")) if fp else None}


# ---------------------------------------------------------------- PDF
def make_qr(url: str) -> io.BytesIO:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@app.get("/api/records/{rid}/pdf")
def pdf(rid: str, request: Request, x_token: str | None = Header(None), t: str | None = Query(None)):
    with db() as con:
        row = get_record(con, rid)
        if access_level(row, tok(x_token, t)) != "owner":
            raise HTTPException(403, "Owner token required")
        items = con.execute("SELECT * FROM items WHERE record_id=? ORDER BY id", (rid,)).fetchall()
        integ = check_integrity(con, rid)
    base = str(request.base_url).rstrip("/")
    fp = integ.get("fingerprint", "")
    verify_url = f"{base}/verify?record={rid}&fp={fp}"

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=20, textColor=colors.HexColor("#3B1F2B"), alignment=0)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, textColor=colors.HexColor("#B83257"), spaceBefore=10)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9.5, leading=13)
    mono = ParagraphStyle("m", parent=body, fontName="Courier", fontSize=7.2, leading=9)
    small = ParagraphStyle("s", parent=body, fontSize=8, textColor=colors.HexColor("#6B5560"))

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = [Paragraph("EvidenceTap incident record", h1),
             Paragraph(f"Record ID: <b>{rid}</b> &nbsp;|&nbsp; Created (server, UTC): {row['created_at']}", body),
             Spacer(1, 6)]

    loc = "Not captured"
    if row["lat"] is not None:
        loc = f"{row['lat']:.6f}, {row['lon']:.6f} (accuracy ~{round(row['accuracy'] or 0)} m)"
    summary = [
        ["Incident type", esc(row["incident_type"]) or "-"],
        ["Location at start", loc],
        ["Safety status", esc(row["safety_status"]) or "-"],
        ["Device clock difference", f"{row['clock_skew_s']} s" if row["clock_skew_s"] is not None else "unknown"],
    ]
    t1 = Table([[Paragraph(f"<b>{a}</b>", body), Paragraph(b, body)] for a, b in summary],
               colWidths=[45 * mm, 125 * mm])
    t1.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E7CFC4")),
                            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FFF1E8")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [Paragraph("Summary", h2), t1,
              Paragraph("What happened", h2), Paragraph(esc(row["description"]) or "No description added.", body),
              Paragraph("People involved", h2), Paragraph(esc(row["people"]) or "Not added.", body),
              Paragraph("Evidence files", h2)]

    rows = [["#", "Type", "Recorded (server, UTC)", "SHA-256"]]
    n = 0
    for it in items:
        if it["kind"] == "audio_chunk":
            continue
        n += 1
        rows.append([str(n), it["kind"], it["server_time"][:19].replace("T", " "),
                     Paragraph(it["sha256"], mono)])
    chunks = sum(1 for i in items if i["kind"] == "audio_chunk")
    t2 = Table(rows, colWidths=[8 * mm, 20 * mm, 42 * mm, 100 * mm], repeatRows=1)
    t2.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E7CFC4")),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FFE3D3")),
                            ("FONTSIZE", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t2, Spacer(1, 4),
              Paragraph(f"Audio was uploaded in {chunks} chunk(s) while recording; each chunk is part of the chain below.", small)]

    for it in [i for i in items if i["kind"] == "photo"][:4]:
        try:
            p = record_dir(rid) / it["filename"]
            if p.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                img = Image(str(p))
                scale = min(70 * mm / img.imageWidth, 70 * mm / img.imageHeight, 1)
                img.drawWidth, img.drawHeight = img.imageWidth * scale, img.imageHeight * scale
                story += [Spacer(1, 6), img, Paragraph(f"Photo {it['id']} (sha256 {it['sha256'][:16]}...)", small)]
        except Exception:
            pass

    qr = Image(make_qr(verify_url), 32 * mm, 32 * mm)
    ver = Table([[qr, [Paragraph("Verification", h2),
                       Paragraph("Scan the QR code, or open the link, to check this record's integrity and to test whether "
                                 "a file matches what was recorded.", body),
                       Paragraph(f"Integrity at export: <b>{'INTACT' if integ.get('ok') else 'BROKEN'}</b>", body),
                       Paragraph(f"Record fingerprint (final chain hash):<br/>{fp}", mono),
                       Paragraph(esc(verify_url), mono)]]], colWidths=[36 * mm, 134 * mm])
    ver.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [Spacer(1, 8), ver, Spacer(1, 8),
              Paragraph("This document helps organise evidence and shows whether recorded files have changed since they were "
                        "captured. It is not legal advice and does not guarantee that any recording is admissible. "
                        "Rules differ by case and place; consider speaking to a lawyer or a women's support service. "
                        "Emergency: 112. Women helpline: 181.", small)]

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                      bottomMargin=16 * mm, title=f"EvidenceTap record {rid}").build(story)
    return Response(buf.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="evidencetap-{rid}.pdf"'})


# ---------------------------------------------------------------- demo helper (off by default)
if DEMO_MODE:
    @app.post("/api/demo/tamper/{rid}")
    def demo_tamper(rid: str):
        """Flip one byte of a stored file so the integrity check can be shown failing. Demo only."""
        with db() as con:
            it = con.execute("SELECT * FROM items WHERE record_id=? AND kind IN ('audio','photo') "
                             "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        if not it:
            raise HTTPException(404, "Nothing to tamper with")
        p = record_dir(rid) / it["filename"]
        b = bytearray(p.read_bytes())
        b[len(b) // 2] ^= 0xFF
        p.write_bytes(bytes(b))
        return {"tampered": it["filename"]}


# ---------------------------------------------------------------- pages
STATIC = BASE / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def page(name):
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return page("index.html")


@app.get("/record")
def record_page():
    return page("record.html")


@app.get("/verify")
def verify_page():
    return page("verify.html")


@app.get("/s/{share_token}")
def share_page(share_token: str):
    return page("share.html")


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "demo_mode": DEMO_MODE, "telegram": bool(TELEGRAM_TOKEN)})
