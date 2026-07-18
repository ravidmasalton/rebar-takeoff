---
name: verify
description: Build, launch and drive the rebar takeoff app to verify changes end-to-end.
---

# Verifying the rebar takeoff app

## Launch

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8123 --app-dir backend
```

(`python backend/main.py` also works but hardcodes port 8000.) No build step;
deps from `backend/requirements.txt`. Check readiness with `GET /health`.

## Generate a test PDF

Real inputs are vector drawings with `ö`-encoded diameter callouts. Build one
with reportlab (see `backend/test_rebar_service.py::_make_pdf` for the
minimal recipe): `c.drawString(100, 700, "ö10@20")` + `"L=100"` + a width
number `"300"` over a 1pt dimension line, plus a 3pt line for the bar itself.

## Drive

```powershell
curl.exe -s -F "file=@test.pdf;type=application/pdf" http://127.0.0.1:8123/analyze
# response: items (no images) + summary + analysis_id
curl.exe -s "http://127.0.0.1:8123/evidence/1?analysis_id=<id>" -o crop.png
```

- `/analyze` must NOT contain `evidence_png` (crops are on-demand only; that
  was the 512MB-server OOM).
- Read `crop.png` with the Read tool to eyeball overlay alignment: red box on
  the callout, blue on the width number, orange on `L=`.
- Stale/foreign `analysis_id` → 404; a new upload replaces the single cache
  slot (old id must 404 afterwards).
- `item.shape` is `null` in `/analyze`; `GET /shapes?analysis_id=<id>` computes
  shapes lazily (cached — second call must be near-instant).
- Progress: POST `/analyze?job_id=<uuid>` and poll `GET /progress/<job_id>`
  concurrently (needs threads — see `scratchpad drive_server.py` pattern);
  phases extract/detect/compute, monotonic percent, `done` at 100. Unknown
  job → `{"phase": "pending"}`, not 404.
- Frontend at `/` — thumbnails are `<img loading="lazy">` pointing at
  `/evidence/{id}?analysis_id=…`; progress bar polls `/progress/{job_id}`.

## Gotchas

- Windows: use `curl.exe`, not the PowerShell `curl` alias.
