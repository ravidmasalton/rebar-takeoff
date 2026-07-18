"""
FastAPI entry point for the rebar takeoff service.

The uploaded PDF is read into memory and analysed; nothing is written to disk
or a database. `/analyze` does only text extraction + counts + weights; the
two expensive passes are served on demand from a single in-memory slot
holding the most recent analysis (parsed rows + original PDF bytes; a new
upload replaces it):

* `/shapes` — bar-shape classification for all items (lazy, cached).
* `/evidence/{item_id}` — one rasterised crop per request, so memory never
  holds more than a single image.

`/analyze` accepts an optional client-generated `job_id`; while it runs (in
the threadpool — the endpoints doing heavy work are sync `def` precisely so
the event loop stays free), `/progress/{job_id}` reports `{phase, percent}`
for a frontend progress bar. The frontend single-page app is served from `/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from rebar_service import Analysis

app = FastAPI(title="Rebar Takeoff", version="1.2.0")

FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

# Single-slot cache of the last analysis: {"id": str, "analysis": Analysis}.
# One slot bounds memory on small servers; the id guards a stale frontend
# against silently receiving crops from someone else's later upload.
_last_analysis: dict = {}

# Progress per job_id: {"phase": str, "percent": float}. Cleared on each new
# upload (same single-slot policy as the analysis cache).
_progress: dict[str, dict] = {}


@app.post("/analyze")
def analyze(
    file: UploadFile = File(...),
    job_id: Optional[str] = Query(default=None, max_length=64),
) -> JSONResponse:
    """Accept a PDF upload and return the takeoff JSON (no images/shapes)."""
    if file.content_type not in ("application/pdf", "application/octet-stream") \
            and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    pdf_bytes = file.file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file.")

    _progress.clear()

    def report(phase: str, percent: float) -> None:
        if job_id:
            _progress[job_id] = {"phase": phase, "percent": percent}

    try:
        report("extract", 0.0)
        analysis = Analysis(pdf_bytes, progress=report)
        result = analysis.response()
    except Exception as exc:  # surface parsing failures as 422 rather than 500
        if job_id:
            _progress.pop(job_id, None)
        raise HTTPException(status_code=422, detail=f"Failed to parse PDF: {exc}")

    report("done", 100.0)
    analysis_id = uuid4().hex
    _last_analysis.clear()
    _last_analysis.update({"id": analysis_id, "analysis": analysis})
    result["analysis_id"] = analysis_id
    return JSONResponse(result)


@app.get("/progress/{job_id}")
async def progress(job_id: str) -> dict:
    """Progress of an in-flight /analyze with the same job_id."""
    # "pending" (not 404) when unknown: the poll can race the upload's start.
    return _progress.get(job_id, {"phase": "pending", "percent": 0.0})


@app.get("/shapes")
def shapes(analysis_id: str) -> dict:
    """Bar-shape classification for every item of the cached analysis."""
    if _last_analysis.get("id") != analysis_id:
        raise HTTPException(
            status_code=404,
            detail="Analysis no longer available; re-upload the PDF.",
        )
    return {"shapes": _last_analysis["analysis"].shapes()}


@app.get("/evidence/{item_id}")
async def evidence(item_id: int, analysis_id: str) -> Response:
    """Render the evidence crop for one item of the cached analysis."""
    if _last_analysis.get("id") != analysis_id:
        raise HTTPException(
            status_code=404,
            detail="Analysis no longer available; re-upload the PDF.",
        )
    png = _last_analysis["analysis"].render_evidence(item_id)
    if png is None:
        raise HTTPException(status_code=404, detail="No evidence crop for this item.")
    # The crop for a given (analysis, item) never changes: let the browser
    # cache it so reopening the modal or re-scrolling costs nothing.
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_INDEX)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
