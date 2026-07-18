"""
FastAPI entry point for the rebar takeoff service.

The uploaded PDF is read into memory and analysed; nothing is written to disk
or a database. `/analyze` returns the takeoff without images — evidence crops
are rasterised one at a time by `/evidence/{item_id}`, so memory never holds
more than a single crop. To serve those on-demand renders, the most recent
analysis (parsed rows + original PDF bytes) is kept in a single in-memory
slot; a new upload replaces it. The frontend single-page app is served
from `/`.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from rebar_service import Analysis

app = FastAPI(title="Rebar Takeoff", version="1.1.0")

FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

# Single-slot cache of the last analysis: {"id": str, "analysis": Analysis}.
# One slot bounds memory on small servers; the id guards a stale frontend
# against silently receiving crops from someone else's later upload.
_last_analysis: dict = {}


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)) -> JSONResponse:
    """Accept a PDF upload and return the takeoff JSON (no images)."""
    if file.content_type not in ("application/pdf", "application/octet-stream") \
            and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file.")

    try:
        analysis = Analysis(pdf_bytes)
        result = analysis.response()
    except Exception as exc:  # surface parsing failures as 422 rather than 500
        raise HTTPException(status_code=422, detail=f"Failed to parse PDF: {exc}")

    analysis_id = uuid4().hex
    _last_analysis.clear()
    _last_analysis.update({"id": analysis_id, "analysis": analysis})
    result["analysis_id"] = analysis_id
    return JSONResponse(result)


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
