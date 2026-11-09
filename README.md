# EvidenceTap

One tap captures audio, time and location into a record that shows if anything is changed afterwards. Built for HERHACK 2026 (Student Innovation track, Women Safety award).

**Problem.** Harassment happens in seconds, and when a woman goes to report it she often has no usable proof. EvidenceTap does not try to prevent harm. It fixes the step where many complaints stall: having evidence in a form that can be checked.

## What it does

1. **Capture.** One tap starts a visible audio recording, notes the time and GPS, and uploads the audio in 5-second parts as it is recorded, so earlier parts survive if the phone is lost.
2. **Evidence chain.** Every file is SHA-256 hashed and linked to the one before it (hash chain, server timestamps). Editing or reordering anything breaks the chain.
3. **Add context.** Photos or screenshots, plus an incident form. Each save is a new timestamped version; nothing is overwritten.
4. **Share.** A private link for a trusted contact, with two scopes: location only, or location and evidence. The contact sees a live position while recording and can mark "responding", "calling" or "informed authorities".
5. **Complaint-ready PDF.** Summary, every file's fingerprint, and a QR code that opens the verify page.
6. **Verify.** Anyone can drop a file on `/verify`; its fingerprint is computed in the browser (the file is not uploaded) and checked against the records.
7. **Control.** The owner can delete a record and all its files at any time. Helplines 112 and 181 are one tap away.

## Run it

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Microphone and location need HTTPS on a phone, so for real-device testing deploy it (Render, Railway, Fly) or use an HTTPS tunnel. `localhost` works on the same machine.

Optional environment variables:

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Sends the share link to a Telegram chat ID. WhatsApp link sharing works without it. |
| `EVIDENCETAP_DATA` | Folder for the database and files (default `./data`). |
| `EVIDENCETAP_DEMO=1` | Enables `/api/demo/tamper/{id}`, which flips one byte in a stored file so you can show the integrity check failing. Never enable this in real use. |
| `EVIDENCETAP_TEMP_STORAGE` | Defaults to on: every page shows a "demo server, records are temporary" warning. Set to `0` only when `EVIDENCETAP_DATA` points at a persistent disk. |

**Storage is temporary on Render's free tier.** Render's free web services lose local files, including SQLite databases, on every redeploy, restart and spin-down (after 15 minutes without traffic). Records on a free Render deployment can disappear. Use it for demos only, and download the PDF and audio of anything you want to keep. A persistent disk needs a paid Render service; set `EVIDENCETAP_DATA` to the disk's mount path and `EVIDENCETAP_TEMP_STORAGE=0`.

Run the tests:

```bash
python test_flow.py                              # 40 checks: capture, chain, sharing scopes, PDF, verify, tamper detection, delete
python -m unittest -v test_audio_reliability     # 25 checks: duplicate/conflicting/missing/out-of-order chunks, finish races, corruption
node test_uploader.js                            # 9 checks: upload retry and recovery logic used by the capture page
```

The same three commands run in GitHub Actions (`.github/workflows/tests.yml`).

## How audio recording stays reliable

- **Idempotent uploads.** Each 5-second part has a sequence number. Re-sending the same bytes for a number is accepted and adds nothing (and repairs the stored file if it was damaged). Different bytes for an already-saved number are refused with HTTP 409 and the original is never replaced.
- **Safe sealing.** Sealing runs in one database write transaction, so it cannot race with uploads or another seal request, and repeating it is harmless. The server checks there are no gaps, re-hashes every part, and builds the audio in sequence order, never arrival order.
- **Nothing sealed silently incomplete.** If parts are missing or damaged, or none were saved, sealing is refused (HTTP 409) and the message names the part numbers. All received parts are kept. The recording can be completed later, or explicitly sealed as `incomplete`; then the audio holds only the verified, unbroken part from the start, and the record says so.
- **Retry and recovery in the browser.** Temporary failures are retried with a growing delay. A part that still fails is kept, never discarded, and the user chooses to retry or to seal as incomplete. An unfinished recording can be finished from its record page.
- **Limit:** the page can only detect parts lost from the very end of a recording if the browser tells the server how many it produced (the capture page does). Parts not yet uploaded live in the browser tab only; closing the tab loses them.

## Stack

FastAPI, SQLite, vanilla HTML/JS (MediaRecorder, Geolocation, SubtleCrypto), reportlab and qrcode for the PDF. No machine-learning model: the logic is deliberately rule-based and easy to audit.

## Demo script (about 2 minutes)

1. Phone A: tap, speak for 10 seconds. Phone B (trusted contact) opens the share link and shows the position and a "Recording right now" badge.
2. Contact taps "I'm responding"; Phone A shows it.
3. Stop and seal. Add a photo and a note. Open the PDF.
4. Scan the PDF's QR code: the verify page shows INTACT.
5. Download the audio, edit or re-save it, drop it on the verify page: "No match". Drop the original: "Match".
6. With demo mode on, call the tamper endpoint, re-check: "file content changed".

## Honest limits

- It is a web page, not a native app. It cannot record with the phone locked, be triggered by hardware buttons, or hide the browser's microphone indicator, and it deliberately does not try to. Recording is always visible and user-started.
- Audio parts are joined by simple concatenation. This works for Chrome and Android (WebM). Safari/iOS MP4 output may not play back as one file; test on Android Chrome for the demo.
- Server timestamps come from the server clock. A trusted third-party timestamp (RFC 3161) would be stronger and is future work. The phone's clock difference is recorded and flagged.
- Files are stored unencrypted on the server. End-to-end encryption, redaction tools, offline queue persistence and native-app triggers are future work.
- The tool helps organise evidence and shows whether files changed. It does not guarantee a recording is admissible; the PDF says so, and the pitch should too.
- Prototype scale: one SQLite file, no accounts. Owner access is a secret token kept in the browser's local storage; clearing browser data loses access to the record.

## Files

```
app.py                     API, hash chain, PDF, verify
static/                    index (capture), record (owner view), share (trusted contact), verify
static/uploader.js         chunk upload queue: retry, backoff, keep-on-failure
test_flow.py               end-to-end checks
test_audio_reliability.py  audio reliability regression tests
test_uploader.js           tests for the upload queue
```


## Exit Call (simulated incoming call)

The home screen includes an optional **Simulate an incoming call** control. It opens an on-screen incoming-call interface with Accept and Decline actions. Accepting switches to a simulated call screen with an elapsed-time display and local Mute/Speaker UI toggles; ending or declining closes it.

This is a visual interaction only. It does not place or receive a telephone call, play a connected caller voice, trigger from the lock screen or hardware buttons, or record anything. Mute and Speaker are visual demo controls and do not control phone audio.
